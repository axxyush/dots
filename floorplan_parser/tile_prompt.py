from __future__ import annotations
import json
from tiler import TileSpec, SeamSpec, tile_neighbor_info

_ELEMENT_LIST = (
    "rooms, corridors, doors (with swing direction), elevators (marked with X symbol), "
    "stairs (hatched pattern), restrooms, fire exits, fire extinguishers, fire alarms, "
    "rest areas, service counters, directional arrows and labels, cafes, offices, stores, "
    "restaurants, entrances, bedrooms, bathrooms (private), living rooms, kitchens, "
    "dining rooms, hallways, any visible text labels"
)

_OBJECT_TYPES = (
    "store | restaurant | restroom | elevator | stairs | door | corridor | "
    "fire_exit | fire_extinguisher | fire_alarm | rest_area | office | cafe | "
    "service_counter | label | entrance | "
    "bedroom | bathroom | living_room | kitchen | dining_room | hallway"
)

_SCHEMA = {
    "tile_id": "<this tile's id>",
    "objects": [
        {
            "id": "unique_string",
            "type": _OBJECT_TYPES,
            "label": "text visible or null",
            "position": {"x": 0, "y": 0, "w": 10, "h": 8},
            "partial": False,
            "confidence": "high|medium|low",
            "door_type": "single|double|emergency|main_entrance or null",
            "door_swing": "inward|outward|unknown or null",
            "accessible": True,
            "width_m": None,
            "notes": "any unusual detail",
        }
    ],
    "corridors": [
        {
            "id": "corridor_unique_id",
            "type": "primary_corridor|secondary_corridor",
            "centerline": [{"x": 0, "y": 0}, {"x": 100, "y": 0}],
            "width_m": 3.5,
            "accessible": True,
            "direction_arrows": [{"from": {"x": 0, "y": 0}, "to": {"x": 100, "y": 0}}],
        }
    ],
    "scale_detected": {"px_per_meter": None, "scale_bar_found": False},
}


def build_tile_prompt(
    spec: TileSpec,
    all_specs: list[TileSpec],
    skeleton_context: str = "",
    landmark_context: str = "",
) -> str:
    x0, y0, x1, y1 = spec.global_range()
    neighbors = tile_neighbor_info(spec, all_specs)
    neighbor_str = ", ".join(f"{d}={tid}" for d, tid in neighbors.items() if tid) or "none (corner/edge tile)"
    schema_str = json.dumps(_SCHEMA, indent=2)

    skeleton_block = f"\nREFERENCE SKELETON (global coords, for context only — do NOT copy into output):\n{skeleton_context}\n" if skeleton_context else ""
    landmark_block = f"\n{landmark_context}\n" if landmark_context else ""
    corridor_constraint = (
        "\nCONSTRAINT: Rooms MUST NOT overlap any corridor listed in the skeleton above. "
        "Shrink the room bbox to the corridor edge if needed.\n"
        if skeleton_context else ""
    )

    return f"""You are parsing tile {spec.tile_id} of a floor plan image.

═══ COORDINATE SYSTEM ═══
This tile covers global x={x0:.1f}%–{x1:.1f}%, y={y0:.1f}%–{y1:.1f}% of the full floor plan.
Report ALL positions in TILE-LOCAL space: (0,0) = top-left of THIS tile, (100,100) = bottom-right of THIS tile.
DO NOT convert to global coordinates — that transformation is applied automatically in post-processing.
"position" is: x,y = top-left corner of the object, w,h = width and height (all in tile-local 0–100).

Neighboring tiles: {neighbor_str}
If an object is cut off at a tile edge, set "partial": true.
{skeleton_block}{landmark_block}{corridor_constraint}
═══ CRITICAL — COUNT EVERY INSTANCE (but do NOT double-count across tiles) ═══
Floor plans often have rows or grids of identical-looking rooms and stores.
You MUST report EACH ONE as a separate object — do NOT merge or skip duplicates.
If you see 12 stores in a row, report 12 separate objects with distinct positions.
Physically count every room outline on the plan. Missing rooms is a critical error.

OWNERSHIP RULE (prevents overcounting across overlapping tiles):
Only report an object if its VISUAL CENTER (the centroid of its outline) lies inside
THIS tile. If an object is mostly inside a neighboring tile and you can only see a
sliver of it at the edge, DO NOT report it — the neighboring tile will report it.
Set "partial": true only for objects whose center IS inside this tile but which
extend beyond a tile boundary.

═══ ELEMENTS TO DETECT ═══
{_ELEMENT_LIST}

═══ RESIDENTIAL vs COMMERCIAL ═══
Use "bedroom" for any room containing a bed or labeled as bedroom.
Use "bathroom" for private bathrooms attached to bedrooms (toilet/sink/shower within a unit).
Use "restroom" only for shared/public restrooms (labeled WC, restroom, toilet, etc.).
Use "dining_room" for a room with a dining table (not a restaurant).
Use "kitchen" for a room with cooking appliances.
Use "living_room" for a sitting/lounge area that is not labeled as rest_area.
Use "hallway" for a narrow connecting passage in a residential building (not a main corridor).

═══ NESTED ROOMS (CRITICAL) ═══
Rooms commonly CONTAIN other rooms. An en-suite bathroom inside a hotel/dorm
bedroom is NOT adjacent to the bedroom — it is INSIDE it. You must report
these as nested bboxes, not side-by-side bboxes.

Rules:
  1. The outer (parent) room's bbox MUST enclose the full unit, INCLUDING the
     area occupied by any rooms nested inside it. Do NOT carve the parent bbox
     around the child.
  2. The inner (child) room's bbox MUST lie entirely inside the parent's bbox.
  3. Typical nestings to watch for:
       • en-suite bathroom inside a bedroom  → report both; bathroom bbox ⊂ bedroom bbox
       • closet / wardrobe inside a bedroom  → report bedroom only unless clearly labeled
       • kitchen inside an open-plan living_room → report both; kitchen bbox ⊂ living_room bbox
  4. Do NOT split one bedroom unit into "bedroom + bathroom" side-by-side
     rectangles. That is wrong. The bedroom covers the WHOLE unit; the
     bathroom is a smaller rectangle sitting inside it.

═══ SCALE ═══
If you can detect a scale bar or dimension label, extract px_per_meter. Otherwise leave null.

═══ OUTPUT FORMAT ═══
Return ONLY valid JSON matching this exact schema (no prose, no markdown, no extra keys):
{schema_str}

Use tile_id: "{spec.tile_id}"
IDs must be globally unique — prefix every object id with "{spec.tile_id}_obj_" and index from 0.
Prefix every corridor id with "{spec.tile_id}_corr_" and index from 0.
Return ONLY valid JSON."""


def build_seam_prompt(spec: SeamSpec) -> str:
    x0, y0, x1, y1 = spec.global_range()
    return f"""This is a seam strip between two adjacent tiles of a floor plan image.
The strip covers global x={x0:.1f}%–{x1:.1f}%, y={y0:.1f}%–{y1:.1f}%.
Orientation: {spec.orientation} seam ({spec.seam_id}).

Report ALL positions in TILE-LOCAL space: (0,0) = top-left of THIS strip, (100,100) = bottom-right.
DO NOT convert to global — post-processing handles that.

List ONLY objects that appear cut off or partially visible at this boundary.

Return ONLY valid JSON:
{{
  "tile_id": "{spec.seam_id}",
  "objects": [
    {{
      "id": "{spec.seam_id}_obj_0",
      "type": "store|restaurant|restroom|elevator|stairs|door|corridor|fire_exit|fire_extinguisher|fire_alarm|rest_area|office|cafe|service_counter|label|entrance",
      "label": null,
      "position": {{"x": 0, "y": 0, "w": 20, "h": 50}},
      "partial": true,
      "confidence": "medium",
      "door_type": null,
      "door_swing": null,
      "accessible": true,
      "width_m": null,
      "notes": null
    }}
  ],
  "corridors": [],
  "scale_detected": {{"px_per_meter": null, "scale_bar_found": false}}
}}"""
