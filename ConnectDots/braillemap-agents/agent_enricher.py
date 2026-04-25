"""Agent 2 — Object Enricher.

Takes the 2D layout produced by Agent 1 and replaces generic RoomPlan
categories ("Chair", "Storage", "Table") with blind-navigation-friendly
descriptions using Gemini Vision + the room photos. Then fans out to
Agents 3 (map) and 4 (narration) in parallel.
"""

from __future__ import annotations

import base64
import json
import os
import re
from io import BytesIO
from typing import Any, Dict, List, Tuple

import numpy as np
from google import genai
import requests
from dotenv import load_dotenv
from PIL import Image, ImageDraw
from uagents import Agent, Context

from schemas import (
    EnrichmentRequest,
    MapGenerationRequest,
    NarrationRequest,
    RecommendationsRequest,
    address_from_seed,
)

load_dotenv()

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
AGENT_SEED_2 = os.getenv("AGENT_SEED_2")
AGENT_SEED_3 = os.getenv("AGENT_SEED_3")
AGENT_SEED_4 = os.getenv("AGENT_SEED_4")
AGENT_SEED_6 = os.getenv("AGENT_SEED_6")
AGENT_PORT_2 = int(os.getenv("AGENT_PORT_2", "8002"))
DROP_LOW_CONFIDENCE_UNKNOWNS = os.getenv("DROP_LOW_CONFIDENCE_UNKNOWNS", "false").lower() == "true"
MAX_PHOTOS_TO_SEND = 4

if not AGENT_SEED_2 or not AGENT_SEED_3 or not AGENT_SEED_4:
    raise SystemExit("Set AGENT_SEED_2, AGENT_SEED_3, and AGENT_SEED_4 in .env")
if not GEMINI_API_KEY:
    raise SystemExit("Set GEMINI_API_KEY in .env")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

MAP_AGENT_ADDRESS = address_from_seed(AGENT_SEED_3)
NARRATION_AGENT_ADDRESS = address_from_seed(AGENT_SEED_4)
RECOMMENDATIONS_AGENT_ADDRESS = (
    address_from_seed(AGENT_SEED_6) if AGENT_SEED_6 else None
)

enricher_agent = Agent(
    name="braillemap_object_enricher",
    seed=AGENT_SEED_2,
    port=AGENT_PORT_2,
    endpoint=[f"http://localhost:{AGENT_PORT_2}/submit"],
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _decode_photo(photo: Any) -> Image.Image | None:
    """Accepts either a raw base64 string or the iPhone upload dict."""
    b64: str | None = None
    if isinstance(photo, str):
        b64 = photo
    elif isinstance(photo, dict):
        b64 = photo.get("image_base64")
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
        img = Image.open(BytesIO(raw))
        img.load()  # force decode so a bad payload fails here, not inside Gemini
        return img
    except Exception:
        return None


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # drop opening fence (possibly "```json")
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def project_object_to_2d(
    raw_obj: Dict[str, Any],
    transform_list: List[float],
    intrinsics_list: List[float],
    img_width: int,
    img_height: int
) -> Tuple[int, int, int, int] | None:
    """Projects 3D RoomPlan object bounding box into 2D image coordinates."""
    if not transform_list or not intrinsics_list:
        return None
        
    try:
        transform = np.array(transform_list).reshape(4, 4)
        intrinsics = np.array(intrinsics_list).reshape(3, 3)
    except Exception:
        return None

    # RoomPlan original 3D coordinates
    px = float(raw_obj.get("positionX", 0.0))
    py = float(raw_obj.get("positionY", 0.0))
    pz = float(raw_obj.get("positionZ", 0.0))
    w = float(raw_obj.get("widthMeters", 0.0))
    h = float(raw_obj.get("heightMeters", 0.0))
    d = float(raw_obj.get("depthMeters", 0.0))
    
    # 8 corners in 3D
    dx, dy, dz = w/2, h/2, d/2
    corners = np.array([
        [px-dx, py-dy, pz-dz], [px+dx, py-dy, pz-dz],
        [px-dx, py+dy, pz-dz], [px+dx, py+dy, pz-dz],
        [px-dx, py-dy, pz+dz], [px+dx, py-dy, pz+dz],
        [px-dx, py+dy, pz+dz], [px+dx, py+dy, pz+dz]
    ])
    
    view_matrix = np.linalg.inv(transform)
    corners_hom = np.hstack([corners, np.ones((8, 1))])
    corners_cam = (view_matrix @ corners_hom.T) # (4, 8)
    
    # ARKit to OpenCV coordinates (Z forward, Y down)
    X_cv = corners_cam[0, :]
    Y_cv = -corners_cam[1, :]
    Z_cv = -corners_cam[2, :]
    
    # Must be in front of camera
    if np.all(Z_cv <= 0):
        return None
        
    Z_cv[Z_cv <= 0] = 1e-5
    
    u = (intrinsics[0, 0] * X_cv / Z_cv) + intrinsics[0, 2]
    v = (intrinsics[1, 1] * Y_cv / Z_cv) + intrinsics[1, 2]
    
    min_x, max_x = max(0, int(np.min(u))), min(img_width, int(np.max(u)))
    min_y, max_y = max(0, int(np.min(v))), min(img_height, int(np.max(v)))
    
    if max_x <= min_x or max_y <= min_y:
        return None
        
    area = (max_x - min_x) * (max_y - min_y)
    img_area = img_width * img_height
    if area / img_area < 0.01:
        return None
        
    return (min_x, min_y, max_x, max_y)

def build_targeted_prompt(category: str) -> str:
    return f"""You are helping a blind person understand a room. 
In the attached photo, there is an object outlined by a thick RED bounding box.
A LiDAR scanner categorized this object as a "{category}".

Please provide a highly specific, descriptive label for the object in the RED box that would help a blind person navigate.
For example:
- If it's a "Table", is it a "reception desk with computer monitor", "dining table", or "coffee table"?
- If it's "Storage", is it a "bookshelf", "filing cabinet", or "hand dryer"?

Respond with ONLY the specific string description. Do not include any other text or formatting. Keep it under 8 words.
"""

def call_gemini_vision_targeted(image: Image.Image, category: str) -> str | None:
    prompt = build_targeted_prompt(category)
    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt, image]
        )
        raw = getattr(response, "text", "") or ""
        return raw.strip()
    except Exception as exc:
        print(f"[enricher] Gemini targeted call failed: {exc}")
        return None


def _object_line(o: Dict[str, Any]) -> str:
    return (
        f"- index={o['index']}: currently '{o['category']}' "
        f"at ({o['x']:.2f}m, {o['y']:.2f}m), "
        f"size {o['width']:.2f}m × {o['depth']:.2f}m, "
        f"RoomPlan confidence={o.get('confidence', 'Medium')}"
    )


def build_batch_prompt(objects: List[Dict[str, Any]]) -> str:
    object_list = "\n".join(_object_line(o) for o in objects)
    return f"""You are helping a blind person understand a room. A LiDAR scanner detected these objects:

{object_list}

Based on the attached photos of the room, provide a more specific label for each object that would help a blind person navigate. For example:
- "Table" → "reception desk with computer monitor" or "dining table" or "coffee table"
- "Storage" → "bookshelf" or "filing cabinet" or "hand dryer"
- "Chair" → "office chair with armrests" or "dining chair" or "lounge chair"

Respond in this EXACT JSON format with no other text:
{{
  "enriched": [
    {{"index": 0, "label": "specific description"}},
    {{"index": 1, "label": "specific description"}}
  ]
}}

If you cannot identify an object from the photos, use the original label. Be concise — labels should be under 8 words.
"""


def call_gemini_vision_batch(
    objects: List[Dict[str, Any]], images: List[Image.Image]
) -> Dict[int, str]:
    """Fallback: batch enrichment when no camera pose data is available."""
    if not objects or not images:
        return {}
    prompt = build_batch_prompt(objects)
    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt, *images]
        )
    except Exception as exc:
        print(f"[enricher] Gemini batch call failed: {exc}")
        return {}
    raw = getattr(response, "text", "") or ""
    cleaned = _strip_code_fences(raw)
    try:
        data = json.loads(cleaned)
    except Exception as exc:
        print(f"[enricher] Gemini JSON parse failed: {exc}")
        print(f"[enricher] raw response: {raw[:600]!r}")
        return {}
    return {
        int(e["index"]): str(e["label"]).strip()
        for e in data.get("enriched", [])
        if "index" in e and "label" in e
    }


# ── Backend I/O ──────────────────────────────────────────────────────────────

def fetch_room_full(room_id: str) -> Dict[str, Any]:
    resp = requests.get(f"{BACKEND_URL}/rooms/{room_id}/full", timeout=30)
    resp.raise_for_status()
    return resp.json()


def patch_room(room_id: str, updates: Dict[str, Any]) -> None:
    resp = requests.patch(f"{BACKEND_URL}/rooms/{room_id}", json=updates, timeout=30)
    resp.raise_for_status()


# ── Message handler ──────────────────────────────────────────────────────────

@enricher_agent.on_message(model=EnrichmentRequest)
async def on_enrichment_request(
    ctx: Context, sender: str, msg: EnrichmentRequest
) -> None:
    room_id = msg.room_id
    ctx.logger.info(f"[msg] EnrichmentRequest from {sender} room={room_id}")

    try:
        room = fetch_room_full(room_id)
    except Exception as exc:
        ctx.logger.error(f"failed to fetch room {room_id}: {exc}")
        return

    layout = room.get("layout_2d") or {}
    objects: List[Dict[str, Any]] = list(layout.get("objects") or [])
    photos = room.get("photos") or []

    if not objects:
        ctx.logger.warning(f"room {room_id} has no objects to enrich; skipping Gemini call")
        patch_room(room_id, {"status": "enriched_no_objects"})
    else:
        ctx.logger.info(
            f"enriching {len(objects)} object(s) for room {room_id} "
            f"with {len(photos)} photo(s)"
        )

        scan_data = room.get("scan_data", {})
        raw_objects = {int(o["index"]): o for o in scan_data.get("objects", [])}

        # Check if any photos have camera pose data
        has_pose_data = any(
            isinstance(p, dict) and p.get("camera_transform")
            for p in photos
        )

        if has_pose_data:
            ctx.logger.info("using targeted projection-based enrichment (camera pose available)")
            kept: List[Dict[str, Any]] = []
            for obj in objects:
                idx = int(obj["index"])
                raw_obj = raw_objects.get(idx)
                category = obj["category"]
                
                best_img = None
                best_area = 0
                
                if raw_obj:
                    for photo in photos:
                        if not isinstance(photo, dict) or not photo.get("camera_transform"):
                            continue
                            
                        img = _decode_photo(photo)
                        if not img:
                            continue
                            
                        transform_list = photo.get("camera_transform")
                        intrinsics_list = photo.get("camera_intrinsics")
                        
                        bbox = project_object_to_2d(raw_obj, transform_list, intrinsics_list, img.width, img.height)
                        if bbox:
                            min_x, min_y, max_x, max_y = bbox
                            area = (max_x - min_x) * (max_y - min_y)
                            if area > best_area:
                                best_area = area
                                
                                # Draw bounding box on a copy
                                img_copy = img.copy()
                                draw = ImageDraw.Draw(img_copy)
                                draw.rectangle([min_x, min_y, max_x, max_y], outline="red", width=5)
                                best_img = img_copy

                enriched_label = None
                if best_img:
                    enriched_label = call_gemini_vision_targeted(best_img, category)
                    
                if enriched_label:
                    obj["original_category"] = category
                    obj["category"] = enriched_label
                    obj["enriched"] = True
                else:
                    obj["enriched"] = False
                    if DROP_LOW_CONFIDENCE_UNKNOWNS and obj.get("confidence") == "Low":
                        ctx.logger.info(f"dropping low-confidence object {idx} ({obj.get('original_category', obj.get('category'))})")
                        continue
                kept.append(obj)
        else:
            ctx.logger.warning("no camera pose data — falling back to batch enrichment")
            images: List[Image.Image] = []
            for photo in photos[:MAX_PHOTOS_TO_SEND]:
                img = _decode_photo(photo)
                if img is not None:
                    images.append(img)

            enriched_map: Dict[int, str] = {}
            if images:
                enriched_map = call_gemini_vision_batch(objects, images)
            else:
                ctx.logger.warning("no usable photos — keeping original labels")

            kept = []
            for obj in objects:
                idx = int(obj["index"])
                if idx in enriched_map and enriched_map[idx]:
                    obj["original_category"] = obj["category"]
                    obj["category"] = enriched_map[idx]
                    obj["enriched"] = True
                else:
                    obj["enriched"] = False
                    if DROP_LOW_CONFIDENCE_UNKNOWNS and obj.get("confidence") == "Low":
                        ctx.logger.info(f"dropping low-confidence object {idx} ({obj.get('original_category', obj.get('category'))})")
                        continue
                kept.append(obj)

        layout["objects"] = kept
        patch_room(room_id, {"layout_2d": layout, "status": "enriched"})

        ctx.logger.info(
            f"enrichment done: {sum(1 for o in kept if o.get('enriched'))}/{len(kept)} "
            f"objects relabeled"
        )

    # Fan out to map generator, narration agent, and ADA recommendations agent.
    ctx.logger.info(f"→ MapGenerationRequest  → {MAP_AGENT_ADDRESS}")
    ctx.logger.info(f"→ NarrationRequest      → {NARRATION_AGENT_ADDRESS}")
    await ctx.send(MAP_AGENT_ADDRESS, MapGenerationRequest(room_id=room_id))
    await ctx.send(NARRATION_AGENT_ADDRESS, NarrationRequest(room_id=room_id))
    if RECOMMENDATIONS_AGENT_ADDRESS:
        ctx.logger.info(
            f"→ RecommendationsRequest → {RECOMMENDATIONS_AGENT_ADDRESS}"
        )
        await ctx.send(
            RECOMMENDATIONS_AGENT_ADDRESS, RecommendationsRequest(room_id=room_id)
        )


if __name__ == "__main__":
    print("═" * 60)
    print(" BrailleMap Object Enricher (Agent 2)")
    print(f" Address        : {enricher_agent.address}")
    print(f" Port           : {AGENT_PORT_2}")
    print(f" → Map Gen      : {MAP_AGENT_ADDRESS}")
    print(f" → Narration    : {NARRATION_AGENT_ADDRESS}")
    print(f" Gemini model   : {GEMINI_MODEL}")
    print(f" Drop low-conf  : {DROP_LOW_CONFIDENCE_UNKNOWNS}")
    print("═" * 60)
    enricher_agent.run()
