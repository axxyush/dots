#!/usr/bin/env python3
"""
Floor-plan reconstruction pipeline (no LLM).

Given a parsed floor-plan JSON:
  1. Validate it against the Pydantic schema (schema.FloorPlanResult).
  2. Render a PNG from the JSON alone (render_map.render_floor_plan).
  3. Optionally build a side-by-side "input vs reconstructed" comparison image.
  4. Emit a reconstruction report (JSON) with counts, coverage, and warnings
     so you can verify the JSON conversion was accurate at a glance.

Usage:
    python reconstruct.py --json result.json --output out.png
    python reconstruct.py --json result.json --image original.png --output compare.png
    python reconstruct.py --json result.json --output out.png --report report.json
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from render_map import render_comparison, render_floor_plan
from schema import FloorPlan, FloorPlanResult

log = logging.getLogger("reconstruct")

# Object buckets used by the schema (match render_map layering)
_BUCKETS: tuple[str, ...] = (
    "rooms",
    "doors",
    "verticals",
    "emergency",
    "labels",
)


def _load_floor_plan_dict(json_path: Path) -> dict:
    """Accept both {"floor_plan": {...}} and a bare floor_plan dict."""
    data = json.loads(json_path.read_text())
    if isinstance(data, dict) and "floor_plan" in data:
        return data["floor_plan"]
    return data


def load_and_validate(json_path: Path) -> tuple[FloorPlan, list[str]]:
    """
    Validate JSON against the schema.

    Returns (floor_plan, warnings). If the top-level document is malformed we
    raise; per-object errors are collected and reported so rendering can still
    proceed on a salvaged dict.
    """
    fp_dict = _load_floor_plan_dict(json_path)
    warnings: list[str] = []

    # Strict path first — if the parser produced a clean document this passes.
    try:
        result = FloorPlanResult.model_validate({"floor_plan": fp_dict})
        return result.floor_plan, warnings
    except ValidationError as exc:
        warnings.append(f"Schema errors: {exc.error_count()} issue(s); salvaging buckets.")

    # Salvage path: validate the easy fields, keep only valid items per bucket.
    fp_core: dict[str, Any] = {
        "id": fp_dict.get("id", "floor_1"),
        "source_image": fp_dict.get("source_image", "unknown.png"),
        "dimensions_px": fp_dict.get("dimensions_px", {"width": 1000, "height": 1000}),
        "coordinate_system": fp_dict.get("coordinate_system", "normalized_0_to_100"),
        "parse_metadata": fp_dict.get(
            "parse_metadata",
            {
                "tile_grid": "1x1",
                "overlap_pct": 0.0,
                "tiles_parsed": 0,
                "total_objects_before_dedup": 0,
                "total_objects_after_dedup": 0,
            },
        ),
        "rooms": [],
        "doors": [],
        "verticals": [],
        "emergency": [],
        "labels": [],
        "low_confidence_flags": [],
        "corridors": fp_dict.get("corridors", []),
        "navigation_graph": fp_dict.get("navigation_graph", {"nodes": [], "edges": []}),
    }
    for bucket in _BUCKETS + ("low_confidence_flags",):
        for i, obj in enumerate(fp_dict.get(bucket, [])):
            try:
                from schema import FloorObject

                FloorObject.model_validate(obj)
                fp_core[bucket].append(obj)
            except ValidationError as e:
                warnings.append(f"{bucket}[{i}] invalid ({e.error_count()} err); dropped.")

    fp_ok = FloorPlan.model_validate(fp_core)
    return fp_ok, warnings


def _clamped_area(pos: dict) -> float:
    """Percent^2 area of a room clamped to the [0,100] canvas (to match renderer)."""
    x, y, w, h = (float(pos.get(k, 0.0)) for k in ("x", "y", "w", "h"))
    x0, y0 = max(0.0, x), max(0.0, y)
    x1, y1 = min(100.0, x + w), min(100.0, y + h)
    cw, ch = max(0.0, x1 - x0), max(0.0, y1 - y0)
    return cw * ch


def _iou(a: dict, b: dict) -> float:
    ax, ay, aw, ah = (float(a.get(k, 0.0)) for k in ("x", "y", "w", "h"))
    bx, by, bw, bh = (float(b.get(k, 0.0)) for k in ("x", "y", "w", "h"))
    ix0, iy0 = max(ax, bx), max(ay, by)
    ix1, iy1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def reconstruction_report(
    fp: FloorPlan,
    *,
    overlap_threshold: float = 0.2,
) -> dict:
    """
    Quantitative reconstruction health report (no AI).

    Metrics:
      - counts_per_type
      - totals per bucket
      - canvas_coverage_pct (rooms only, clamped; 100 = full canvas)
      - room_overlaps (pairs with IoU > threshold)
      - out_of_bounds: objects whose bbox extends past [0,100]
      - partial_objects: count of obj.partial True
      - low_confidence_count
      - corridor_stats: total segments + mean centerline points
    """
    fp_dict = fp.model_dump(by_alias=True)

    counts_per_type: dict[str, int] = {}
    bucket_totals: dict[str, int] = {}
    for bucket in _BUCKETS:
        items = fp_dict.get(bucket, []) or []
        bucket_totals[bucket] = len(items)
        for obj in items:
            t = obj.get("type", "unknown")
            counts_per_type[t] = counts_per_type.get(t, 0) + 1
    bucket_totals["corridors"] = len(fp_dict.get("corridors", []) or [])

    # Coverage (rooms bucket only — rooms are the dominant signal)
    room_area_sum = sum(_clamped_area(o["position"]) for o in fp_dict.get("rooms", []))
    canvas_coverage_pct = min(100.0, room_area_sum / 100.0)

    # Overlapping rooms (IoU)
    rooms = fp_dict.get("rooms", [])
    overlaps: list[dict] = []
    for i in range(len(rooms)):
        for j in range(i + 1, len(rooms)):
            iou = _iou(rooms[i]["position"], rooms[j]["position"])
            if iou > overlap_threshold:
                overlaps.append(
                    {
                        "a": rooms[i].get("id"),
                        "b": rooms[j].get("id"),
                        "iou": round(iou, 3),
                    }
                )

    # Out-of-bounds (after schema clamp this should be empty)
    oob: list[str] = []
    for bucket in _BUCKETS:
        for obj in fp_dict.get(bucket, []) or []:
            pos = obj.get("position", {})
            x, y, w, h = (float(pos.get(k, 0.0)) for k in ("x", "y", "w", "h"))
            if x < 0 or y < 0 or x + w > 100.001 or y + h > 100.001:
                oob.append(str(obj.get("id", "?")))

    partial_count = sum(
        1
        for bucket in _BUCKETS
        for obj in fp_dict.get(bucket, []) or []
        if obj.get("partial")
    )

    corridors = fp_dict.get("corridors", []) or []
    corridor_pts_mean = (
        sum(len(c.get("centerline", []) or []) for c in corridors) / len(corridors)
        if corridors
        else 0.0
    )

    return {
        "source_image": fp_dict.get("source_image"),
        "dimensions_px": fp_dict.get("dimensions_px"),
        "totals": bucket_totals,
        "counts_per_type": dict(sorted(counts_per_type.items())),
        "canvas_coverage_pct": round(canvas_coverage_pct, 2),
        "room_overlaps_over_threshold": overlaps,
        "overlap_threshold": overlap_threshold,
        "out_of_bounds_ids": oob,
        "partial_objects": partial_count,
        "low_confidence_count": len(fp_dict.get("low_confidence_flags", []) or []),
        "corridor_segments": len(corridors),
        "corridor_centerline_points_mean": round(corridor_pts_mean, 2),
    }


def reconstruct(
    json_path: Path,
    output_path: Path,
    *,
    image_path: Path | None = None,
    report_path: Path | None = None,
    show_nav_graph: bool = True,
    show_grid: bool = True,
    show_low_conf: bool = True,
    target_long_side: int = 900,
) -> dict:
    fp, warnings = load_and_validate(json_path)
    fp_dict = fp.model_dump(by_alias=True)

    render_kwargs = {
        "show_nav_graph": show_nav_graph,
        "show_grid": show_grid,
        "show_low_conf": show_low_conf,
        "target_long_side": target_long_side,
    }

    if image_path is not None:
        if not image_path.is_file():
            raise FileNotFoundError(f"Image not found: {image_path}")
        img = render_comparison(image_path, fp_dict, **render_kwargs)
    else:
        img = render_floor_plan(fp_dict, **render_kwargs)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, format="PNG")

    report = reconstruction_report(fp)
    report["output_image"] = str(output_path)
    report["input_image"] = str(image_path) if image_path else None
    report["mode"] = "comparison" if image_path else "render_only"
    report["image_size_px"] = [img.width, img.height]
    report["warnings"] = warnings

    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2))

    return report


def _print_summary(report: dict) -> None:
    print(f"Source image:     {report.get('source_image')}")
    print(f"Output PNG:       {report.get('output_image')}  "
          f"({report['image_size_px'][0]}×{report['image_size_px'][1]})")
    totals = report.get("totals", {})
    print("Objects rendered:")
    for bucket in ("rooms", "doors", "verticals", "emergency", "labels", "corridors"):
        n = totals.get(bucket, 0)
        if n:
            print(f"  {bucket:<12} {n}")
    print("Per-type counts:")
    for t, n in report.get("counts_per_type", {}).items():
        print(f"  {t:<20} {n}")
    print(f"Room coverage:    {report.get('canvas_coverage_pct')}%  "
          f"(of 100% canvas area)")
    overlaps = report.get("room_overlaps_over_threshold", [])
    if overlaps:
        print(f"Overlaps (IoU>{report['overlap_threshold']}): {len(overlaps)}")
        for o in overlaps[:5]:
            print(f"  {o['a']} vs {o['b']}: IoU={o['iou']}")
        if len(overlaps) > 5:
            print(f"  … and {len(overlaps) - 5} more")
    else:
        print(f"Overlaps (IoU>{report['overlap_threshold']}): 0")
    if report.get("out_of_bounds_ids"):
        print(f"Out-of-bounds:    {len(report['out_of_bounds_ids'])}  "
              f"(e.g. {report['out_of_bounds_ids'][:3]})")
    print(f"Partial objects:  {report.get('partial_objects', 0)}")
    print(f"Low-confidence:   {report.get('low_confidence_count', 0)}")
    if report.get("warnings"):
        print("Warnings:")
        for w in report["warnings"][:5]:
            print(f"  - {w}")
        if len(report["warnings"]) > 5:
            print(f"  … and {len(report['warnings']) - 5} more")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="Floor-plan reconstruction (no LLM)")
    ap.add_argument("--json", required=True, help="Parsed floor-plan JSON")
    ap.add_argument("--output", required=True, help="Output PNG path")
    ap.add_argument("--image", default=None,
                    help="Original floor-plan image for side-by-side comparison")
    ap.add_argument("--report", default=None, help="Optional JSON report path")
    ap.add_argument("--no-nav", action="store_true", help="Hide navigation graph")
    ap.add_argument("--no-grid", action="store_true", help="Hide grid lines")
    ap.add_argument("--no-flags", action="store_true", help="Hide low-confidence borders")
    ap.add_argument("--size", type=int, default=900, help="Render long-side px")
    args = ap.parse_args()

    try:
        report = reconstruct(
            json_path=Path(args.json),
            output_path=Path(args.output),
            image_path=Path(args.image) if args.image else None,
            report_path=Path(args.report) if args.report else None,
            show_nav_graph=not args.no_nav,
            show_grid=not args.no_grid,
            show_low_conf=not args.no_flags,
            target_long_side=args.size,
        )
    except FileNotFoundError as e:
        log.error("%s", e)
        return 1
    except ValidationError as e:
        log.error("Schema validation failed: %s", e)
        return 2

    _print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
