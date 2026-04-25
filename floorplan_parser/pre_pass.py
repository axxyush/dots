"""Fix 1 + Fix 2: structural skeleton pass + landmark detection on the full image."""
from __future__ import annotations
import io
import logging
import math
from PIL import Image

import gemini_client as gc
from schema import Point, TileResponse

log = logging.getLogger(__name__)

_SKELETON_PROMPT = """You are analyzing a floor plan image to extract its structural skeleton.

TASK — return ONLY:
1. Corridors / hallways: their centerlines and widths
2. The overall building boundary (usually 0,0 to 100,100 but may be smaller if there is whitespace)
3. ONE primary navigation landmark that will serve as a coordinate calibration anchor.
   Choose the most visually unambiguous element: main entrance, elevator bank, or prominent staircase.

DO NOT identify individual rooms, stores, labels, or furniture.

COORDINATE SYSTEM: (0,0) = top-left of the image, (100,100) = bottom-right. All values 0–100.

Return ONLY valid JSON — no prose, no markdown fences:
{
  "building_boundary": {"x": 0.0, "y": 0.0, "w": 100.0, "h": 100.0},
  "corridors": [
    {
      "id": "corr_0",
      "type": "primary_corridor|secondary_corridor",
      "centerline": [{"x": 18.0, "y": 0.0}, {"x": 18.0, "y": 100.0}],
      "width_pct": 5.0,
      "accessible": true
    }
  ],
  "landmark": {
    "type": "entrance|elevator|stairs|restroom",
    "description": "short unique description e.g. main entrance south wall",
    "position": {"x": 50.0, "y": 95.0}
  }
}"""


async def structural_pass(client, model_name: str, img: Image.Image, target_px: int = 1024) -> dict:
    """Downscale full image and extract corridor skeleton + landmark."""
    w, h = img.size
    scale = min(1.0, target_px / max(w, h))
    small = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    buf = io.BytesIO()
    small.save(buf, format="PNG")

    raw = await gc.call_gemini(client, model_name, _SKELETON_PROMPT, buf.getvalue(), "structural_pass")

    if raw.get("_parse_error"):
        log.warning("Structural pass failed: %s — no skeleton available.", raw["_parse_error"])
        return {"corridors": [], "landmark": None, "building_boundary": {"x": 0, "y": 0, "w": 100, "h": 100}}

    n_corr = len(raw.get("corridors", []))
    lm = raw.get("landmark")
    log.info("Structural pass: %d corridor(s) detected, landmark: %s", n_corr,
             lm.get("description") if lm else "none")
    return raw


def format_skeleton_for_prompt(skeleton: dict) -> str:
    """Render skeleton as a concise text block for injection into tile prompts."""
    lines = ["CORRIDOR SKELETON (detected from full image — your room coordinates MUST NOT overlap these):"]
    for c in skeleton.get("corridors", []):
        pts = " → ".join(f"({p['x']:.1f},{p['y']:.1f})" for p in c.get("centerline", []))
        w = c.get("width_pct", "?")
        lines.append(f"  [{c['id']}] {c['type']}: {pts}, width={w}%")
    if not skeleton.get("corridors"):
        lines.append("  (none detected)")
    return "\n".join(lines)


def format_landmark_for_prompt(skeleton: dict) -> str:
    """Render landmark as a calibration line for tile prompts."""
    lm = skeleton.get("landmark")
    if not lm:
        return ""
    pos = lm.get("position", {})
    return (
        f"CALIBRATION LANDMARK: {lm.get('description', lm.get('type', 'landmark'))} "
        f"is at global position x={pos.get('x', '?'):.1f}, y={pos.get('y', '?'):.1f}. "
        f"If this landmark appears in your tile, verify your coordinates match this position."
    )


def apply_landmark_correction(
    response: TileResponse,
    tile_global_range: tuple[float, float, float, float],
    landmark: dict | None,
) -> TileResponse:
    """
    Fix 2: if the landmark falls inside this tile's global range, find it in the
    response and compute a translation correction for all objects in the tile.
    """
    if not landmark:
        return response

    lpos = landmark.get("position", {})
    lx, ly = lpos.get("x"), lpos.get("y")
    ltype = landmark.get("type")

    if lx is None or ly is None:
        return response

    x0, y0, x1, y1 = tile_global_range
    if not (x0 <= lx <= x1 and y0 <= ly <= y1):
        return response  # landmark not in this tile

    # find closest matching object
    best_dist = float("inf")
    best_cx = best_cy = None
    for obj in response.objects:
        if ltype and obj.type != ltype:
            continue
        cx = obj.position.x + obj.position.w / 2
        cy = obj.position.y + obj.position.h / 2
        d = math.hypot(cx - lx, cy - ly)
        if d < best_dist:
            best_dist = d
            best_cx, best_cy = cx, cy

    if best_cx is None or best_dist < 3:
        return response  # no match or already accurate

    dx = lx - best_cx
    dy = ly - best_cy

    if abs(dx) < 2 and abs(dy) < 2:
        return response  # negligible

    log.info("Tile landmark correction: dx=%.1f dy=%.1f (landmark %s)", dx, dy, ltype)

    from schema import Position, TileResponse as TR

    def shift_pos(p):
        return Position(
            x=max(0.0, min(100.0, p.x + dx)),
            y=max(0.0, min(100.0, p.y + dy)),
            w=p.w, h=p.h,
        )

    corrected_objs = [obj.model_copy(update={"position": shift_pos(obj.position)}) for obj in response.objects]
    corrected_corrs = [
        corr.model_copy(update={
            "centerline": [Point(x=max(0.0, min(100.0, p.x + dx)), y=max(0.0, min(100.0, p.y + dy)))
                           for p in corr.centerline]
        })
        for corr in response.corridors
    ]
    return TR(tile_id=response.tile_id, objects=corrected_objs, corridors=corrected_corrs,
              scale_detected=response.scale_detected)
