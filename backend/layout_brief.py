from __future__ import annotations

from typing import Any


def orient_relative_to_entrance(
    layout: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Shift coords so entrance is (0,0) and +forward is into the space."""
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

    def rel(px: float, py: float) -> tuple[float, float]:
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

    relative_objects: list[dict[str, Any]] = []
    for obj in layout.get("objects") or []:
        fx, fy = rel(float(obj["x"]), float(obj["y"]))
        relative_objects.append(
            {
                "index": int(obj.get("index", 0)),
                "category": str(obj.get("category") or "object"),
                "forward_m": round(fy, 2),
                "side_m": round(fx, 2),  # negative=left, positive=right
                "width_m": round(float(obj.get("width") or 0.0), 2),
                "depth_m": round(float(obj.get("depth") or 0.0), 2),
            }
        )

    return relative_entrance, relative_objects


def format_object_lines(rel_objects: list[dict[str, Any]]) -> str:
    if not rel_objects:
        return "- (no objects detected)"
    return "\n".join(
        f"- {o['category']}: {o['forward_m']:+.1f} m forward, "
        f"{abs(o['side_m']):.1f} m to the "
        f"{'right' if o['side_m'] > 0 else 'left' if o['side_m'] < 0 else 'center'}, "
        f"size {o['width_m']:.1f} × {o['depth_m']:.1f} m"
        for o in rel_objects
    )


def build_system_prompt(layout: dict[str, Any], metadata: dict[str, Any]) -> str:
    _, rel_objects = orient_relative_to_entrance(layout)
    room_w = float(layout.get("room_width") or 0.0)
    room_d = float(layout.get("room_depth") or 0.0)
    label = (metadata.get("room_name") or metadata.get("space_name") or "space").strip()
    brief = format_object_lines(rel_objects)
    return (
        "You are an accessibility guide for a blind user holding a tactile map.\n"
        f"Map: {label}\n"
        "Frame of reference: user is at the main entrance, facing into the space.\n"
        "\"Forward\" means deeper into the space; left/right are sideways from that heading.\n"
        f"Overall extent: {room_w:.1f} m wide by {room_d:.1f} m deep.\n\n"
        "Spatial brief (objects relative to entrance):\n"
        f"{brief}\n\n"
        "Rules:\n"
        "- Answer using natural directions + approximate meters.\n"
        "- Do not invent rooms/objects not in the brief.\n"
        "- If asked about stairs/doors/walls but they are missing, say they were not detected.\n"
        "- Keep replies under 3 short sentences unless asked for a full description.\n"
    )


def build_system_prompt_from_context(context_text: str, metadata: dict[str, Any]) -> str:
    label = (metadata.get("room_name") or metadata.get("space_name") or "space").strip()
    return (
        "You are an accessibility guide for a blind user holding a tactile map.\n"
        f"Map: {label}\n\n"
        "You are given a structured description of the map below. Use ONLY this information.\n"
        "If asked about something not present (e.g., stairs) and the context doesn't mention it, say it is not detected / unknown.\n"
        "Answer with natural directions and counts; keep replies short.\n\n"
        f"MAP CONTEXT:\n{context_text.strip()}\n"
    )

