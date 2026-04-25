#!/usr/bin/env python3
"""
CV-based floorplan → result.json, with optional Gemini refinement.

Steps:
  1. OpenCV extracts candidate room regions (`extract_rooms_cv_v2`).
  2. (optional, --gemini-gapfill) Gemini looks at the original image with
     existing rooms boxed in orange and reports any obvious rooms/pathways
     that were missed. Their bboxes are added to the candidate set.
  3. (optional, --gemini-label) For each region, Gemini labels the cropped
     image with a single room type.
  4. Regions are written to a schema-compatible `FloorPlanResult` JSON plus
     a debug overlay PNG.

Usage:
  # CV only
  python floorplan_parser/cv_parse_floorplan.py \
    --image /path/to/floorplan.png \
    --output floorplan_parser/result.json \
    --debug-out data/cv_debug.png

  # CV + Gemini refinement (needs GEMINI_API_KEY in .env or --api-key)
  python floorplan_parser/cv_parse_floorplan.py \
    --image /path/to/floorplan.png \
    --output floorplan_parser/result.json \
    --debug-out data/cv_debug.png \
    --gemini-gapfill --gemini-label
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import cv2

from cv_rooms import (
    RoomRegion,
    extract_rooms_cv,
    extract_rooms_cv_multiscale,
    extract_rooms_cv_v2,
    merge_regions,
    regions_to_floor_objects,
)
from schema import DimensionsPx, FloorPlan, FloorPlanResult, ParseMetadata

log = logging.getLogger("cv_parse_floorplan")


def _load_dotenv_if_present() -> None:
    """Best-effort load of .env (repo root or floorplan_parser/.env)."""
    candidates = [
        Path(__file__).resolve().parents[1] / ".env",
        Path(__file__).resolve().parent / ".env",
    ]
    for env_path in candidates:
        try:
            if not env_path.exists():
                continue
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and v and k not in os.environ:
                    os.environ[k] = v
            return
        except Exception:
            return


def _draw_overlay(image_bgr, regions: list[RoomRegion], out_path: Path) -> None:
    vis = image_bgr.copy()
    color_by_source = {
        "cv": (0, 140, 255),
        "cv_v2": (0, 140, 255),
        "gemini_gapfill": (0, 0, 255),
    }
    for r in regions:
        color = color_by_source.get(r.source, (0, 140, 255))
        x, y = r.bbox_px["x"], r.bbox_px["y"]
        w, h = r.bbox_px["w"], r.bbox_px["h"]
        cv2.rectangle(vis, (x, y), (x + w, y + h), color, 2)
        # Prefer the verbatim printed text when available, else the canonical
        # type hint.
        display = (r.label_text or r.label_hint or "").strip()
        caption = f"{r.id}" + (f" · {display}" if display else "")
        cv2.putText(
            vis,
            caption,
            (x, max(12, y - 4)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            color,
            1,
            cv2.LINE_AA,
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), vis)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    ap = argparse.ArgumentParser(description="CV-based floor plan → JSON (optional Gemini refine)")
    ap.add_argument("--image", required=True, help="Path to floor plan image")
    ap.add_argument("--output", default="result.json", help="Output JSON path")
    ap.add_argument("--debug-out", default=None, help="Optional debug overlay PNG")
    ap.add_argument("--algorithm", choices=["multiscale", "v2", "v1"], default="multiscale",
                    help="CV algorithm (default multiscale: union over multiple door-seals)")

    # v2 knobs
    ap.add_argument("--threshold", type=int, default=None,
                    help="Wall threshold (omit for Otsu auto)")
    ap.add_argument("--door-seal-px", type=int, default=9,
                    help="Dilate walls by this many pixels to seal door gaps (v2)")
    ap.add_argument("--seal-scales", type=str, default="5,9,13",
                    help="Comma-separated seal sizes to union (multiscale only)")
    ap.add_argument("--min-wall-component-px", type=int, default=20,
                    help="Drop wall components smaller than this (text/icons)")
    ap.add_argument("--min-area-frac", type=float, default=0.0015,
                    help="Min room area as fraction of image")
    ap.add_argument("--max-area-frac", type=float, default=0.45,
                    help="Max room area as fraction of image")

    # Gemini refinement
    ap.add_argument("--gemini-gapfill", action="store_true",
                    help="Ask Gemini for rooms missed by CV")
    ap.add_argument("--gemini-label", action="store_true",
                    help="Ask Gemini to classify each CV region (per-crop, slow)")
    ap.add_argument("--gemini-global-label", action="store_true",
                    help="One-shot global labeling: send full plan with numbered "
                         "boxes and ask Gemini to label every region at once. "
                         "Preferred over --gemini-label for institutional plans.")
    ap.add_argument("--api-key", default=None, help="Gemini API key (else uses .env)")
    ap.add_argument("--gemini-model", default="gemini-2.5-flash",
                    help="Model for per-crop/gapfill calls")
    ap.add_argument("--gemini-global-model", default="gemini-2.5-pro",
                    help="Model for the one-shot global labeling call")

    args = ap.parse_args()

    image_path = Path(args.image)
    if not image_path.exists():
        log.error("image not found: %s", image_path)
        return 1

    img = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if img is None:
        log.error("failed to read image: %s", image_path)
        return 1

    h, w = img.shape[:2]
    log.info("Image loaded: %dx%d px", w, h)

    # ── CV pass ─────────────────────────────────────────────────────────────
    if args.algorithm == "multiscale":
        seals = tuple(int(s.strip()) for s in args.seal_scales.split(",") if s.strip())
        regions, _debug = extract_rooms_cv_multiscale(
            img,
            seal_pxs=seals,
            threshold=args.threshold,
            min_wall_component_px=args.min_wall_component_px,
            min_area_frac=args.min_area_frac,
            max_area_frac=args.max_area_frac,
        )
    elif args.algorithm == "v2":
        regions, _debug = extract_rooms_cv_v2(
            img,
            threshold=args.threshold,
            door_seal_px=args.door_seal_px,
            min_wall_component_px=args.min_wall_component_px,
            min_area_frac=args.min_area_frac,
            max_area_frac=args.max_area_frac,
        )
    else:
        regions, _debug = extract_rooms_cv(
            img,
            threshold=args.threshold if args.threshold is not None else 180,
            min_area_frac=args.min_area_frac,
        )
    log.info("CV extracted: %d region(s)", len(regions))

    # ── Gemini refinement (optional) ────────────────────────────────────────
    if args.gemini_gapfill or args.gemini_label or args.gemini_global_label:
        api_key = args.api_key
        if not api_key:
            _load_dotenv_if_present()
            api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            log.error("Gemini refinement requested but GEMINI_API_KEY not set.")
            return 2

        from google import genai
        from cv_gemini_refine import find_missed_regions, label_regions, label_regions_global

        client = genai.Client(api_key=api_key)

        if args.gemini_gapfill:
            log.info("Gemini gap-fill: asking for missed regions…")
            missed = find_missed_regions(img, regions, client=client, model_name=args.gemini_model)
            log.info("Gemini gap-fill: +%d region(s)", len(missed))
            regions = merge_regions(regions, missed)

        if args.gemini_global_label:
            log.info("Gemini global label: one-shot labeling %d region(s) via %s…",
                     len(regions), args.gemini_global_model)
            regions = label_regions_global(
                img, regions, client=client, model_name=args.gemini_global_model
            )
            n_labeled = sum(1 for r in regions if r.label_hint or r.label_text)
            log.info("Gemini global label: %d/%d regions have label/type", n_labeled, len(regions))

        if args.gemini_label:
            # Per-crop pass. When combined with global labeling, only fill in
            # the regions the global pass left unlabeled.
            only_missing = bool(args.gemini_global_label)
            log.info("Gemini per-crop label: classifying %d region(s)%s…",
                     len(regions), " (missing only)" if only_missing else "")
            regions = label_regions(
                img, regions, client=client, model_name=args.gemini_model,
                only_missing=only_missing,
            )
            n_labeled = sum(1 for r in regions if r.label_hint or r.label_text)
            log.info("Gemini per-crop label: %d/%d regions have label/type",
                     n_labeled, len(regions))

    # ── Write debug overlay ─────────────────────────────────────────────────
    if args.debug_out:
        _draw_overlay(img, regions, Path(args.debug_out))
        log.info("Wrote debug overlay: %s", args.debug_out)

    # ── Build schema JSON ───────────────────────────────────────────────────
    objs = regions_to_floor_objects(regions)
    floor_plan = FloorPlan(
        source_image=image_path.name,
        dimensions_px=DimensionsPx(width=w, height=h),
        parse_metadata=ParseMetadata(
            tile_grid="cv",
            overlap_pct=0.0,
            tiles_parsed=1,
            total_objects_before_dedup=len(objs),
            total_objects_after_dedup=len(objs),
        ),
        rooms=objs,
        corridors=[],
        doors=[],
        verticals=[],
        emergency=[],
        labels=[],
        low_confidence_flags=[],
        navigation_graph={"nodes": [], "edges": []},
    )
    result = FloorPlanResult(floor_plan=floor_plan)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.model_dump_json(indent=2, by_alias=True), encoding="utf-8")

    # ── Summary ─────────────────────────────────────────────────────────────
    by_type: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for r, o in zip(regions, objs):
        by_type[o["type"]] = by_type.get(o["type"], 0) + 1
        by_source[r.source] = by_source.get(r.source, 0) + 1

    print(f"\nOutput written to: {out_path}")
    print(f"Total rooms:       {len(objs)}")
    print("By source:")
    for k, v in sorted(by_source.items()):
        print(f"  {k:<16} {v}")
    print("By type:")
    for k, v in sorted(by_type.items()):
        print(f"  {k:<16} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
