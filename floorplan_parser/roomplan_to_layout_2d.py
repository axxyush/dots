from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RoomplanLayoutConfig:
    padding_m: float = 1.5
    min_room_size_m: float = 2.0
    assume_entrance: bool = True


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def roomplan_json_to_layout_2d(
    roomplan: dict[str, Any],
    *,
    cfg: RoomplanLayoutConfig = RoomplanLayoutConfig(),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Convert Apple RoomPlan-style JSON (objects/walls/doors in meters) into the
    ConnectDots `layout_2d` format used by `connectdots_pdf.generate_tactile_pdf`.

    This path is deterministic (no LLM). If walls are missing (common), we
    synthesize a rectangular room around the objects.
    """
    objects_in = list((roomplan.get("objects") or []))
    meta = (roomplan.get("metadata") or {}) if isinstance(roomplan.get("metadata"), dict) else {}

    # Use metadata if present; otherwise compute from objects.
    room_w = _f(meta.get("roomWidthMeters"), 0.0)
    room_d = _f(meta.get("roomDepthMeters"), 0.0)

    # Bounding box of objects (in RoomPlan coordinates, meters).
    xs: list[float] = []
    zs: list[float] = []
    half_w: list[float] = []
    half_d: list[float] = []
    for o in objects_in:
        if not isinstance(o, dict):
            continue
        x = _f(o.get("positionX"))
        z = _f(o.get("positionZ"))
        w = _f(o.get("widthMeters"), _f(o.get("depthMeters"), 0.0))
        d = _f(o.get("depthMeters"), _f(o.get("widthMeters"), 0.0))
        xs.append(x)
        zs.append(z)
        half_w.append(max(0.0, w) / 2.0)
        half_d.append(max(0.0, d) / 2.0)

    if xs and zs:
        min_x = min(x - hw for x, hw in zip(xs, half_w)) - cfg.padding_m
        max_x = max(x + hw for x, hw in zip(xs, half_w)) + cfg.padding_m
        min_z = min(z - hd for z, hd in zip(zs, half_d)) - cfg.padding_m
        max_z = max(z + hd for z, hd in zip(zs, half_d)) + cfg.padding_m
    else:
        # No objects: fall back to a small square.
        min_x, max_x = 0.0, cfg.min_room_size_m
        min_z, max_z = 0.0, cfg.min_room_size_m

    inferred_w = max(max_x - min_x, cfg.min_room_size_m)
    inferred_d = max(max_z - min_z, cfg.min_room_size_m)

    # If metadata was empty/zero, use inferred.
    if room_w <= 0.0:
        room_w = inferred_w
    if room_d <= 0.0:
        room_d = inferred_d

    # Shift origin to min corner, so all coords are positive.
    def sx(x: float) -> float:
        return x - min_x

    def sz(z: float) -> float:
        return z - min_z

    # Synthesize 4 walls as segments. ConnectDots uses:
    # wall: {x,y,width,height,rotation_y}
    # where rotation_y = 0 => horizontal, pi/2 => vertical.
    cx = room_w / 2.0
    cy = room_d / 2.0
    walls = [
        {"x": cx, "y": 0.0, "width": room_w, "height": 2.5, "rotation_y": 0.0},  # top
        {"x": cx, "y": room_d, "width": room_w, "height": 2.5, "rotation_y": 0.0},  # bottom
        {"x": 0.0, "y": cy, "width": room_d, "height": 2.5, "rotation_y": 1.5708},  # left
        {"x": room_w, "y": cy, "width": room_d, "height": 2.5, "rotation_y": 1.5708},  # right
    ]

    objects: list[dict[str, Any]] = []
    for o in objects_in:
        if not isinstance(o, dict):
            continue
        idx = int(_f(o.get("index"), len(objects)))
        cat = str(o.get("category") or "object")
        x = sx(_f(o.get("positionX")))
        y = sz(_f(o.get("positionZ")))
        w = _f(o.get("widthMeters"), _f(o.get("depthMeters"), 0.3))
        d = _f(o.get("depthMeters"), _f(o.get("widthMeters"), 0.3))
        h = _f(o.get("heightMeters"), 1.0)
        objects.append(
            {
                "index": idx,
                "category": cat,
                "x": float(x),
                "y": float(y),
                "width": max(0.2, float(w)),
                "depth": max(0.2, float(d)),
                "height": max(0.2, float(h)),
                "confidence": str(o.get("confidence") or "Medium"),
            }
        )

    layout = {
        "source": "roomplan",
        "room_width": float(room_w),
        "room_depth": float(room_d),
        "walls": walls,
        "doors": [],
        "windows": [],
        "objects": objects,
        "entrance": {"kind": "unknown"} if cfg.assume_entrance else {},
    }

    metadata_out = {
        "room_name": "RoomPlan Scan",
        "building_name": "",
        "space_name": "",
    }
    return layout, metadata_out

