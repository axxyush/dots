from __future__ import annotations
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class Position(BaseModel):
    x: float = Field(ge=0, le=100)
    y: float = Field(ge=0, le=100)
    w: float = Field(ge=0, le=100)
    h: float = Field(ge=0, le=100)


class Point(BaseModel):
    x: float = Field(ge=0, le=100)
    y: float = Field(ge=0, le=100)


class DirectionArrow(BaseModel):
    from_: Point = Field(alias="from")
    to: Point

    model_config = {"populate_by_name": True}


class FloorObject(BaseModel):
    id: str
    type: Literal[
        "store", "restaurant", "restroom", "elevator", "stairs", "door",
        "corridor", "fire_exit", "fire_extinguisher", "fire_alarm", "rest_area",
        "office", "cafe", "service_counter", "label", "entrance",
        "bedroom", "bathroom", "living_room", "kitchen", "dining_room", "hallway",
        # institutional / school / public-building types
        "classroom", "laboratory", "library", "auditorium", "gym",
        "music_room", "art_room", "staff_room", "reading_room",
        "computer_lab", "courtyard", "multimedia_room", "general_office",
        "utility", "lobby", "reception", "unknown",
    ]
    label: Optional[str] = None
    position: Position
    partial: bool = False
    confidence: Literal["high", "medium", "low"] = "medium"
    door_type: Optional[Literal["single", "double", "emergency", "main_entrance"]] = None
    door_swing: Optional[Literal["inward", "outward", "unknown"]] = None
    accessible: Optional[bool] = True
    width_m: Optional[float] = None
    notes: Optional[str] = None
    seen_in_tiles: int = 1
    source: str = "tile"

    @field_validator("position", mode="before")
    @classmethod
    def clamp_position(cls, v: Any) -> Any:
        if isinstance(v, dict):
            import logging
            for k in ("x", "y", "w", "h"):
                if k in v and isinstance(v[k], (int, float)):
                    clamped = max(0.0, min(100.0, float(v[k])))
                    if clamped != v[k]:
                        logging.warning("Clamped position.%s from %s to %s", k, v[k], clamped)
                    v[k] = clamped
        return v


class Corridor(BaseModel):
    id: str
    type: Literal["primary_corridor", "secondary_corridor"] = "primary_corridor"
    centerline: list[Point]
    width_m: Optional[float] = None
    accessible: Optional[bool] = True
    direction_arrows: list[DirectionArrow] = Field(default_factory=list)
    seen_in_tiles: int = 1
    source: str = "tile"


class ScaleDetected(BaseModel):
    px_per_meter: Optional[float] = None
    scale_bar_found: bool = False


class TileResponse(BaseModel):
    tile_id: str
    objects: list[FloorObject] = Field(default_factory=list)
    corridors: list[Corridor] = Field(default_factory=list)
    scale_detected: ScaleDetected = Field(default_factory=ScaleDetected)


# ── Final merged schema ──────────────────────────────────────────────────────

class ParseMetadata(BaseModel):
    tile_grid: str
    overlap_pct: float
    tiles_parsed: int
    total_objects_before_dedup: int
    total_objects_after_dedup: int


class NavNode(BaseModel):
    id: str
    position: Point
    type: Literal["corridor_junction", "room_entrance", "elevator", "stairs", "exit"]
    accessible: Optional[bool] = True


class NavEdge(BaseModel):
    id: str
    from_node: str
    to_node: str
    width_m: Optional[float] = None
    accessible: bool = True
    corridor_id: Optional[str] = None


class NavigationGraph(BaseModel):
    nodes: list[NavNode] = Field(default_factory=list)
    edges: list[NavEdge] = Field(default_factory=list)


class DimensionsPx(BaseModel):
    width: int
    height: int


class FloorPlan(BaseModel):
    id: str = "floor_1"
    source_image: str
    dimensions_px: DimensionsPx
    coordinate_system: str = "normalized_0_to_100"
    parse_metadata: ParseMetadata
    rooms: list[FloorObject] = Field(default_factory=list)
    corridors: list[Corridor] = Field(default_factory=list)
    doors: list[FloorObject] = Field(default_factory=list)
    verticals: list[FloorObject] = Field(default_factory=list)
    emergency: list[FloorObject] = Field(default_factory=list)
    labels: list[FloorObject] = Field(default_factory=list)
    low_confidence_flags: list[FloorObject] = Field(default_factory=list)
    navigation_graph: NavigationGraph = Field(default_factory=NavigationGraph)


class FloorPlanResult(BaseModel):
    floor_plan: FloorPlan
