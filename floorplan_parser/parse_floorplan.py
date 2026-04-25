#!/usr/bin/env python3
from __future__ import annotations
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from PIL import Image

import gemini_client as gc
import tiler as tl
import tile_prompt as tp
import merger as mg
import validator as vl
import pre_pass as pp
import constraints as cs
from schema import (
    DimensionsPx, FloorPlan, FloorPlanResult, ParseMetadata, TileResponse,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("parse_floorplan")


def _load_dotenv_if_present() -> None:
    """
    Best-effort load of GEMINI_API_KEY from a local .env file.

    This script reads environment variables via os.environ; many users keep
    secrets in a `.env` at the repo root and expect it to work automatically.
    We avoid adding a hard dependency by parsing simple KEY=VALUE lines.
    """
    candidates = [
        # repo root: braille-map/.env
        Path(__file__).resolve().parents[1] / ".env",
        # also allow floorplan_parser/.env
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


def _parse_grid(s: str) -> tuple[int, int] | None:
    """Parse CxR (e.g. '3x2'). Returns None for 'auto' — resolved later from image size."""
    if s.strip().lower() == "auto":
        return None
    parts = s.lower().replace("x", " ").split()
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"Grid must be CxR (e.g. 3x2) or 'auto', got: {s!r}")
    return int(parts[0]), int(parts[1])


# ── Async orchestration ──────────────────────────────────────────────────────

def _tile_local_to_global(response: TileResponse, spec: tl.TileSpec | tl.SeamSpec) -> TileResponse:
    """Transform tile-local (0–100) coords to global (0–100) coords."""
    from schema import Position, Point as Pt

    def t_pos(p) -> Position:
        g = tl.tile_to_global_bbox({"x": p.x, "y": p.y, "w": p.w, "h": p.h}, spec)
        return Position(**{k: max(0.0, min(100.0, v)) for k, v in g.items()})

    def t_pt(p: Pt) -> Pt:
        gx, gy = tl.tile_to_global_point(p.x, p.y, spec)
        return Pt(x=max(0.0, min(100.0, gx)), y=max(0.0, min(100.0, gy)))

    new_objs = [obj.model_copy(update={"position": t_pos(obj.position)}) for obj in response.objects]
    new_corrs = [
        corr.model_copy(update={"centerline": [t_pt(p) for p in corr.centerline]})
        for corr in response.corridors
    ]
    return TileResponse(tile_id=response.tile_id, objects=new_objs,
                        corridors=new_corrs, scale_detected=response.scale_detected)


async def _run_tiles(
    client,
    model_name: str,
    img: Image.Image,
    specs: list[tl.TileSpec],
    skeleton_context: str,
    landmark_context: str,
    landmark: dict | None,
) -> list[TileResponse]:
    async def process_one(spec: tl.TileSpec) -> TileResponse:
        img_bytes = tl.crop_to_bytes(img, spec.px_x0, spec.px_y0, spec.px_x1, spec.px_y1)
        prompt = tp.build_tile_prompt(spec, specs, skeleton_context, landmark_context)
        raw = await gc.call_gemini(client, model_name, prompt, img_bytes, spec.tile_id)
        response = vl.parse_tile_response(raw, spec.tile_id)
        if raw.get("_parse_error") and response.objects:
            for obj in response.objects:
                object.__setattr__(obj, "confidence", "low")
        # Convert tile-local → global before any further processing
        response = _tile_local_to_global(response, spec)
        # Fix 2: apply landmark coordinate correction if applicable
        response = pp.apply_landmark_correction(response, spec.global_range(), landmark)
        return response

    tasks = [process_one(s) for s in specs]
    return list(await asyncio.gather(*tasks))


async def _run_seams(
    client,
    model_name: str,
    img: Image.Image,
    seam_specs: list[tl.SeamSpec],
) -> list[TileResponse]:
    async def process_seam(spec: tl.SeamSpec) -> TileResponse:
        img_bytes = tl.crop_to_bytes(img, spec.px_x0, spec.px_y0, spec.px_x1, spec.px_y1)
        prompt = tp.build_seam_prompt(spec)
        raw = await gc.call_gemini(client, model_name, prompt, img_bytes, spec.seam_id)
        resp = vl.parse_tile_response(raw, spec.seam_id)
        resp = _tile_local_to_global(resp, spec)
        for obj in resp.objects:
            object.__setattr__(obj, "source", "seam_review")
        return resp

    tasks = [process_seam(s) for s in seam_specs]
    return list(await asyncio.gather(*tasks))


# ── Main pipeline ────────────────────────────────────────────────────────────

async def run_pipeline(
    image_path: Path,
    api_key: str,
    grid_cols: int,
    grid_rows: int,
    overlap: float,
    model_name: str,
    *,
    do_seams: bool = True,
) -> FloorPlanResult:
    img = Image.open(image_path).convert("RGB")
    img_w, img_h = img.size
    log.info("Image loaded: %dx%d px", img_w, img_h)

    client = gc.make_client(api_key)

    # ── Fix 1: structural pre-pass — skeleton + landmark ─────────────────────
    log.info("Running structural pre-pass…")
    skeleton = await pp.structural_pass(client, model_name, img)
    skeleton_ctx = pp.format_skeleton_for_prompt(skeleton)
    landmark_ctx = pp.format_landmark_for_prompt(skeleton)
    landmark = skeleton.get("landmark")

    # ── 1. Tile pass (with skeleton + landmark context) ───────────────────────
    specs = tl.compute_tiles(img_w, img_h, grid_cols, grid_rows, overlap)
    log.info("Dispatching %d tiles (%dx%d grid, %.0f%% overlap)…",
             len(specs), grid_cols, grid_rows, overlap * 100)
    tile_responses = await _run_tiles(client, model_name, img, specs, skeleton_ctx, landmark_ctx, landmark)

    all_objects_pre: list = []
    all_corridors_pre: list = []
    for resp in tile_responses:
        all_objects_pre.extend(resp.objects)
        all_corridors_pre.extend(resp.corridors)

    total_before = len(all_objects_pre)
    log.info("Tile pass done. Raw objects: %d, corridors: %d", total_before, len(all_corridors_pre))

    # ── 2. Fix 3: type-aware dedup ────────────────────────────────────────────
    deduped_objects = mg.dedup_objects(all_objects_pre)
    deduped_corridors = mg.dedup_corridors(all_corridors_pre)
    total_after = len(deduped_objects)
    log.info("After dedup: %d objects (%d removed), %d corridors",
             total_after, total_before - total_after, len(deduped_corridors))

    # ── 3. Seam review (optional) ─────────────────────────────────────────────
    # This step is expensive; it improves cross-tile consistency, but we want
    # a usable `result.json` even if the user interrupts or chooses to skip.
    combined_objects = deduped_objects
    combined_corridors = deduped_corridors
    seam_additions = 0
    if do_seams:
        seam_specs = tl.compute_seams(img_w, img_h, grid_cols, grid_rows)
        log.info("Running seam review on %d seams…", len(seam_specs))
        try:
            seam_responses = await _run_seams(client, model_name, img, seam_specs)
        except KeyboardInterrupt:
            log.warning("Seam review interrupted; continuing with tile-pass result.")
            seam_responses = []

        seam_objects: list = []
        seam_corridors: list = []
        for sr in seam_responses:
            seam_objects.extend(sr.objects)
            seam_corridors.extend(sr.corridors)

        combined_objects = mg.dedup_objects(deduped_objects + seam_objects)
        combined_corridors = mg.dedup_corridors(deduped_corridors + seam_corridors)
        seam_additions = len(combined_objects) - total_after
        log.info("Seam review added %d new objects", seam_additions)

    # ── 4. Merge skeleton corridors from pre-pass ─────────────────────────────
    # Convert skeleton raw corridor dicts into Corridor objects and merge in
    from schema import Corridor, Point
    skeleton_corridors: list[Corridor] = []
    for c in skeleton.get("corridors", []):
        try:
            centerline = [Point(x=p["x"], y=p["y"]) for p in c.get("centerline", [])]
            if centerline:
                skeleton_corridors.append(Corridor(
                    id=f"skel_{c.get('id', 'corr')}",
                    type=c.get("type", "primary_corridor"),
                    centerline=centerline,
                    accessible=c.get("accessible", True),
                ))
        except Exception:
            pass
    if skeleton_corridors:
        combined_corridors = mg.dedup_corridors(skeleton_corridors + combined_corridors)
        log.info("After merging skeleton corridors: %d total corridors", len(combined_corridors))

    # ── 5. Categorize ─────────────────────────────────────────────────────────
    buckets = vl.categorize(combined_objects)

    # ── 6. Assemble floor plan ────────────────────────────────────────────────
    floor_plan = FloorPlan(
        source_image=image_path.name,
        dimensions_px=DimensionsPx(width=img_w, height=img_h),
        parse_metadata=ParseMetadata(
            tile_grid=f"{grid_cols}x{grid_rows}",
            overlap_pct=overlap,
            tiles_parsed=len(specs),
            total_objects_before_dedup=total_before,
            total_objects_after_dedup=total_after,
        ),
        rooms=buckets["rooms"],
        corridors=combined_corridors,
        doors=buckets["doors"],
        verticals=buckets["verticals"],
        emergency=buckets["emergency"],
        labels=buckets["labels"],
        low_confidence_flags=[],
        navigation_graph=mg.build_nav_graph(
            corridors=combined_corridors,
            rooms=buckets["rooms"],
            doors=buckets["doors"],
            verticals=buckets["verticals"],
        ),
    )

    # ── 7. Fix 4: constraint solver ───────────────────────────────────────────
    floor_plan, constraint_issues = cs.validate_and_resolve(floor_plan)
    log.info("Constraint solver: %d issues resolved", len(constraint_issues))

    # ── 8. Fix 5: grid snap ───────────────────────────────────────────────────
    floor_plan = cs.snap_all_coordinates(floor_plan)

    # ── 9. Low-confidence flags (after all fixes) ─────────────────────────────
    all_objects = (
        floor_plan.rooms + floor_plan.doors + floor_plan.verticals
        + floor_plan.emergency + floor_plan.labels
    )
    floor_plan.low_confidence_flags = vl.collect_low_confidence(all_objects)
    log.info("Low-confidence flags: %d", len(floor_plan.low_confidence_flags))

    return FloorPlanResult(floor_plan=floor_plan)


def _print_summary(result: FloorPlanResult, output_path: Path, seam_additions: int) -> None:
    fp = result.floor_plan
    m = fp.parse_metadata
    removed = m.total_objects_before_dedup - m.total_objects_after_dedup
    total = (
        len(fp.rooms) + len(fp.doors) + len(fp.verticals)
        + len(fp.emergency) + len(fp.labels)
    )
    print(f"\nTiles parsed:         {m.tiles_parsed}")
    print(f"Objects found:        {total}")
    print(f"Duplicates removed:   {removed}")
    print(f"Low confidence flags: {len(fp.low_confidence_flags)}")
    print(f"Seam review additions:{seam_additions}")
    print(f"Output written to:    {output_path}\n")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Tiled floor plan → structured JSON")
    parser.add_argument("--image", required=True, help="Path to floor plan image")
    parser.add_argument("--grid", default="auto", type=str,
                        help="Tile grid cols×rows (e.g. 3x2) or 'auto' — picked from image resolution (default: auto)")
    parser.add_argument("--overlap", default=0.10, type=float,
                        help="Tile overlap fraction (default 0.10; smaller = fewer duplicate detections)")
    parser.add_argument("--output", default="result.json", help="Output JSON path")
    parser.add_argument("--api-key", default=os.environ.get("GEMINI_API_KEY"), help="Gemini API key")
    parser.add_argument("--model", default="gemini-2.5-pro",
                        help="Gemini model (default: gemini-2.5-pro for best accuracy; use gemini-2.5-flash for speed)")
    parser.add_argument("--no-seams", action="store_true", help="Skip seam review (faster, less accurate)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load `.env` after parsing, then backfill args.api_key if needed.
    if not args.api_key:
        _load_dotenv_if_present()
        args.api_key = os.environ.get("GEMINI_API_KEY")

    if not args.api_key:
        sys.exit("Error: GEMINI_API_KEY not set. Use --api-key or set the env var.")

    image_path = Path(args.image)
    if not image_path.exists():
        sys.exit(f"Error: image not found: {image_path}")

    parsed_grid = _parse_grid(args.grid)
    if parsed_grid is None:
        # Peek image dims to pick an appropriate grid.
        import tiler as _tl
        with Image.open(image_path) as _probe:
            _pw, _ph = _probe.size
        grid_cols, grid_rows = _tl.auto_grid(_pw, _ph)
        log.info("Auto-grid: %d×%d px → %dx%d tiles", _pw, _ph, grid_cols, grid_rows)
    else:
        grid_cols, grid_rows = parsed_grid

    output_path = Path(args.output)

    result = asyncio.run(
        run_pipeline(
            image_path=image_path,
            api_key=args.api_key,
            grid_cols=grid_cols,
            grid_rows=grid_rows,
            overlap=args.overlap,
            model_name=args.model,
            do_seams=not args.no_seams,
        )
    )

    fp = result.floor_plan
    total_objects = (
        len(fp.rooms) + len(fp.doors) + len(fp.verticals)
        + len(fp.emergency) + len(fp.labels)
    )
    seam_additions = max(0, total_objects - fp.parse_metadata.total_objects_after_dedup)

    output_path.write_text(
        result.model_dump_json(indent=2, by_alias=True),
        encoding="utf-8",
    )
    _print_summary(result, output_path, seam_additions)


if __name__ == "__main__":
    main()
