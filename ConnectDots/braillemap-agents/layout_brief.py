"""Pure-Python helpers for turning a `layout_2d` dict into entrance-relative
spatial wording. Imported by `agent_narration.py` and `voice_session.py` —
keep this file dependency-free so the FastAPI backend can import it without
pulling in google-genai / uagents / elevenlabs.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple


def orient_relative_to_entrance(
    layout: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Shift coords so the entrance is (0, 0) and 'forward' (+y) is into the room.

    Picks orientation by checking which room edge the entrance is closest to and
    flipping the forward axis so positive y always points deeper into the space.
    """
    room_w = float(layout.get("room_width") or 0.0)
    room_d = float(layout.get("room_depth") or 0.0)
    entrance = layout.get("entrance") or {}
    ex = float(entrance.get("x", room_w / 2))
    ey = float(entrance.get("y", 0.0))

    to_top = abs(room_d - ey)
    to_bottom = abs(ey)
    to_left = abs(ex)
    to_right = abs(room_w - ex)
    nearest = min(to_top, to_bottom, to_left, to_right)

    def rel(px: float, py: float) -> Tuple[float, float]:
        dx = px - ex
        dy = py - ey
        if nearest == to_bottom:
            return dx, dy
        if nearest == to_top:
            return -dx, -dy
        if nearest == to_left:
            return dy, dx
        return -dy, -dx  # to_right

    relative_entrance = {
        "x": 0.0,
        "y": 0.0,
        "width": float(entrance.get("width", 0.8)),
        "kind": entrance.get("kind", "door"),
    }
    relative_objects: List[Dict[str, Any]] = []
    for obj in layout.get("objects") or []:
        fx, fy = rel(float(obj["x"]), float(obj["y"]))
        relative_objects.append({
            "index": int(obj["index"]),
            "category": obj["category"],
            "forward_m": round(fy, 2),
            "side_m": round(fx, 2),  # negative = left, positive = right
            "width_m": round(float(obj["width"]), 2),
            "depth_m": round(float(obj["depth"]), 2),
        })
    return relative_entrance, relative_objects


def format_object_lines(rel_objects: List[Dict[str, Any]]) -> str:
    """Render entrance-relative objects as the bullet list used in prompts."""
    if not rel_objects:
        return "- (no objects detected)"
    return "\n".join(
        f"- {o['category']}: {o['forward_m']:+.1f} m forward, "
        f"{abs(o['side_m']):.1f} m to the "
        f"{'right' if o['side_m'] > 0 else 'left' if o['side_m'] < 0 else 'center'}, "
        f"size {o['width_m']:.1f} × {o['depth_m']:.1f} m"
        for o in rel_objects
    )


def resolve_space_label(layout: Dict[str, Any], metadata: Dict[str, Any]) -> str:
    """Pick the most meaningful label for the space."""
    space_name = (layout.get("space_name") or "").strip()
    space_type = (layout.get("space_type") or "").strip()
    return space_name or space_type or metadata.get("room_name") or "space"
