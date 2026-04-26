"""Shared agent-to-agent protocol between SenseGrid (scene store) and Wayfind
(blind-user navigation chat).

SenseGrid persists every parsed venue keyed by a short ``venue_id``. The
Wayfind agent looks up that venue once per chat session and does all
reasoning locally with the cached scene.

Coordinate system delivered to Wayfind
--------------------------------------
Everything is in **meters**, origin at the room's bottom-left, +x → right,
+y → forward (away from the entrance). The entrance is at
``(entrance_x_m, entrance_y_m)`` with the user initially facing
``entrance_heading_deg`` (0° = +x, 90° = +y, measured CCW).

We derive metric scale from the business owner's reported "longest wall in
meters" (default 15.0). The shorter wall is computed from the floorplan
image aspect ratio.
"""

from __future__ import annotations

from uagents import Model

PROTOCOL_NAME = "wayfind-venue"
PROTOCOL_VERSION = "0.1.0"


class VenueLookup(Model):
    """Request: load a venue by id."""

    venue_id: str


class VenueInfo(Model):
    """Response: structured scene + metric frame for navigation reasoning.

    On miss/error, ``found`` is False and ``error`` describes why; all other
    fields will be empty/zero.
    """

    found: bool
    error: str = ""
    venue_id: str = ""
    venue_label: str = ""

    # Full floor_plan dict, JSON-encoded. Caller json.loads() it.
    scene_json: str = ""

    # Metric frame (meters, origin = room bottom-left).
    room_width_m: float = 0.0
    room_height_m: float = 0.0
    entrance_x_m: float = 0.0
    entrance_y_m: float = 0.0
    entrance_heading_deg: float = 90.0  # facing +y (into the room)
