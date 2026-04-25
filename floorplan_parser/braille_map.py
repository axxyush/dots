#!/usr/bin/env python3
"""
Generate a tactile BrailleMap-style PDF from a parsed floor-plan JSON.

This *replaces* the earlier Braille-unicode grid renderer. It now uses the
ConnectDots rendering approach (dot-based tactile PDF + legend page).

Input:  result.json produced by the floorplan parser (schema.FloorPlanResult)
Output: a PDF (page 1 tactile map, page 2 legend with Braille text)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Any

from reconstruct import load_and_validate
from connectdots_pdf import generate_tactile_pdf

log = logging.getLogger("braille_map")


@dataclass(frozen=True)
class GridSpec:
    cols: int
    rows: int


def generate_braille_map(
    json_path: Path,
    *,
    out_pdf: Path,
    meters_width: float = 12.0,
) -> dict:
    fp, warnings = load_and_validate(json_path)
    fp_dict: dict[str, Any] = fp.model_dump(by_alias=True)

    layout, metadata = floorplan_to_layout_2d(fp_dict, meters_width=meters_width)
    pdf_path = generate_tactile_pdf(
        output_pdf_path=str(out_pdf),
        layout=layout,
        metadata=metadata,
        room_id=fp_dict.get("id", "floor_1"),
    )

    return {
        "pdf_path": str(pdf_path),
        "warnings": warnings,
        "layout_2d": layout,
        "metadata": metadata,
    }


def _pos_to_meters(pos: dict, *, room_w_m: float, room_d_m: float) -> tuple[float, float, float, float]:
    x = float(pos.get("x", 0.0)) / 100.0 * room_w_m
    y = float(pos.get("y", 0.0)) / 100.0 * room_d_m
    w = float(pos.get("w", 0.0)) / 100.0 * room_w_m
    h = float(pos.get("h", 0.0)) / 100.0 * room_d_m
    return x, y, w, h


def floorplan_to_layout_2d(fp: dict[str, Any], *, meters_width: float = 12.0) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Convert this repo's `floor_plan` dict into the ConnectDots `layout_2d` format.

    Our schema is rectangle-based. ConnectDots expects wall segments + objects.
    We approximate each room rectangle as 4 wall segments and render rooms as
    numbered objects. This yields a tactile map + a detailed legend page.
    """
    dims = fp.get("dimensions_px", {}) or {}
    src_w = float(dims.get("width", 1000) or 1000)
    src_h = float(dims.get("height", 1000) or 1000)
    aspect = src_h / src_w if src_w > 0 else 1.0
    room_w_m = float(meters_width)
    room_d_m = max(1.0, room_w_m * aspect)

    walls: list[dict[str, Any]] = []
    doors: list[dict[str, Any]] = []
    windows: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []

    # Rooms as objects + walls (4 edges)
    for i, r in enumerate(fp.get("rooms", []) or []):
        pos = r.get("position") or {}
        x, y, w, h = _pos_to_meters(pos, room_w_m=room_w_m, room_d_m=room_d_m)
        cx = x + w / 2.0
        cy = y + h / 2.0
        label = (r.get("label") or "").strip()
        category = label if label else str(r.get("type", "unknown")).replace("_", " ")
        objects.append(
            {
                "index": i,
                "category": category,
                "x": cx,
                "y": cy,
                "width": max(0.2, w),
                "depth": max(0.2, h),
                "height": 1.0,
                "confidence": str(r.get("confidence", "medium")).title(),
            }
        )

        # Wall segments: top, bottom (rotation 0), left, right (rotation pi/2)
        # Coordinates in meters, origin at top-left.
        # Top
        walls.append({"x": cx, "y": y, "width": w, "height": 2.5, "rotation_y": 0.0})
        # Bottom
        walls.append({"x": cx, "y": y + h, "width": w, "height": 2.5, "rotation_y": 0.0})
        # Left
        walls.append({"x": x, "y": cy, "width": h, "height": 2.5, "rotation_y": 1.5708})
        # Right
        walls.append({"x": x + w, "y": cy, "width": h, "height": 2.5, "rotation_y": 1.5708})

    # Doors from schema, if present
    for j, d in enumerate(fp.get("doors", []) or []):
        pos = d.get("position") or {}
        x, y, w, h = _pos_to_meters(pos, room_w_m=room_w_m, room_d_m=room_d_m)
        cx = x + w / 2.0
        cy = y + h / 2.0
        is_entrance = d.get("door_type") in {"main_entrance"} or (d.get("type") == "entrance")
        doors.append(
            {
                "index": j,
                "category": "entrance" if is_entrance else "door",
                "x": cx,
                "y": cy,
                "width": max(0.6, max(w, h)),
                "rotation_y": 0.0,
                "parent_wall_index": None,
                "is_entrance": bool(is_entrance),
            }
        )

    layout = {
        "room_width": room_w_m,
        "room_depth": room_d_m,
        "walls": walls,
        "doors": doors,
        "windows": windows,
        "objects": objects,
        "entrance": {},
    }
    metadata = {
        "room_name": fp.get("source_image", "Floorplan"),
        "building_name": "SenseGrid",
        "space_name": fp.get("id", "floor_1"),
    }
    return layout, metadata


def main(argv: Optional[Iterable[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True, help="Path to result.json")
    ap.add_argument("--out-pdf", required=True, help="Output tactile PDF path")
    ap.add_argument("--meters-width", type=float, default=12.0, help="Assumed total width in meters")
    args = ap.parse_args(list(argv) if argv is not None else None)

    res = generate_braille_map(
        Path(args.json),
        out_pdf=Path(args.out_pdf),
        meters_width=float(args.meters_width),
    )
    log.info("tactile PDF written → %s", res["pdf_path"])
    if res.get("warnings"):
        log.warning("warnings: %s", res["warnings"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

