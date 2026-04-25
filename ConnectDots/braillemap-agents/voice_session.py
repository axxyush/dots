"""Voice-session helper for the "Talk to the Agent" feature.

Called by the FastAPI backend's `POST /rooms/{room_id}/voice_session` route
to mint a short-lived ElevenLabs Conversational AI conversation token and
build the per-session prompt overrides (spatial brief + first message + voice).

The ElevenLabs API key never leaves the server — iOS only sees the token and
the override payload it should pass into `ElevenLabs.startConversation`.
"""

from __future__ import annotations

import os
from typing import Any, Dict

import requests
from dotenv import load_dotenv

from layout_brief import (
    format_object_lines,
    orient_relative_to_entrance,
    resolve_space_label,
)

load_dotenv()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_TOKEN_URL = "https://api.elevenlabs.io/v1/convai/conversation/token"


SYSTEM_PROMPT_TEMPLATE = """You are BrailleMap's voice guide. The user is blind and is holding a tactile Braille map of {space_label} that was just generated from a floor plan they uploaded.

Frame of reference: the user is standing at the main entrance, facing into the {space_label}. "Forward" or "ahead" means deeper into the space. "Left" and "right" are sideways from that heading. Distances are in meters.

You do NOT know the user's live position — only that they are at the entrance, facing in. Never invent a different location for them. If they ask "what's behind me", explain that behind them is the entrance / outside.

Overall extent: {room_w:.1f} m wide by {room_d:.1f} m deep.

Spatial brief (every object, relative to the entrance, facing in):
{layout_brief}

Answer questions about: counts ("how many tables"), what's in a direction ("what's in front of me", "on my left", "10 meters ahead"), the nearest object, and overall layout. Use natural directions and approximate meters — never coordinates, indices, or brackets. Keep replies under 3 short sentences unless the user explicitly asks you to describe the space in full. If the brief cannot answer a question, say so plainly instead of guessing."""


def build_layout_brief(layout: Dict[str, Any]) -> str:
    """Return the bullet list of objects in entrance-relative coordinates."""
    _, rel_objects = orient_relative_to_entrance(layout)
    return format_object_lines(rel_objects)


def build_system_prompt(layout: Dict[str, Any], metadata: Dict[str, Any]) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        space_label=resolve_space_label(layout, metadata),
        room_w=float(layout.get("room_width") or 0.0),
        room_d=float(layout.get("room_depth") or 0.0),
        layout_brief=build_layout_brief(layout),
    )


def build_first_message(layout: Dict[str, Any], metadata: Dict[str, Any]) -> str:
    label = resolve_space_label(layout, metadata)
    return (
        f"Hi, I'm your BrailleMap guide. I can answer questions about the "
        f"{label} you just uploaded. What would you like to know?"
    )


def mint_conversation_token(agent_id: str) -> str:
    """Hit the ElevenLabs token endpoint with our server-side API key.

    The returned token is what the iOS SDK passes to
    `ElevenLabs.startConversation(conversationToken: …)`. Tokens are short-lived,
    so iOS should mint a fresh one for each conversation attempt.
    """
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY not set in environment")

    resp = requests.get(
        ELEVENLABS_TOKEN_URL,
        params={"agent_id": agent_id},
        headers={"xi-api-key": ELEVENLABS_API_KEY},
        timeout=10,
    )
    if not resp.ok:
        raise RuntimeError(
            f"ElevenLabs token mint failed: {resp.status_code} {resp.text[:200]}"
        )
    body = resp.json()
    # ElevenLabs returns either {"token": "..."} or {"conversation_token": "..."}
    # depending on API version; accept whichever is present.
    token = body.get("token") or body.get("conversation_token")
    if not token:
        raise RuntimeError(f"ElevenLabs token response missing 'token': {body}")
    return token


def fetch_conversations(agent_id: str) -> list[Dict[str, Any]]:
    """Fetch the list of recent conversations for this agent."""
    if not ELEVENLABS_API_KEY:
        return []
    
    url = "https://api.elevenlabs.io/v1/convai/conversations"
    resp = requests.get(
        url,
        params={"agent_id": agent_id},
        headers={"xi-api-key": ELEVENLABS_API_KEY},
        timeout=10,
    )
    if not resp.ok:
        return []
    return resp.json().get("conversations", [])


def fetch_conversation_transcript(conversation_id: str) -> Dict[str, Any]:
    """Fetch the full transcript and metadata for a specific conversation."""
    if not ELEVENLABS_API_KEY:
        return {}
        
    url = f"https://api.elevenlabs.io/v1/convai/conversations/{conversation_id}"
    resp = requests.get(
        url,
        headers={"xi-api-key": ELEVENLABS_API_KEY},
        timeout=10,
    )
    if not resp.ok:
        return {}
    return resp.json()
