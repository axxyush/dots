"""Agent 4 — Narration Agent.

Fetches the enriched layout, generates a 150-word walkthrough with ASI1-Mini
(Gemini fallback if ASI:One is unreachable), synthesizes it with ElevenLabs,
saves the MP3 locally (or to Cloudinary if USE_CLOUDINARY=true), and patches
`audio_url` + `narration_text` back to the room document.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

import google.generativeai as genai_legacy  # kept for backward compat if needed
from google import genai
import requests
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from uagents import Agent, Context

from schemas import NarrationRequest

load_dotenv()

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
AGENT_SEED_4 = os.getenv("AGENT_SEED_4")
AGENT_PORT_4 = int(os.getenv("AGENT_PORT_4", "8004"))
USE_CLOUDINARY = os.getenv("Set_cloudinary", "false").lower() == "true"

ASI_API_URL = os.getenv("ASI_API_URL", "https://api.asi1.ai/v1/chat/completions")
ASI_API_KEY = os.getenv("ASI_API_KEY")
ASI_MODEL = os.getenv("ASI_MODEL", "asi1-mini")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")

# Local output directory
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs", "audio")
os.makedirs(OUTPUT_DIR, exist_ok=True)

if not AGENT_SEED_4:
    raise SystemExit("Set AGENT_SEED_4 in .env")
if not ELEVENLABS_API_KEY:
    raise SystemExit("Set ELEVENLABS_API_KEY in .env")
if not GEMINI_API_KEY and not ASI_API_KEY:
    raise SystemExit("Set at least one of GEMINI_API_KEY or ASI_API_KEY in .env")

if USE_CLOUDINARY:
    import cloudinary
    import cloudinary.uploader
    CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
    CLOUDINARY_API_KEY_C = os.getenv("CLOUDINARY_API_KEY")
    CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")
    if not (CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY_C and CLOUDINARY_API_SECRET):
        raise SystemExit("Set CLOUDINARY_* vars in .env or set Set_cloudinary=false")
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY_C,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True,
    )

if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
else:
    gemini_client = None

eleven_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

narration_agent = Agent(
    name="braillemap_narration_agent",
    seed=AGENT_SEED_4,
    port=AGENT_PORT_4,
    endpoint=[f"http://localhost:{AGENT_PORT_4}/submit"],
)


# ── Prompt construction ──────────────────────────────────────────────────────
# Spatial-wording helpers live in `layout_brief.py` so the FastAPI backend can
# import them without dragging in google-genai / elevenlabs / uagents.

from layout_brief import (  # noqa: E402  (kept after stdlib imports for clarity)
    format_object_lines,
    orient_relative_to_entrance as _orient_relative_to_entrance,
    resolve_space_label,
)


def build_narration_prompt(layout: Dict[str, Any], metadata: Dict[str, Any]) -> str:
    room_w = float(layout.get("room_width") or 0.0)
    room_d = float(layout.get("room_depth") or 0.0)
    _, rel_objects = _orient_relative_to_entrance(layout)
    object_lines = format_object_lines(rel_objects)

    is_floorplan = layout.get("source") == "floorplan"
    space_type = (layout.get("space_type") or "").strip()
    space_name = (layout.get("space_name") or "").strip()
    metadata_room = metadata.get("room_name", "room")
    metadata_building = metadata.get("building_name", "")

    # For floor plans, prefer the AI-derived space label ("mall", "subway station", …)
    # and adapt the framing — a city block walkthrough is not a room walkthrough.
    if is_floorplan:
        space_label = space_name or space_type or metadata_room or "space"
        location_phrase = (
            f"{space_label} in {metadata_building}" if metadata_building else space_label
        )
        word_cap = 220 if (space_type and space_type != "room") else 170
        return f"""You are generating a spoken orientation for a blind person who has the tactile Braille map of a {space_label} in front of them.

Source: this layout came from an uploaded floor plan, not a 3D scan, so describe it as an overview of the {space_label} rather than a first-person room walkthrough.

Overall extent: {room_w:.1f} m wide by {room_d:.1f} m deep.
Reference origin is the main entrance — "forward" means deeper into the {space_label}, "left" and "right" are sideways from that heading.

Landmarks and zones (relative to the entrance, facing in):
{object_lines}

Write a calm, clear narration of this {location_phrase}. Open with "Welcome. This is a Braille map of {location_phrase}…". Briefly state overall size, then describe the main zones, corridors, and landmarks using "ahead", "to the left/right", "near the entrance", "at the far end", with approximate distances in meters. Group nearby items naturally. Keep it under {word_cap} words. Do not mention raw coordinates, indices, or brackets — only natural directions. No preamble or sign-off beyond the narration itself."""

    # Original room-scan path (LiDAR scan): keep the first-person walkthrough framing.
    return f"""You are generating a spoken walkthrough for a blind person entering an unfamiliar {metadata_room}.
You are standing at the entrance, facing into the room. "Forward" means deeper into the room, "left" and "right" are sideways from that heading.

Room dimensions: {room_w:.1f} m wide by {room_d:.1f} m deep.

Objects (relative to you at the entrance, facing in):
{object_lines}

Write a calm, clear walkthrough of this room. Start with "Welcome. You are entering a {metadata_room}…". Describe dimensions, then walk the listener through the main objects using "ahead of you", "to your left/right", with distances in meters. Group nearby objects when natural. Keep it under 150 words. Do not mention coordinates or brackets — only natural directions. Do not add any preamble or sign-off beyond the walkthrough itself."""


# ── LLM calls ────────────────────────────────────────────────────────────────

def generate_with_asi1(prompt: str) -> str:
    if not ASI_API_KEY:
        raise RuntimeError("ASI_API_KEY not set")
    resp = requests.post(
        ASI_API_URL,
        headers={
            "Authorization": f"Bearer {ASI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": ASI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 320,
        },
        timeout=45,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def generate_with_gemini(prompt: str) -> str:
    if not gemini_client:
        raise RuntimeError("GEMINI_API_KEY not set")
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt
    )
    return (getattr(response, "text", "") or "").strip()


def generate_narration(prompt: str, ctx: Context) -> Tuple[str, str]:
    """Returns (text, provider_used). ASI1-Mini first, Gemini fallback."""
    if ASI_API_KEY:
        try:
            text = generate_with_asi1(prompt)
            if text:
                ctx.logger.info("narration generated with ASI1-Mini")
                return text, "asi1-mini"
        except Exception as exc:
            ctx.logger.warning(f"ASI1-Mini failed ({exc}); falling back to Gemini")
    text = generate_with_gemini(prompt)
    ctx.logger.info("narration generated with Gemini (fallback)")
    return text, f"gemini:{GEMINI_MODEL}"


# ── ElevenLabs + Cloudinary ──────────────────────────────────────────────────

def synthesize_mp3(text: str, room_id: str) -> str:
    audio_iter = eleven_client.text_to_speech.convert(
        text=text,
        voice_id=ELEVENLABS_VOICE_ID,
        model_id=ELEVENLABS_MODEL,
        output_format="mp3_44100_128",
    )
    path = os.path.join(OUTPUT_DIR, f"narration_{room_id}.mp3")
    with open(path, "wb") as f:
        for chunk in audio_iter:
            if chunk:
                f.write(chunk)
    return path


def upload_mp3(path: str, room_id: str) -> str:
    """Upload to Cloudinary if enabled, otherwise serve locally."""
    if USE_CLOUDINARY:
        result = cloudinary.uploader.upload(
            path,
            resource_type="video",  # Cloudinary groups audio under 'video'
            folder="braillemap/audio",
            public_id=f"narration_{room_id}",
            overwrite=True,
            use_filename=False,
            unique_filename=False,
        )
        return result["secure_url"]
    filename = os.path.basename(path)
    return f"{BACKEND_URL}/files/audio/{filename}"


# ── Backend I/O ──────────────────────────────────────────────────────────────

def fetch_room_full(room_id: str) -> Dict[str, Any]:
    resp = requests.get(f"{BACKEND_URL}/rooms/{room_id}/full", timeout=30)
    resp.raise_for_status()
    return resp.json()


def patch_room(room_id: str, updates: Dict[str, Any]) -> None:
    resp = requests.patch(f"{BACKEND_URL}/rooms/{room_id}", json=updates, timeout=30)
    resp.raise_for_status()


# ── Message handler ──────────────────────────────────────────────────────────

@narration_agent.on_message(model=NarrationRequest)
async def on_narration_request(
    ctx: Context, sender: str, msg: NarrationRequest
) -> None:
    room_id = msg.room_id
    ctx.logger.info(f"[msg] NarrationRequest from {sender} room={room_id}")

    try:
        room = fetch_room_full(room_id)
    except Exception as exc:
        ctx.logger.error(f"failed to fetch room {room_id}: {exc}")
        return

    layout = room.get("layout_2d") or {}
    if not layout:
        ctx.logger.error(f"room {room_id} has no layout_2d — run Agent 1 first")
        return
    metadata = room.get("metadata") or {}

    prompt = build_narration_prompt(layout, metadata)
    try:
        text, provider = generate_narration(prompt, ctx)
    except Exception as exc:
        ctx.logger.error(f"narration generation failed: {exc}")
        patch_room(room_id, {"status": "error_narration_llm", "narration_error": str(exc)})
        return
    if not text:
        ctx.logger.error("narration text is empty")
        patch_room(room_id, {"status": "error_empty_narration"})
        return

    ctx.logger.info(f"narration ({len(text)} chars): {text[:120]}…")

    try:
        mp3_path = synthesize_mp3(text, room_id)
    except Exception as exc:
        ctx.logger.error(f"ElevenLabs synthesis failed: {exc}")
        patch_room(room_id, {
            "narration_text": text,
            "narration_provider": provider,
            "status": "error_tts",
            "tts_error": str(exc),
        })
        return

    try:
        audio_url = upload_mp3(mp3_path, room_id)
    except Exception as exc:
        ctx.logger.error(f"Cloudinary upload failed: {exc}")
        patch_room(room_id, {
            "narration_text": text,
            "narration_provider": provider,
            "status": "error_cloudinary_audio",
        })
        return

    patch_room(room_id, {
        "audio_url": audio_url,
        "narration_text": text,
        "narration_provider": provider,
        "status_narration_done": True,
    })
    ctx.logger.info(f"✓ Audio uploaded: {audio_url}")


if __name__ == "__main__":
    print("═" * 60)
    print(" BrailleMap Narration Agent (Agent 4)")
    print(f" Address       : {narration_agent.address}")
    print(f" Port          : {AGENT_PORT_4}")
    print(f" ASI endpoint  : {ASI_API_URL} (model={ASI_MODEL})")
    print(f" Gemini fallback: {GEMINI_MODEL}")
    print(f" ElevenLabs    : voice={ELEVENLABS_VOICE_ID}, model={ELEVENLABS_MODEL}")
    print(f" Storage       : {'Cloudinary' if USE_CLOUDINARY else 'local → ' + OUTPUT_DIR}")
    print("═" * 60)
    narration_agent.run()
