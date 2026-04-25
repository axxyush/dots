from __future__ import annotations
import logging
from typing import Any

from pydantic import ValidationError

from schema import Corridor, FloorObject, TileResponse

log = logging.getLogger(__name__)

_ROOM_TYPES = frozenset({
    "store", "restaurant", "restroom", "office", "cafe", "rest_area", "service_counter", "entrance",
    "bedroom", "bathroom", "living_room", "kitchen", "dining_room", "hallway",
    "classroom", "laboratory", "library", "auditorium", "gym",
    "music_room", "art_room", "staff_room", "reading_room",
    "computer_lab", "courtyard", "multimedia_room", "general_office",
    "utility", "lobby", "reception", "unknown",
})
_DOOR_TYPES = frozenset({"door"})
_VERTICAL_TYPES = frozenset({"elevator", "stairs"})
_EMERGENCY_TYPES = frozenset({"fire_exit", "fire_extinguisher", "fire_alarm"})
_LABEL_TYPES = frozenset({"label"})
_CORRIDOR_TYPES = frozenset({"corridor"})


def parse_tile_response(raw: dict, tile_id: str) -> TileResponse:
    """
    Validate a raw Gemini dict against TileResponse schema.
    On partial failure, log field errors and return what's valid.
    """
    # ensure tile_id is set
    raw.setdefault("tile_id", tile_id)

    valid_objects: list[FloorObject] = []
    for i, obj in enumerate(raw.get("objects", [])):
        try:
            valid_objects.append(FloorObject.model_validate(obj))
        except ValidationError as exc:
            log.warning("Tile %s object[%d] validation errors: %s", tile_id, i, exc)
            # try field-by-field salvage
            salvaged = _salvage_object(obj, tile_id, i)
            if salvaged:
                valid_objects.append(salvaged)

    valid_corridors: list[Corridor] = []
    for i, corr in enumerate(raw.get("corridors", [])):
        try:
            valid_corridors.append(Corridor.model_validate(corr))
        except ValidationError as exc:
            log.warning("Tile %s corridor[%d] validation errors: %s", tile_id, i, exc)

    scale_raw = raw.get("scale_detected", {})
    try:
        from schema import ScaleDetected
        scale = ScaleDetected.model_validate(scale_raw)
    except Exception:
        from schema import ScaleDetected
        scale = ScaleDetected()

    return TileResponse(
        tile_id=tile_id,
        objects=valid_objects,
        corridors=valid_corridors,
        scale_detected=scale,
    )


def _salvage_object(obj: dict, tile_id: str, idx: int) -> FloorObject | None:
    """Best-effort field salvage: clamp coords, fix confidence, then try again."""
    if not isinstance(obj, dict):
        return None

    # clamp position fields
    if "position" in obj and isinstance(obj["position"], dict):
        for k in ("x", "y", "w", "h"):
            v = obj["position"].get(k)
            if isinstance(v, (int, float)):
                obj["position"][k] = max(0.0, min(100.0, float(v)))
            else:
                obj["position"][k] = 0.0

    # fix bad confidence
    if obj.get("confidence") not in ("high", "medium", "low"):
        obj["confidence"] = "low"

    # fix bad type
    from schema import FloorObject as _FO
    valid_types = _FO.model_fields["type"].annotation.__args__
    if obj.get("type") not in valid_types:
        obj["type"] = "label"

    # null out invalid enum fields
    valid_door_types = {"single", "double", "emergency", "main_entrance", None}
    if obj.get("door_type") not in valid_door_types:
        obj["door_type"] = None
    valid_door_swings = {"inward", "outward", "unknown", None}
    if obj.get("door_swing") not in valid_door_swings:
        obj["door_swing"] = None

    # ensure id
    obj.setdefault("id", f"{tile_id}_salvaged_{idx}")

    try:
        return FloorObject.model_validate(obj)
    except Exception as exc:
        log.error("Tile %s: could not salvage object[%d]: %s", tile_id, idx, exc)
        return None


def categorize(objects: list[FloorObject]) -> dict[str, list[FloorObject]]:
    """Split objects into typed buckets for the final schema."""
    buckets: dict[str, list[FloorObject]] = {
        "rooms": [], "doors": [], "verticals": [], "emergency": [], "labels": [],
    }
    for obj in objects:
        if obj.type in _ROOM_TYPES:
            buckets["rooms"].append(obj)
        elif obj.type in _DOOR_TYPES:
            buckets["doors"].append(obj)
        elif obj.type in _VERTICAL_TYPES:
            buckets["verticals"].append(obj)
        elif obj.type in _EMERGENCY_TYPES:
            buckets["emergency"].append(obj)
        elif obj.type in _LABEL_TYPES:
            buckets["labels"].append(obj)
        # corridor type objects handled separately via Corridor schema
    return buckets


def collect_low_confidence(
    objects: list[FloorObject],
) -> list[FloorObject]:
    return [o for o in objects if o.seen_in_tiles == 1 and o.confidence != "high"]
