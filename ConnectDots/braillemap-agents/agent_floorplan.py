"""Agent 5 — Floor Plan Analyzer uAgent.

Receives a FloorPlanAnalysisRequest, fetches the floor plan image from the
backend, sends it to Gemini Vision to extract spatial layout data, and produces
the standard `layout_2d` format.

Then, it directly triggers Agent 3 (Map) and Agent 4 (Narration) to complete
the pipeline.
"""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv
from google import genai
from PIL import Image
from uagents import Agent, Context

from schemas import (
    FloorPlanAnalysisRequest,
    MapGenerationRequest,
    NarrationRequest,
    RecommendationsRequest,
    address_from_seed,
)

load_dotenv()

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-pro-preview")

AGENT_SEED_5 = os.getenv("AGENT_SEED_5")
AGENT_SEED_3 = os.getenv("AGENT_SEED_3")
AGENT_SEED_4 = os.getenv("AGENT_SEED_4")
AGENT_SEED_6 = os.getenv("AGENT_SEED_6")
AGENT_PORT_5 = int(os.getenv("AGENT_PORT_5", "8005"))

if not AGENT_SEED_5 or not AGENT_SEED_3 or not AGENT_SEED_4:
    raise SystemExit("Set AGENT_SEED_5, AGENT_SEED_3, and AGENT_SEED_4 in .env")

MAP_ADDRESS = address_from_seed(AGENT_SEED_3)
NARRATION_ADDRESS = address_from_seed(AGENT_SEED_4)
RECOMMENDATIONS_ADDRESS = (
    address_from_seed(AGENT_SEED_6) if AGENT_SEED_6 else None
)

floorplan_agent = Agent(
    name="braillemap_floorplan_analyzer",
    seed=AGENT_SEED_5,
    port=AGENT_PORT_5,
    endpoint=[f"http://localhost:{AGENT_PORT_5}/submit"],
)

if not GEMINI_API_KEY:
    print("[floorplan] WARNING: GEMINI_API_KEY not set — floor plan analysis disabled")
    gemini_client = None
else:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)


# ── Gemini Prompt ────────────────────────────────────────────────────────────

ANALYSIS_PROMPT = """You are an expert spatial analyst helping create accessible tactile maps for blind and visually impaired people.

I'm providing you with a floor plan image. This could be a floor plan of a room, building, mall, subway station, city block, or any other space.

Please analyze this floor plan and extract ALL spatial information. Respond in this EXACT JSON format with NO other text:

{
  "space_type": "room | building_floor | mall | subway | campus | city_block | other",
  "space_name": "descriptive name of the space",
  "estimated_width_meters": <float>,
  "estimated_depth_meters": <float>,
  "walls": [
    {
      "index": 0,
      "x": <float, center X in meters from left>,
      "y": <float, center Y in meters from top>,
      "width": <float, length of wall segment in meters>,
      "height": 2.5,
      "rotation_y": <float, angle in radians, 0=horizontal, 1.5708=vertical>
    }
  ],
  "doors": [
    {
      "index": 0,
      "category": "door | entrance | emergency_exit | revolving_door",
      "x": <float>,
      "y": <float>,
      "width": <float, door width in meters>,
      "rotation_y": <float>,
      "parent_wall_index": <int or null>,
      "is_entrance": <bool, true for main entrance>
    }
  ],
  "windows": [
    {
      "index": 0,
      "x": <float>,
      "y": <float>,
      "width": <float>,
      "rotation_y": <float>,
      "parent_wall_index": <int or null>
    }
  ],
  "objects": [
    {
      "index": 0,
      "category": "<specific label: 'reception desk', 'elevator', 'staircase', 'restroom', 'seating area', 'information kiosk', 'escalator', 'ATM', 'ticket counter', etc.>",
      "x": <float>,
      "y": <float>,
      "width": <float>,
      "depth": <float>,
      "height": <float, estimated>,
      "confidence": "High"
    }
  ],
  "entrance": {
    "kind": "door",
    "x": <float>,
    "y": <float>,
    "width": <float>,
    "parent_wall_index": <int or null>
  }
}

IMPORTANT RULES:
1. Use meters for ALL dimensions. If no scale is visible, estimate based on standard architectural proportions (doors are ~0.9m wide, corridors ~1.5-2m wide, rooms ~3-6m, etc.)
2. The coordinate system starts at (0, 0) in the top-left corner.
3. For walls, trace the outline of the space. Each wall segment is a line with a center position, width (length), and rotation.
4. Identify EVERY labeled room, area, or landmark as an "object" with a descriptive category (e.g., "men's restroom", "elevator bank", "food court seating", "security checkpoint").
5. Mark the main entrance with is_entrance=true.
6. For complex spaces (malls, subways), corridors should be represented as wall segments.
7. Be thorough — a blind person depends on this for navigation.
"""


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _decode_image(image_base64: str) -> Image.Image | None:
    try:
        raw = base64.b64decode(image_base64)
        img = Image.open(BytesIO(raw))
        img.load()
        return img
    except Exception:
        return None


# ── Core Analysis ────────────────────────────────────────────────────────────

def analyze_floor_plan(image_base64: str) -> Dict[str, Any]:
    if not gemini_client:
        return {"error": "GEMINI_API_KEY not set"}

    img = _decode_image(image_base64)
    if not img:
        return {"error": "could not decode image"}

    # Resize if very large (save tokens)
    max_dim = 2048
    if max(img.size) > max_dim:
        ratio = max_dim / max(img.size)
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[ANALYSIS_PROMPT, img],
        )
    except Exception as exc:
        return {"error": f"Gemini API call failed: {exc}"}

    raw_text = getattr(response, "text", "") or ""
    cleaned = _strip_code_fences(raw_text)

    try:
        data = json.loads(cleaned)
    except Exception as exc:
        return {"error": f"JSON parse failed: {exc}", "raw": raw_text[:500]}

    # Convert Gemini output to the standard layout_2d format
    return _normalize_layout(data)


def _normalize_layout(data: Dict[str, Any]) -> Dict[str, Any]:
    room_w = float(data.get("estimated_width_meters") or 5.0)
    room_d = float(data.get("estimated_depth_meters") or 5.0)

    walls = []
    for w in data.get("walls") or []:
        walls.append({
            "index": int(w.get("index", len(walls))),
            "x": float(w.get("x", 0)),
            "y": float(w.get("y", 0)),
            "width": float(w.get("width", 1.0)),
            "height": float(w.get("height", 2.5)),
            "rotation_y": float(w.get("rotation_y", 0)),
        })

    doors = []
    for d in data.get("doors") or []:
        doors.append({
            "index": int(d.get("index", len(doors))),
            "category": d.get("category", "door"),
            "x": float(d.get("x", 0)),
            "y": float(d.get("y", 0)),
            "width": float(d.get("width", 0.9)),
            "rotation_y": float(d.get("rotation_y", 0)),
            "parent_wall_index": d.get("parent_wall_index"),
            "is_entrance": bool(d.get("is_entrance", False)),
        })

    windows = []
    for win in data.get("windows") or []:
        windows.append({
            "index": int(win.get("index", len(windows))),
            "x": float(win.get("x", 0)),
            "y": float(win.get("y", 0)),
            "width": float(win.get("width", 1.0)),
            "rotation_y": float(win.get("rotation_y", 0)),
            "parent_wall_index": win.get("parent_wall_index"),
        })

    objects = []
    for o in data.get("objects") or []:
        objects.append({
            "index": int(o.get("index", len(objects))),
            "category": str(o.get("category", "unknown")),
            "x": float(o.get("x", 0)),
            "y": float(o.get("y", 0)),
            "width": float(o.get("width", 1.0)),
            "depth": float(o.get("depth", 1.0)),
            "height": float(o.get("height", 1.0)),
            "confidence": o.get("confidence", "High"),
            "enriched": True,
        })

    entrance_data = data.get("entrance")
    if entrance_data:
        entrance = {
            "kind": entrance_data.get("kind", "door"),
            "x": float(entrance_data.get("x", 0)),
            "y": float(entrance_data.get("y", 0)),
            "width": float(entrance_data.get("width", 0.9)),
            "parent_wall_index": entrance_data.get("parent_wall_index"),
        }
    elif doors:
        ent_door = next((d for d in doors if d.get("is_entrance")), doors[0])
        entrance = {
            "kind": "door",
            "x": ent_door["x"],
            "y": ent_door["y"],
            "width": ent_door["width"],
            "parent_wall_index": ent_door.get("parent_wall_index"),
        }
    else:
        entrance = {
            "kind": "estimated",
            "x": room_w / 2,
            "y": 0.0,
            "width": 0.9,
            "parent_wall_index": None,
        }

    return {
        "room_width": room_w,
        "room_depth": room_d,
        "origin_offset": {"x": 0, "z": 0},
        "walls": walls,
        "doors": doors,
        "windows": windows,
        "objects": objects,
        "entrance": entrance,
        "source": "floorplan",
        "space_type": data.get("space_type", "unknown"),
        "space_name": data.get("space_name", "Floor Plan"),
    }


# ── uAgent logic ─────────────────────────────────────────────────────────────

def patch_room(room_id: str, updates: Dict[str, Any]) -> None:
    resp = requests.patch(f"{BACKEND_URL}/rooms/{room_id}", json=updates, timeout=30)
    resp.raise_for_status()


async def process_floorplan(ctx: Context, room_id: str) -> Optional[str]:
    """Fetch room from backend, analyze image, and trigger downstream."""
    ctx.logger.info(f"Analyzing floor plan for room {room_id}…")
    patch_room(room_id, {"status": "analyzing_floorplan"})

    # Fetch full room data to get base64 image
    try:
        resp = requests.get(f"{BACKEND_URL}/rooms/{room_id}/full", timeout=10)
        resp.raise_for_status()
        room_data = resp.json()
    except Exception as exc:
        ctx.logger.error(f"Failed to fetch room data: {exc}")
        return str(exc)

    image_base64 = room_data.get("floorplan_image")
    if not image_base64:
        return "No floorplan_image found in room record"

    layout = analyze_floor_plan(image_base64)

    if "error" in layout:
        ctx.logger.error(f"Analysis failed: {layout['error']}")
        patch_room(room_id, {
            "status": f"error_floorplan_{layout['error'][:50]}",
            "floorplan_error": layout.get("error"),
        })
        return layout["error"]

    patch_room(room_id, {
        "layout_2d": layout,
        "status": "floorplan_analyzed",
    })

    # Trigger Agents 3 (Map) and 4 (Narration) directly
    ctx.logger.info(f"→ sending MapGenerationRequest to {MAP_ADDRESS}")
    await ctx.send(MAP_ADDRESS, MapGenerationRequest(room_id=room_id))

    ctx.logger.info(f"→ sending NarrationRequest to {NARRATION_ADDRESS}")
    await ctx.send(NARRATION_ADDRESS, NarrationRequest(room_id=room_id))

    if RECOMMENDATIONS_ADDRESS:
        ctx.logger.info(
            f"→ sending RecommendationsRequest to {RECOMMENDATIONS_ADDRESS}"
        )
        await ctx.send(
            RECOMMENDATIONS_ADDRESS, RecommendationsRequest(room_id=room_id)
        )
    else:
        ctx.logger.info(
            "AGENT_SEED_6 not set — skipping ADA recommendations agent"
        )

    return None


@floorplan_agent.on_message(model=FloorPlanAnalysisRequest)
async def on_floorplan_request(
    ctx: Context, sender: str, msg: FloorPlanAnalysisRequest
) -> None:
    ctx.logger.info(f"[msg] FloorPlanAnalysisRequest from {sender} room={msg.room_id}")
    await process_floorplan(ctx, msg.room_id)


if __name__ == "__main__":
    print(f"════════════════════════════════════════════════════════════")
    print(f" BrailleMap Floor Plan Analyzer (Agent 5)")
    print(f" Address       : {floorplan_agent.address}")
    print(f" Port          : {AGENT_PORT_5}")
    print(f" Model         : {GEMINI_MODEL}")
    print(f" ════════════════════════════════════════════════════════════")
    floorplan_agent.run()
