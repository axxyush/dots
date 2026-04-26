"""Wayfind — Fetch.ai chat agent that lets a blind visitor ask voice questions
about a venue whose floor plan is owned by the SenseGrid agent.

Flow per chat session
---------------------
  1. User (via ASI:One voice mode) sends the venue id printed at the entrance
     ("venue-7f2a01"). We send a VenueLookup to SenseGrid and cache the
     returned scene + metric frame in ctx.storage.
  2. User starts at the entrance facing into the room. Each subsequent
     message may include movement updates ("I moved 5 steps forward",
     "turned 90 left") which we apply deterministically BEFORE asking the
     LLM. The LLM never gets to lie about position.
  3. We hand the cached scene + current pose to ASI:One and return its
     answer. Replies are short, spoken-friendly text.

Env
---
  - ASI_API_KEY (required) — ASI:One LLM credential.
  - SENSEGRID_AGENT_ADDRESS (required) — the address printed by SenseGrid
    on startup so this agent can reach its VenueLookup protocol.
  - WAYFIND_AGENT_SEED — stable seed phrase.
  - ASI_API_URL, ASI_MODEL — optional overrides.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(_REPO_ROOT / ".env", override=False)
except ImportError:
    pass

import requests
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    TextContent,
    chat_protocol_spec,
)

from agents.wayfind_protocol import VenueLookup, VenueInfo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger("wayfind.agent")


def _ensure_event_loop() -> None:
    try:
        asyncio.get_running_loop()
        return
    except RuntimeError:
        pass
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


_ensure_event_loop()


# ── Config ────────────────────────────────────────────────────────────────────

AGENT_SEED = os.environ.get("WAYFIND_AGENT_SEED", "wayfind-seed-phrase")
SENSEGRID_AGENT_ADDRESS = os.environ.get("SENSEGRID_AGENT_ADDRESS", "").strip()

ASI_API_URL = os.environ.get("ASI_API_URL", "https://api.asi1.ai/v1/chat/completions")
ASI_API_KEY = os.environ.get("ASI_API_KEY", "").strip()
ASI_MODEL = os.environ.get("ASI_MODEL", "asi1-mini")

LOOKUP_TIMEOUT_S = 20.0
SESSION_TTL_S = 60 * 60  # 1 hour of idle keeps the cached venue


# ── Session helpers ──────────────────────────────────────────────────────────

VENUE_ID_REGEX = re.compile(r"venue-[0-9a-f]{4,}", re.IGNORECASE)
_MENTION_REGEX = re.compile(r"@\S+")

# Movement parsing — applied deterministically before the LLM call.
_MOVE_REGEX = re.compile(
    r"(?:i\s+)?(?:just\s+)?(?:moved|walked|went|stepped|took)\s+"
    r"(\d+)\s*(?:steps?|metres?|meters?|m)?\s*"
    r"(?:to\s+(?:the|my)\s+)?(forward|forwards?|ahead|back|backward|backwards?|left|right)",
    re.IGNORECASE,
)
_TURN_REGEX = re.compile(
    r"(?:i\s+)?(?:just\s+)?turned\s+(\d+)\s*(?:degrees?|deg)?\s*(?:to\s+(?:the|my)\s+)?(left|right)",
    re.IGNORECASE,
)
_TURN_AROUND_REGEX = re.compile(r"\bturn(?:ed)?\s+around\b", re.IGNORECASE)


def _normalize(text: str) -> str:
    return _MENTION_REGEX.sub("", text).strip()


def _session_key(sender: str) -> str:
    return f"wf:{sender}"


def _pending_key(venue_id: str) -> str:
    return f"wfpend:{venue_id}"


def _load_session(ctx: Context, sender: str) -> dict:
    raw = ctx.storage.get(_session_key(sender))
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}


def _save_session(ctx: Context, sender: str, sess: dict) -> None:
    sess["updated_at"] = time.time()
    ctx.storage.set(_session_key(sender), json.dumps(sess))


def _clear_session(ctx: Context, sender: str) -> None:
    ctx.storage.set(_session_key(sender), json.dumps({}))


def _waiters_for(ctx: Context, venue_id: str) -> list[str]:
    raw = ctx.storage.get(_pending_key(venue_id))
    if not raw:
        return []
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return [str(s) for s in v] if isinstance(v, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _set_waiters(ctx: Context, venue_id: str, waiters: list[str]) -> None:
    ctx.storage.set(_pending_key(venue_id), json.dumps(waiters))


# ── Pose math ────────────────────────────────────────────────────────────────


def _apply_movement(text: str, sess: dict) -> tuple[bool, list[str]]:
    """Update sess pose in place from movement phrases in ``text``. Returns
    (changed, notes) where notes are short status snippets to surface to the
    user (e.g. "you would walk into a wall, ignored")."""
    changed = False
    notes: list[str] = []

    width = float(sess.get("room_width_m") or 0.0)
    depth = float(sess.get("room_height_m") or 0.0)

    for m in _TURN_REGEX.finditer(text):
        deg = int(m.group(1))
        direction = m.group(2).lower()
        sign = 1.0 if direction == "left" else -1.0
        sess["heading_deg"] = (float(sess.get("heading_deg", 90.0)) + sign * deg) % 360.0
        changed = True

    if _TURN_AROUND_REGEX.search(text):
        sess["heading_deg"] = (float(sess.get("heading_deg", 90.0)) + 180.0) % 360.0
        changed = True

    for m in _MOVE_REGEX.finditer(text):
        steps = float(m.group(1))
        direction = m.group(2).lower()
        heading = float(sess.get("heading_deg", 90.0))
        if direction.startswith("forward") or direction == "ahead":
            bearing = heading
        elif direction.startswith("back"):
            bearing = heading + 180.0
        elif direction == "left":
            bearing = heading + 90.0
        else:  # right
            bearing = heading - 90.0
        rad = math.radians(bearing % 360.0)
        new_x = float(sess.get("pos_x_m", 0.0)) + steps * math.cos(rad)
        new_y = float(sess.get("pos_y_m", 0.0)) + steps * math.sin(rad)

        clamped_x = max(0.0, min(width, new_x)) if width > 0 else new_x
        clamped_y = max(0.0, min(depth, new_y)) if depth > 0 else new_y
        if (abs(clamped_x - new_x) > 0.05 or abs(clamped_y - new_y) > 0.05):
            notes.append(
                "Heads up — that path runs into a wall, so I have you stopped at the edge."
            )
        sess["pos_x_m"] = clamped_x
        sess["pos_y_m"] = clamped_y
        changed = True

    return changed, notes


def _heading_label(deg: float) -> str:
    deg = deg % 360.0
    if 45.0 <= deg < 135.0:
        return "forward (toward the back of the room)"
    if 135.0 <= deg < 225.0:
        return "to your left along the front wall"
    if 225.0 <= deg < 315.0:
        return "back toward the entrance"
    return "to your right along the front wall"


# ── ASI:One LLM call ─────────────────────────────────────────────────────────


SYSTEM_PROMPT_TMPL = """You are Wayfind, a voice-only navigation guide for a blind visitor inside a venue.

Venue scene (floor-plan JSON; objects use coordinate_system="normalized_0_to_100" where x and y are 0..100 and (0,0) is the image's top-left, y grows DOWN):
{scene_json}

The visitor's metric frame (origin = entrance, +x = right along the front wall, +y = forward into the room, units = meters; 1 step ≈ 1 meter):
- room width:  {room_width_m:.1f} m
- room depth:  {room_height_m:.1f} m
- visitor at: ({pos_x_m:.1f}, {pos_y_m:.1f}) m
- visitor facing: {heading_label} ({heading_deg:.0f}°, where 90° is straight into the room)

To place a scene object on the metric grid, convert (nx, ny) → ((nx/100) * room width, (1 - ny/100) * room depth).

Reply rules — these answers will be SPOKEN ALOUD by a screen reader:
- Reply in plain prose, at most two short sentences. No markdown, no bullet lists, no URLs, no code.
- Use clock positions ("at your two o'clock") and step counts ("about five steps") rather than coordinates.
- Lead with the headline number when the visitor asks "how many".
- If the visitor asks where they are or what's near, name the closest scene object and its rough direction.
- If the data doesn't answer the question, say so plainly. Do not invent rooms, exits, or objects that are not in the scene JSON.
"""


def _build_system_prompt(sess: dict) -> str:
    return SYSTEM_PROMPT_TMPL.format(
        scene_json=sess.get("scene_json", "{}"),
        room_width_m=float(sess.get("room_width_m") or 0.0),
        room_height_m=float(sess.get("room_height_m") or 0.0),
        pos_x_m=float(sess.get("pos_x_m") or 0.0),
        pos_y_m=float(sess.get("pos_y_m") or 0.0),
        heading_deg=float(sess.get("heading_deg") or 90.0),
        heading_label=_heading_label(float(sess.get("heading_deg") or 90.0)),
    )


def _ask_llm(sess: dict, user_text: str) -> str:
    if not ASI_API_KEY:
        return "Sorry — the navigation language model isn't configured (missing ASI_API_KEY)."

    history = sess.get("history") or []
    messages = [{"role": "system", "content": _build_system_prompt(sess)}]
    for m in history[-6:]:
        if isinstance(m, dict) and m.get("role") in {"user", "assistant"} and isinstance(m.get("content"), str):
            messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": user_text})

    try:
        resp = requests.post(
            ASI_API_URL,
            headers={"Authorization": f"Bearer {ASI_API_KEY}", "Content-Type": "application/json"},
            json={"model": ASI_MODEL, "messages": messages, "temperature": 0.2, "max_tokens": 220},
            timeout=45,
        )
    except Exception as exc:
        log.exception("ASI:One request failed")
        return f"I couldn't reach the language model right now: {exc}."

    if not resp.ok:
        return f"The language model returned an error ({resp.status_code}). Please try again."

    try:
        reply = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return "I got an unexpected response from the language model."
    return reply or "I'm not sure how to answer that."


# ── uagents wiring ───────────────────────────────────────────────────────────

agent = Agent(
    name="wayfind",
    seed=AGENT_SEED,
    port=8003,
    mailbox=True,
    handle="wayfind",
    description="Voice-mode navigation guide that helps blind visitors explore an indoor venue.",
    readme_path=str(Path(__file__).resolve().parent / "WAYFIND_README.md"),
    publish_agent_details=True,
)

chat_protocol = Protocol(spec=chat_protocol_spec)
wayfind_protocol = Protocol(name="wayfind-venue", version="0.1.0")


def _reply_msg(text: str) -> ChatMessage:
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[
            TextContent(type="text", text=text),
            EndSessionContent(type="end-session"),
        ],
    )


def _hold_msg(text: str) -> ChatMessage:
    """Reply that does NOT close the session — used while we wait on lookup."""
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[TextContent(type="text", text=text)],
    )


@chat_protocol.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc),
            acknowledged_msg_id=msg.msg_id,
        ),
    )

    user_text = "".join(item.text for item in msg.content if isinstance(item, TextContent)).strip()
    if not user_text:
        await ctx.send(sender, _reply_msg("I didn't catch that — could you say it again?"))
        return

    text = _normalize(user_text)
    ctx.logger.info("chat from %s: %r", sender[:12] + "…", text[:160])

    sess = _load_session(ctx, sender)

    # Expire stale sessions to force re-entering venue id.
    if sess and sess.get("updated_at") and time.time() - float(sess["updated_at"]) > SESSION_TTL_S:
        _clear_session(ctx, sender)
        sess = {}

    # No venue cached yet → expect a venue id.
    if not sess.get("venue_loaded"):
        match = VENUE_ID_REGEX.search(text)
        if not match:
            await ctx.send(
                sender,
                _reply_msg(
                    "Welcome. Please say or send the venue ID posted at the entrance — "
                    "it looks like venue dash followed by six characters."
                ),
            )
            return

        venue_id = match.group(0).lower()

        if not SENSEGRID_AGENT_ADDRESS:
            await ctx.send(
                sender,
                _reply_msg(
                    "Sorry — I'm not connected to the venue directory right now. "
                    "The operator needs to set the SENSEGRID_AGENT_ADDRESS variable."
                ),
            )
            return

        # Park this user in pending and dispatch the lookup.
        waiters = _waiters_for(ctx, venue_id)
        if sender not in waiters:
            waiters.append(sender)
            _set_waiters(ctx, venue_id, waiters)

        sess.update({"awaiting_venue_id": venue_id, "venue_loaded": False})
        _save_session(ctx, sender, sess)

        try:
            await ctx.send(SENSEGRID_AGENT_ADDRESS, VenueLookup(venue_id=venue_id))
        except Exception as exc:
            ctx.logger.exception("VenueLookup send failed")
            _clear_session(ctx, sender)
            await ctx.send(
                sender,
                _reply_msg(f"I couldn't reach the venue directory: {exc}."),
            )
            return

        await ctx.send(
            sender,
            _hold_msg(f"Looking up {venue_id}. One moment."),
        )
        return

    # Venue cached → apply movement, then ask the LLM.
    changed, notes = _apply_movement(text, sess)
    answer = _ask_llm(sess, text)

    history = sess.get("history") or []
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": answer})
    sess["history"] = history[-12:]
    _save_session(ctx, sender, sess)

    if notes:
        answer = " ".join(notes) + " " + answer

    await ctx.send(sender, _reply_msg(answer))


@chat_protocol.on_message(ChatAcknowledgement)
async def on_chat_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.debug("chat ack from %s", sender[:12] + "…")


@wayfind_protocol.on_message(VenueInfo)
async def on_venue_info(ctx: Context, sender: str, msg: VenueInfo):
    venue_id = (msg.venue_id or "").strip().lower()
    waiters = _waiters_for(ctx, venue_id)
    if not waiters:
        ctx.logger.warning("VenueInfo for %s arrived with no waiters", venue_id)
        return

    if not msg.found:
        for user in waiters:
            sess = _load_session(ctx, user)
            sess.pop("awaiting_venue_id", None)
            _save_session(ctx, user, sess)
            err = (msg.error or "venue not found").strip() or "venue not found"
            await ctx.send(
                user,
                _reply_msg(
                    f"I couldn't find venue {venue_id}: {err}. Please double-check the ID and try again."
                ),
            )
        _set_waiters(ctx, venue_id, [])
        return

    for user in waiters:
        sess = _load_session(ctx, user)
        sess.update(
            {
                "venue_loaded": True,
                "venue_id": msg.venue_id,
                "venue_label": msg.venue_label,
                "scene_json": msg.scene_json,
                "room_width_m": float(msg.room_width_m),
                "room_height_m": float(msg.room_height_m),
                "pos_x_m": float(msg.entrance_x_m),
                "pos_y_m": float(msg.entrance_y_m),
                "heading_deg": float(msg.entrance_heading_deg),
                "history": [],
            }
        )
        sess.pop("awaiting_venue_id", None)
        _save_session(ctx, user, sess)

        label = msg.venue_label or msg.venue_id
        await ctx.send(
            user,
            _reply_msg(
                f"You're at the entrance of {label}. The room is about "
                f"{msg.room_width_m:.0f} meters wide and {msg.room_height_m:.0f} meters deep, "
                f"and you're facing into it. Ask me anything — how many exits, where the counter "
                f"is, or tell me when you've moved or turned and I'll keep track."
            ),
        )

    _set_waiters(ctx, venue_id, [])


agent.include(chat_protocol, publish_manifest=True)
agent.include(wayfind_protocol, publish_manifest=True)


if __name__ == "__main__":
    log.info("Wayfind starting.")
    log.info("Wayfind agent address: %s", agent.address)
    if not SENSEGRID_AGENT_ADDRESS:
        log.warning("SENSEGRID_AGENT_ADDRESS is not set — venue lookups will fail.")
    if not ASI_API_KEY:
        log.warning("ASI_API_KEY is not set — LLM calls will fail.")
    agent.run()
