#!/usr/bin/env python3
"""
BrailleMap Mock Backend

Run this locally to test the iOS upload flow and drive the agent pipeline.

Setup:
    pip install fastapi uvicorn python-dotenv
    python mock_backend.py

From iPhone: use ngrok so the device can reach your Mac:
    brew install ngrok
    ngrok http 8000
    # Copy the https URL into kBaseURL in BackendClient.swift
"""

import os
import sys
import uuid
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import base64
import json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Make braillemap-agents importable so POST /trigger can call the signing helper.
_AGENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "braillemap-agents")
if os.path.isdir(_AGENT_DIR) and _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

# Load .env from braillemap-agents so AGENT_SEED_1 / AGENT_PORT_1 resolve.
try:
    from dotenv import load_dotenv

    _env_path = os.path.join(_AGENT_DIR, ".env")
    if os.path.isfile(_env_path):
        load_dotenv(_env_path)
except Exception:
    pass

USE_CLOUDINARY = os.getenv("Set_cloudinary", "false").lower() == "true"
if USE_CLOUDINARY:
    import cloudinary
    import cloudinary.uploader
    cloudinary.config(
        cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
        api_key=os.getenv("CLOUDINARY_API_KEY"),
        api_secret=os.getenv("CLOUDINARY_API_SECRET"),
        secure=True,
    )

app = FastAPI(title="BrailleMap Mock Backend", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static file serving (local PDF + audio from agents) ──────────────────────
_OUTPUTS_DIR = os.path.join(_AGENT_DIR, "outputs")
_PDF_DIR = os.path.join(_OUTPUTS_DIR, "pdfs")
_AUDIO_DIR = os.path.join(_OUTPUTS_DIR, "audio")
_RECS_DIR = os.path.join(_OUTPUTS_DIR, "recommendations")
os.makedirs(_PDF_DIR, exist_ok=True)
os.makedirs(_AUDIO_DIR, exist_ok=True)
os.makedirs(_RECS_DIR, exist_ok=True)
app.mount("/files/pdfs", StaticFiles(directory=_PDF_DIR), name="pdfs")
app.mount("/files/audio", StaticFiles(directory=_AUDIO_DIR), name="audio")
app.mount(
    "/files/recommendations",
    StaticFiles(directory=_RECS_DIR),
    name="recommendations",
)

# In-memory store (resets on restart — that's fine for testing)
rooms_db: Dict[str, Dict] = {}

# Fields kept server-side but not returned on the light GET endpoints.
HEAVY_FIELDS = {"scan_data", "photos"}


# ── Models ───────────────────────────────────────────────────────────────────

class PhotoData(BaseModel):
    image_base64: str
    camera_position: Optional[Dict[str, float]] = None
    camera_rotation: Optional[Dict[str, float]] = None
    camera_transform: Optional[List[float]] = None
    camera_intrinsics: Optional[List[float]] = None
    timestamp: float = 0.0


class UploadMetadata(BaseModel):
    room_name: str
    building_name: str
    scanned_at: str
    device_model: str = "Unknown"


class ScanUpload(BaseModel):
    scan_data: Dict[str, Any]
    photos: List[PhotoData]
    metadata: UploadMetadata


class UploadResponse(BaseModel):
    room_id: str
    status: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _light_view(room: Dict[str, Any]) -> Dict[str, Any]:
    """Room document without the heavy scan_data + photos blobs."""
    return {k: v for k, v in room.items() if k not in HEAVY_FIELDS}


def _print_receipt(room_id: str, payload: ScanUpload) -> None:
    meta = payload.metadata
    scan = payload.scan_data
    divider = "═" * 52
    print(f"\n{divider}")
    print("  NEW SCAN RECEIVED")
    print(divider)
    print(f"  Room ID   : {room_id}")
    print(f"  Room      : {meta.room_name}")
    print(f"  Building  : {meta.building_name}")
    print(f"  Device    : {meta.device_model}")
    print(f"  Scanned   : {meta.scanned_at}")
    print(f"  Photos    : {len(payload.photos)}")
    print(f"  Walls     : {len(scan.get('walls', []))}")
    print(f"  Doors     : {len(scan.get('doors', []))}")
    print(f"  Windows   : {len(scan.get('windows', []))}")
    print(f"  Objects   : {len(scan.get('objects', []))}")
    print(divider + "\n")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "status": "BrailleMap mock backend running",
        "rooms_stored": len(rooms_db),
        "endpoints": {
            "POST  /scan": "Upload a room scan",
            "POST  /floorplan": "Upload a floor plan image for AI analysis",
            "GET   /rooms": "List all scanned rooms (light view)",
            "GET   /rooms/{id}": "Room details (light — no scan_data/photos)",
            "GET   /rooms/{id}/full": "Full room including scan_data + photos (agents use this)",
            "PATCH /rooms/{id}": "Partial update — agents write layout_2d / pdf_url / audio_url",
            "POST  /trigger/{id}": "Kick off the agent pipeline for a room",
            "POST  /rooms/{id}/voice_session": "Mint an ElevenLabs Conversational AI token + per-room overrides",
        },
    }


@app.post("/scan", response_model=UploadResponse)
async def upload_scan(payload: ScanUpload) -> UploadResponse:
    if len(payload.photos) < 3:
        raise HTTPException(
            status_code=400,
            detail=f"At least 3 photos required, got {len(payload.photos)}.",
        )

    room_id = str(uuid.uuid4())
    scan = payload.scan_data

    rooms_db[room_id] = {
        "_id": room_id,
        "metadata": payload.metadata.model_dump(),
        "status": "received",
        "scan_summary": {
            "walls": len(scan.get("walls", [])),
            "doors": len(scan.get("doors", [])),
            "windows": len(scan.get("windows", [])),
            "objects": len(scan.get("objects", [])),
        },
        "photo_count": len(payload.photos),
        "created_at": datetime.now(timezone.utc).isoformat(),
        # Persisted so agents can read via GET /rooms/{id}/full.
        "scan_data": scan,
        "photos": [p.model_dump() for p in payload.photos],
    }

    _print_receipt(room_id, payload)

    def _upload_to_cloudinary_bg():
        if not USE_CLOUDINARY:
            return
        try:
            json_str = json.dumps(scan)
            b64_json = base64.b64encode(json_str.encode('utf-8')).decode('utf-8')
            res = cloudinary.uploader.upload(
                f"data:application/json;base64,{b64_json}",
                resource_type="raw",
                folder="braillemap/scans",
                public_id=f"scan_{room_id}.json",
            )
            rooms_db[room_id]["cloudinary_scan_url"] = res.get("secure_url")
            
            photo_urls = []
            for i, p in enumerate(payload.photos):
                img_data = p.image_base64
                if not img_data.startswith("data:"):
                    img_data = f"data:image/jpeg;base64,{img_data}"
                pres = cloudinary.uploader.upload(
                    img_data,
                    folder=f"braillemap/scans/{room_id}",
                    public_id=f"photo_{i}",
                )
                photo_urls.append(pres.get("secure_url"))
            rooms_db[room_id]["cloudinary_photo_urls"] = photo_urls
            print(f"  ✓ Uploaded scan data and {len(photo_urls)} photos to Cloudinary for room {room_id}")
        except Exception as e:
            print(f"  ✗ Cloudinary scan upload failed: {e}")

    threading.Thread(target=_upload_to_cloudinary_bg, daemon=True).start()

    # Auto-trigger the agent pipeline in the background
    def _trigger_bg():
        try:
            from trigger import trigger_spatial_pipeline
            trigger_spatial_pipeline(room_id)
            rooms_db[room_id]["status"] = "pipeline_triggered"
            rooms_db[room_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
            print(f"  ✓ Pipeline auto-triggered for room {room_id}")
        except Exception as exc:
            print(f"  ✗ Auto-trigger failed for room {room_id}: {exc}")
            rooms_db[room_id]["status"] = "trigger_failed"
            rooms_db[room_id]["trigger_error"] = str(exc)

    threading.Thread(target=_trigger_bg, daemon=True).start()

    return UploadResponse(room_id=room_id, status="received")


# ── Floor Plan Upload ────────────────────────────────────────────────────────

class FloorPlanMetadata(BaseModel):
    building_name: str
    location_name: str


class FloorPlanUpload(BaseModel):
    image_base64: str
    metadata: FloorPlanMetadata


@app.post("/floorplan", response_model=UploadResponse)
async def upload_floorplan(payload: FloorPlanUpload) -> UploadResponse:
    if not payload.image_base64:
        raise HTTPException(status_code=400, detail="image_base64 is required")

    room_id = str(uuid.uuid4())
    meta = payload.metadata

    rooms_db[room_id] = {
        "_id": room_id,
        "metadata": {
            "room_name": meta.location_name,
            "building_name": meta.building_name,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "device_model": "Floor Plan Upload",
        },
        "source": "floorplan",
        "status": "received",
        "photo_count": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "floorplan_image": payload.image_base64,
    }

    divider = "═" * 52
    print(f"\n{divider}")
    print("  NEW FLOOR PLAN RECEIVED")
    print(divider)
    print(f"  Room ID   : {room_id}")
    print(f"  Building  : {meta.building_name}")
    print(f"  Location  : {meta.location_name}")
    print(f"  Image size: {len(payload.image_base64)} chars (base64)")
    print(divider + "\n")

    cloudinary_done = threading.Event()

    def _upload_floorplan_bg():
        if not USE_CLOUDINARY:
            cloudinary_done.set()
            return
        try:
            img_data = payload.image_base64
            if not img_data.startswith("data:"):
                img_data = f"data:image/jpeg;base64,{img_data}"
            res = cloudinary.uploader.upload(
                img_data,
                folder="braillemap/floorplans",
                public_id=f"floorplan_{room_id}",
            )
            rooms_db[room_id]["cloudinary_floorplan_url"] = res.get("secure_url")
            print(f"  ✓ Uploaded floorplan to Cloudinary for room {room_id}")
        except Exception as e:
            print(f"  ✗ Cloudinary floorplan upload failed: {e}")
        finally:
            cloudinary_done.set()

    threading.Thread(target=_upload_floorplan_bg, daemon=True).start()

    # Auto-trigger floor plan analysis uAgent in background
    # MUST wait for Cloudinary upload to finish first so the agent can read the URL
    def _analyze_bg():
        cloudinary_done.wait(timeout=60)  # wait up to 60s for Cloudinary
        try:
            from trigger import trigger_floorplan_pipeline
            trigger_floorplan_pipeline(room_id)
            print(f"  ✓ Floor plan pipeline triggered for room {room_id}")
        except Exception as exc:
            print(f"  ✗ Failed to trigger floor plan agent: {exc}")
            rooms_db[room_id]["status"] = "error_trigger_floorplan"
            rooms_db[room_id]["trigger_error"] = str(exc)

    threading.Thread(target=_analyze_bg, daemon=True).start()

    return UploadResponse(room_id=room_id, status="received")


@app.get("/rooms/{room_id}/status")
async def get_room_status(room_id: str):
    """Lightweight polling endpoint for the iOS app."""
    if room_id not in rooms_db:
        raise HTTPException(status_code=404, detail="Room not found")
    room = rooms_db[room_id]
    return {
        "room_id": room_id,
        "status": room.get("status", "unknown"),
        "pdf_url": room.get("pdf_url"),
        "audio_url": room.get("audio_url"),
        "narration_text": room.get("narration_text"),
        "recommendations_pdf_url": room.get("recommendations_pdf_url"),
        "recommendations_summary": room.get("recommendations_summary"),
        "recommendations_score": room.get("recommendations_score"),
        "recommendations_count": room.get("recommendations_count"),
        "status_map_done": room.get("status_map_done", False),
        "status_narration_done": room.get("status_narration_done", False),
        "status_recommendations_done": room.get("status_recommendations_done", False),
    }


@app.get("/rooms")
async def list_rooms():
    return {"rooms": [_light_view(r) for r in rooms_db.values()]}


@app.get("/rooms/{room_id}")
async def get_room(room_id: str):
    if room_id not in rooms_db:
        raise HTTPException(status_code=404, detail="Room not found")
    return _light_view(rooms_db[room_id])


@app.get("/rooms/{room_id}/full")
async def get_room_full(room_id: str):
    if room_id not in rooms_db:
        raise HTTPException(status_code=404, detail="Room not found")
    return rooms_db[room_id]


@app.patch("/rooms/{room_id}")
async def patch_room(room_id: str, updates: Dict[str, Any]):
    if room_id not in rooms_db:
        raise HTTPException(status_code=404, detail="Room not found")
    # Shallow merge — agents send field-level patches like {"layout_2d": {...}}.
    rooms_db[room_id].update(updates)
    rooms_db[room_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
    return _light_view(rooms_db[room_id])


@app.post("/trigger/{room_id}")
async def trigger_pipeline(room_id: str):
    if room_id not in rooms_db:
        raise HTTPException(status_code=404, detail="Room not found")

    try:
        from trigger import trigger_spatial_pipeline  # type: ignore
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Could not import trigger helper. Check braillemap-agents/.env "
                f"and that `uagents` is installed. ({exc})"
            ),
        )

    try:
        trigger_spatial_pipeline(room_id)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach Agent 1 — is agent_spatial.py running? ({exc})",
        )

    rooms_db[room_id]["status"] = "pipeline_triggered"
    rooms_db[room_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
    return {"room_id": room_id, "status": "pipeline_triggered"}


@app.post("/rooms/{room_id}/voice_session")
async def start_voice_session(room_id: str):
    """Mint a per-room ElevenLabs Conversational AI session.

    Returns a short-lived `conversation_token` plus the system-prompt /
    first-message / voice-id overrides iOS should pass into
    `ElevenLabs.startConversation`. The xi-api-key never leaves the server.
    """
    if room_id not in rooms_db:
        raise HTTPException(status_code=404, detail="Room not found")

    room = rooms_db[room_id]
    layout = room.get("layout_2d") or {}
    if not layout:
        raise HTTPException(
            status_code=409,
            detail="Layout not ready yet — try again in a moment.",
        )

    agent_id = os.getenv("ELEVENLABS_AGENT_ID")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID")
    if not agent_id:
        raise HTTPException(
            status_code=500,
            detail="ELEVENLABS_AGENT_ID not configured on the server.",
        )

    try:
        from voice_session import (
            build_first_message,
            build_system_prompt,
            mint_conversation_token,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"voice_session import failed: {exc}"
        )

    metadata = room.get("metadata") or {}
    system_prompt = build_system_prompt(layout, metadata)
    first_message = build_first_message(layout, metadata)

    try:
        conversation_token = mint_conversation_token(agent_id)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"ElevenLabs token mint failed: {exc}"
        )

    return {
        "conversation_token": conversation_token,
        "agent_id": agent_id,
        "agent_overrides": {
            "prompt": system_prompt,
            "first_message": first_message,
            "language": "en",
        },
        "tts_overrides": {
            "voice_id": voice_id,
        },
    }


@app.get("/conversations")
async def get_conversations():
    agent_id = os.getenv("ELEVENLABS_AGENT_ID")
    if not agent_id:
        raise HTTPException(
            status_code=500,
            detail="ELEVENLABS_AGENT_ID not configured on the server.",
        )
    try:
        from voice_session import fetch_conversations, fetch_conversation_transcript
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"voice_session import failed: {exc}"
        )
    
    convs = fetch_conversations(agent_id)
    # Fetch transcripts for the recent conversations (limit to 10 for performance)
    results = []
    for c in convs[:10]:
        transcript = fetch_conversation_transcript(c["conversation_id"])
        results.append(transcript)
    return {"conversations": results}
# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    print("\n" + "═" * 52)
    print("  BrailleMap Mock Backend  (v2 — agent pipeline enabled)")
    print("  Interactive API docs → http://localhost:8000/docs")
    print()
    print("  New endpoints for the agent pipeline:")
    print("    POST  /floorplan            upload a floor plan image")
    print("    GET   /rooms/{id}/full      full scan_data + photos")
    print("    PATCH /rooms/{id}           partial updates from agents")
    print("    POST  /trigger/{id}         kick off Agent 1")
    print()
    print("  To reach from iPhone:")
    print("    ngrok http 8000")
    print("    → paste the https URL into kBaseURL in BackendClient.swift")
    print("═" * 52 + "\n")

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
