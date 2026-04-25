"""Agent 1 — Spatial Processor.

Entry point to the BrailleMap pipeline. Converts 3D RoomPlan scan data into
a normalized 2D floor layout, writes it back to the backend, and triggers
the Object Enricher (Agent 2).

Implements the Fetch.ai Chat Protocol so it is discoverable via ASI:One —
a natural-language message like "process room <id>" kicks off the pipeline.
"""

from __future__ import annotations

import math
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import requests
from dotenv import load_dotenv
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

from schemas import EnrichmentRequest, SpatialProcessingRequest, address_from_seed

load_dotenv()

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
AGENT_SEED_1 = os.getenv("AGENT_SEED_1")
AGENT_SEED_2 = os.getenv("AGENT_SEED_2")
AGENT_PORT_1 = int(os.getenv("AGENT_PORT_1", "8001"))

if not AGENT_SEED_1 or not AGENT_SEED_2:
    raise SystemExit("Set AGENT_SEED_1 and AGENT_SEED_2 in .env")

ENRICHER_ADDRESS = address_from_seed(AGENT_SEED_2)

spatial_agent = Agent(
    name="braillemap_spatial_processor",
    seed=AGENT_SEED_1,
    port=AGENT_PORT_1,
    endpoint=[f"http://localhost:{AGENT_PORT_1}/submit"],
)


# ── 3D → 2D projection ───────────────────────────────────────────────────────

def quaternion_to_yaw(q: Dict[str, float]) -> float:
    """Yaw (rotation about Y axis) in radians from a unit quaternion."""
    x = float(q.get("x", 0.0))
    y = float(q.get("y", 0.0))
    z = float(q.get("z", 0.0))
    w = float(q.get("w", 1.0))
    return math.atan2(2.0 * (w * y + x * z), 1.0 - 2.0 * (y * y + z * z))


def _shift(px: float, pz: float, min_x: float, min_z: float) -> Tuple[float, float]:
    return px - min_x, pz - min_z


def _synthesize_walls_from_objects(
    objects: List[Dict[str, Any]],
    meta: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Build a 4-wall bounding rectangle around the detected objects.

    RoomPlan sometimes ends a session without walls (poor lighting, partial
    scan, user stopped early). When that happens we still have object data,
    so we synthesize a rectangular room around the object bbox so the
    downstream pipeline (enricher → map → narration → recommendations) can
    still produce a usable Braille map and ADA report.
    """
    if not objects:
        return []

    xs = [float(o["positionX"]) for o in objects]
    zs = [float(o["positionZ"]) for o in objects]
    obj_w = [float(o.get("widthMeters") or 0.0) for o in objects]
    obj_d = [float(o.get("depthMeters") or o.get("widthMeters") or 0.0) for o in objects]

    pad = 1.5  # m of padding so the room isn't tight against the objects
    min_x = min(x - w / 2 for x, w in zip(xs, obj_w)) - pad
    max_x = max(x + w / 2 for x, w in zip(xs, obj_w)) + pad
    min_z = min(z - d / 2 for z, d in zip(zs, obj_d)) - pad
    max_z = max(z + d / 2 for z, d in zip(zs, obj_d)) + pad

    width = max(max_x - min_x, 2.0)
    depth = max(max_z - min_z, 2.0)
    cx = (min_x + max_x) / 2
    cz = (min_z + max_z) / 2

    height = float(meta.get("roomHeightMeters") or 2.5)

    # Two pairs of walls forming a rectangle — yaw 0 = horizontal (width axis).
    return [
        {  # bottom
            "index": 0,
            "positionX": cx, "positionY": 0.0, "positionZ": min_z,
            "widthMeters": width, "heightMeters": height,
            "rotationQuaternion": {"x": 0, "y": 0, "z": 0, "w": 1},
            "_synthesized": True,
        },
        {  # top
            "index": 1,
            "positionX": cx, "positionY": 0.0, "positionZ": max_z,
            "widthMeters": width, "heightMeters": height,
            "rotationQuaternion": {"x": 0, "y": 0, "z": 0, "w": 1},
            "_synthesized": True,
        },
        {  # left — yaw 90° about Y (sin(45)=0.7071)
            "index": 2,
            "positionX": min_x, "positionY": 0.0, "positionZ": cz,
            "widthMeters": depth, "heightMeters": height,
            "rotationQuaternion": {"x": 0, "y": 0.7071068, "z": 0, "w": 0.7071068},
            "_synthesized": True,
        },
        {  # right
            "index": 3,
            "positionX": max_x, "positionY": 0.0, "positionZ": cz,
            "widthMeters": depth, "heightMeters": height,
            "rotationQuaternion": {"x": 0, "y": 0.7071068, "z": 0, "w": 0.7071068},
            "_synthesized": True,
        },
    ]


def project_to_2d(scan_data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop Y, normalize so the room's min corner is (0, 0), return a clean layout."""
    walls = scan_data.get("walls") or []
    doors = scan_data.get("doors") or []
    windows = scan_data.get("windows") or []
    objects = scan_data.get("objects") or []
    meta = scan_data.get("metadata") or {}

    walls_synthesized = False
    if not walls:
        if not objects:
            return {"error": "no_walls_or_objects_in_scan"}
        walls = _synthesize_walls_from_objects(objects, meta)
        walls_synthesized = True
        if not walls:
            return {"error": "no_walls_or_objects_in_scan"}

    xs = [float(w["positionX"]) for w in walls]
    zs = [float(w["positionZ"]) for w in walls]
    min_x, max_x = min(xs), max(xs)
    min_z, max_z = min(zs), max(zs)

    bbox_width = max(max_x - min_x, 0.0)
    bbox_depth = max(max_z - min_z, 0.0)
    room_width = max(float(meta.get("roomWidthMeters") or 0.0), bbox_width, 0.1)
    room_depth = max(float(meta.get("roomDepthMeters") or 0.0), bbox_depth, 0.1)

    walls_2d = []
    for w in walls:
        x, y = _shift(float(w["positionX"]), float(w["positionZ"]), min_x, min_z)
        walls_2d.append({
            "index": int(w["index"]),
            "x": x,
            "y": y,
            "width": float(w["widthMeters"]),
            "height": float(w.get("heightMeters", 0.0)),
            "rotation_y": quaternion_to_yaw(w.get("rotationQuaternion", {})),
        })

    doors_2d = []
    for d in doors:
        x, y = _shift(float(d["positionX"]), float(d["positionZ"]), min_x, min_z)
        doors_2d.append({
            "index": int(d["index"]),
            "category": d.get("category", "door"),
            "x": x,
            "y": y,
            "width": float(d["widthMeters"]),
            "rotation_y": quaternion_to_yaw(d.get("rotationQuaternion", {})),
            "parent_wall_index": d.get("parentWallIndex"),
            "is_entrance": False,
        })

    windows_2d = []
    for win in windows:
        x, y = _shift(float(win["positionX"]), float(win["positionZ"]), min_x, min_z)
        windows_2d.append({
            "index": int(win["index"]),
            "x": x,
            "y": y,
            "width": float(win["widthMeters"]),
            "rotation_y": quaternion_to_yaw(win.get("rotationQuaternion", {})),
            "parent_wall_index": win.get("parentWallIndex"),
        })

    objects_2d = []
    for o in objects:
        x, y = _shift(float(o["positionX"]), float(o["positionZ"]), min_x, min_z)
        objects_2d.append({
            "index": int(o["index"]),
            "category": o.get("category", "Object"),
            "x": x,
            "y": y,
            "width": float(o["widthMeters"]),
            "depth": float(o.get("depthMeters") or o["widthMeters"]),
            "height": float(o.get("heightMeters", 0.0)),
            "confidence": o.get("confidence", "Medium"),
        })

    # First door is the entrance. If none, fall back to the longest wall midpoint.
    entrance: Optional[Dict[str, Any]]
    if doors_2d:
        doors_2d[0]["is_entrance"] = True
        entrance = {
            "kind": "door",
            "x": doors_2d[0]["x"],
            "y": doors_2d[0]["y"],
            "width": doors_2d[0]["width"],
            "parent_wall_index": doors_2d[0]["parent_wall_index"],
        }
    else:
        longest = max(walls_2d, key=lambda w: w["width"])
        entrance = {
            "kind": "wall_midpoint",
            "x": longest["x"],
            "y": longest["y"],
            "width": longest["width"],
            "parent_wall_index": longest["index"],
        }

    layout = {
        "room_width": room_width,
        "room_depth": room_depth,
        "origin_offset": {"x": min_x, "z": min_z},
        "walls": walls_2d,
        "doors": doors_2d,
        "windows": windows_2d,
        "objects": objects_2d,
        "entrance": entrance,
        "source": "scan",
    }
    if walls_synthesized:
        # Tells downstream agents (recommendations, narration) that the walls
        # are an estimated bbox rather than a true RoomPlan capture.
        layout["walls_synthesized"] = True
        layout["space_name"] = (meta.get("roomName") or "Scanned space")
        layout["space_type"] = "room"
    return layout


# ── Backend I/O ──────────────────────────────────────────────────────────────

def fetch_room_full(room_id: str) -> Dict[str, Any]:
    resp = requests.get(f"{BACKEND_URL}/rooms/{room_id}/full", timeout=30)
    resp.raise_for_status()
    return resp.json()


def patch_room(room_id: str, updates: Dict[str, Any]) -> None:
    resp = requests.patch(f"{BACKEND_URL}/rooms/{room_id}", json=updates, timeout=30)
    resp.raise_for_status()


# ── Core pipeline step ───────────────────────────────────────────────────────

async def process_room(ctx: Context, room_id: str) -> Optional[str]:
    """Fetch → project → PATCH → hand off to enricher. Returns error string or None."""
    ctx.logger.info(f"fetching scan for room {room_id}")
    try:
        room = fetch_room_full(room_id)
    except Exception as exc:
        err = f"failed to fetch room {room_id}: {exc}"
        ctx.logger.error(err)
        return err

    scan_data = room.get("scan_data") or {}
    layout = project_to_2d(scan_data)
    if "error" in layout:
        ctx.logger.error(f"projection failed: {layout['error']}")
        patch_room(room_id, {"status": f"error_{layout['error']}"})
        return layout["error"]
    if layout.get("walls_synthesized"):
        ctx.logger.warning(
            f"RoomPlan returned no walls — synthesized a {layout['room_width']:.1f}m × "
            f"{layout['room_depth']:.1f}m bbox from {len(layout.get('objects') or [])} objects"
        )

    ctx.logger.info(
        f"room {room_id}: {layout['room_width']:.2f}m × {layout['room_depth']:.2f}m, "
        f"walls={len(layout['walls'])}, doors={len(layout['doors'])}, "
        f"windows={len(layout['windows'])}, objects={len(layout['objects'])}"
    )

    patch_room(room_id, {"layout_2d": layout, "status": "spatial_processed"})

    ctx.logger.info(f"→ sending EnrichmentRequest to {ENRICHER_ADDRESS}")
    await ctx.send(ENRICHER_ADDRESS, EnrichmentRequest(room_id=room_id))
    return None


# ── uAgent message handler (from backend /trigger or test_pipeline) ──────────

@spatial_agent.on_message(model=SpatialProcessingRequest)
async def on_spatial_request(
    ctx: Context, sender: str, msg: SpatialProcessingRequest
) -> None:
    ctx.logger.info(f"[msg] SpatialProcessingRequest from {sender} room={msg.room_id}")
    await process_room(ctx, msg.room_id)


# ── Fetch.ai Chat Protocol (required for prize eligibility + ASI:One) ────────

chat_proto = Protocol(spec=chat_protocol_spec)

ROOM_ID_PATTERN = re.compile(
    r"(?:process|run|scan|start|generate|map)\s+room\s+([A-Za-z0-9\-_]+)",
    re.IGNORECASE,
)

HELP_TEXT = (
    "Hi — I'm the BrailleMap Spatial Processor, entry point to a four-agent "
    "pipeline that turns an iPhone LiDAR scan into a Braille-ready PDF and an "
    "audio walkthrough.\n\n"
    "Say `process room <room_id>` and I'll kick off the pipeline. The room "
    "document in the backend will fill in with `layout_2d`, `pdf_url`, "
    "`narration_text`, and `audio_url` as each agent finishes."
)


async def _send_text(ctx: Context, recipient: str, text: str) -> None:
    reply = ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[TextContent(type="text", text=text)],
    )
    await ctx.send(recipient, reply)


@chat_proto.on_message(ChatMessage)
async def on_chat_message(ctx: Context, sender: str, msg: ChatMessage) -> None:
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc),
            acknowledged_msg_id=msg.msg_id,
        ),
    )

    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
    if not text:
        return

    ctx.logger.info(f"[chat] {sender}: {text!r}")

    match = ROOM_ID_PATTERN.search(text)
    if not match:
        await _send_text(ctx, sender, HELP_TEXT)
        return

    room_id = match.group(1)
    await _send_text(ctx, sender, f"Starting the BrailleMap pipeline for room `{room_id}`…")
    error = await process_room(ctx, room_id)
    if error:
        await _send_text(ctx, sender, f"Pipeline could not start: {error}")
    else:
        await _send_text(
            ctx,
            sender,
            f"Room `{room_id}` has been spatially processed and handed off to the "
            f"Object Enricher. The PDF and audio URLs will appear on the room "
            f"document when Agents 3 and 4 finish.",
        )


@chat_proto.on_message(ChatAcknowledgement)
async def on_chat_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
    ctx.logger.debug(f"[chat] ack {msg.acknowledged_msg_id} from {sender}")


spatial_agent.include(chat_proto, publish_manifest=True)


if __name__ == "__main__":
    print("═" * 60)
    print(" BrailleMap Spatial Processor (Agent 1)")
    print(f" Address        : {spatial_agent.address}")
    print(f" Port           : {AGENT_PORT_1}")
    print(f" → Enricher     : {ENRICHER_ADDRESS}")
    print(f" Backend        : {BACKEND_URL}")
    print(" Chat Protocol  : enabled (ASI:One compatible)")
    print("═" * 60)
    spatial_agent.run()
