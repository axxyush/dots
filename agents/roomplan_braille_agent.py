"""RoomPlanBraille — Fetch.ai chat agent: room photos → tactile map (Nano Banana Pro).

Queue design:
  - Every message is checked for images FIRST, before any intent is evaluated.
  - Images are stored as references (https:// URLs or local paths for data: URIs).
  - https:// URLs are queued as-is — Gemini fetches them directly at generation time,
    so nothing is downloaded to disk during queuing.
  - When the user says 'generate' / 'done' / 'go' etc., ALL queued image refs are
    sent to Gemini in one call so it synthesises a single coherent tactile map.
  - 'clear' empties the queue.  'status' shows how many refs are queued.

Env:
  GEMINI_API_KEY               (required)
  ROOMPLAN_BRAILLE_AGENT_SEED  (stable seed phrase)

Run:
  .venv/bin/python agents/roomplan_braille_agent.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sys
import tempfile
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

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    AgentContent,
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    Resource,
    ResourceContent,
    TextContent,
    chat_protocol_spec,
)

from agents.tools import dispatch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("roomplan_braille.agent")


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

AGENT_SEED = os.environ.get("ROOMPLAN_BRAILLE_AGENT_SEED", "roomplan-braille-seed")

ARTIFACT_DIR = Path(tempfile.gettempdir()) / "roomplan_braille_artifacts"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

URL_REGEX = re.compile(r"https?://[^\s<>\"]+")
_MENTION_REGEX = re.compile(r"@\S+")
_TRAILING_PUNCT = ".,;:!?)\"']"

_GENERATE = {
    "generate", "go", "done", "ready", "process",
    "make", "create", "build", "run", "start",
    "make map", "create map", "generate map", "make it",
}
_CLEAR = {"clear", "reset", "restart", "empty", "flush"}
_STATUS = {"status", "how many"}
_HI = {"hi", "hello", "hey", "yo", "sup", "hola"}
_HELP = {"help", "/help", "?", "instructions", "usage"}

_QUEUE_KEY = "img_queue"

_HELP_TEXT = (
    "Send room photos and I'll build a tactile accessibility map from all of them together.\n\n"
    "**How it works:**\n"
    "1. Attach photos (or paste http/https URLs) — each message adds to your queue.\n"
    "2. Say **'generate'** (or 'go', 'done', 'make map') when you've sent all your photos.\n"
    "3. All queued photos are sent to Gemini together so it can see the whole room.\n\n"
    "Other commands: **status** — see queue size · **clear** — empty the queue."
)


def _normalize(text: str) -> str:
    return _MENTION_REGEX.sub("", text).strip()


def _extract_urls(text: str) -> list[str]:
    urls = []
    for raw in URL_REGEX.findall(text):
        while raw and raw[-1] in _TRAILING_PUNCT:
            raw = raw[:-1]
        if raw:
            urls.append(raw)
    out, seen = [], set()
    for u in urls:
        if u not in seen:
            out.append(u)
            seen.add(u)
    return out


def _intent(text: str) -> str:
    """Classify text intent. Images are always collected before this is called."""
    low = text.lower().strip().rstrip(".,!?")
    words = set(re.findall(r"[a-z]+", low))
    if not low:
        return "other"
    if low in _HELP or words & _HELP:
        return "help"
    if low in _HI or words & _HI:
        return "hi"
    if low in _STATUS or words & _STATUS:
        return "status"
    if low in _CLEAR or words & _CLEAR:
        return "clear"
    if low in _GENERATE or words & _GENERATE:
        return "generate"
    return "other"


# ── Queue helpers ─────────────────────────────────────────────────────────────

def _load_queue(ctx, sender: str) -> list[str]:
    raw = ctx.storage.get(f"{_QUEUE_KEY}:{sender}")
    if not raw:
        return []
    try:
        q = json.loads(raw) if isinstance(raw, str) else raw
        return [r for r in q if isinstance(r, str)]
    except Exception:
        return []


def _save_queue(ctx, sender: str, queue: list[str]) -> None:
    ctx.storage.set(f"{_QUEUE_KEY}:{sender}", json.dumps(queue))


def _clear_queue(ctx, sender: str) -> None:
    ctx.storage.set(f"{_QUEUE_KEY}:{sender}", json.dumps([]))


# ── Image ref collection ──────────────────────────────────────────────────────

def _save_data_uri(uri: str, index: int) -> str | None:
    """Decode a data: URI to a local temp file. Returns path or None."""
    try:
        header, encoded = uri.split(",", 1)
        mime = header.split(":")[1].split(";")[0] if ":" in header else "image/jpeg"
        ext = "." + mime.split("/")[-1].replace("jpeg", "jpg")
        out = ARTIFACT_DIR / f"room_{index}_{uuid4().hex[:8]}{ext}"
        out.write_bytes(base64.b64decode(encoded))
        return str(out)
    except Exception as exc:
        log.warning("failed to decode data URI #%d: %s", index, exc)
        return None


def _collect_image_refs(msg: ChatMessage) -> tuple[list[str], list[str]]:
    """
    Returns (refs, errors).

    refs is a list of image references to add to the queue:
      - https:// URLs from ResourceContent or text  → stored as-is, Gemini fetches later
      - data: URIs from ResourceContent             → decoded to a local temp file path

    Nothing is downloaded during this step for https:// sources.
    """
    refs: list[str] = []
    errors: list[str] = []

    for i, item in enumerate(msg.content):
        if not isinstance(item, ResourceContent):
            continue
        resources = item.resource if isinstance(item.resource, list) else [item.resource]
        for res in resources:
            uri = res.uri
            if uri.startswith("https://") or uri.startswith("http://"):
                # Queue the URL directly — no download needed.
                refs.append(uri)
                log.info("queued image URL from attachment #%d: %s…", i, uri[:60])
            elif uri.startswith("data:"):
                # Inline base64 image — decode to disk once, store path.
                path = _save_data_uri(uri, i)
                if path:
                    refs.append(path)
                    log.info("decoded inline attachment #%d → %s", i, path)
                else:
                    errors.append(f"Could not decode attached image #{i + 1}.")
            else:
                log.warning("unknown URI scheme for attachment #%d: %s", i, uri[:40])
                errors.append(f"Unsupported image format for attachment #{i + 1}.")

    # Fall back to URLs in text only when no ResourceContent images were found.
    if not refs:
        text = "".join(item.text for item in msg.content if isinstance(item, TextContent))
        for url in _extract_urls(text):
            refs.append(url)
            log.info("queued image URL from text: %s…", url[:60])

    return refs, errors


# ── Generation ────────────────────────────────────────────────────────────────

def _do_generate(
    ctx, sender: str, queue: list[str], extra_errors: list[str]
) -> tuple[str, list[AgentContent]]:
    """Send all queued image refs to Gemini, clear queue, return reply + ResourceContent."""
    no_extra: list[AgentContent] = []

    if not queue:
        return (
            "Nothing in the queue yet. "
            "Attach room photos first, then say 'generate'.",
            no_extra,
        )

    n = len(queue)
    log.info("generating tactile map from %d image ref(s) for %s", n, sender[:16])

    t = dispatch(
        "tactile_map_from_images_nanobanana",
        {"image_sources": queue, "model": "gemini-3-pro-image-preview"},
    )
    if "error" in t:
        return f"Tactile map generation failed: {t['error']}", no_extra

    up = dispatch("upload_artifact", {"file_path": t["png_path"]})
    if "error" in up:
        return f"Generation succeeded but upload failed: {up['error']}", no_extra

    _clear_queue(ctx, sender)

    url = up["url"]
    plural = "s" if n != 1 else ""
    reply = (
        f"Here's your tactile map — synthesised from {n} photo{plural}.\n\n"
        f"Download: {url}"
    )
    if extra_errors:
        reply += "\n\nWarnings:\n" + "\n".join(f"- {e}" for e in extra_errors)

    resource_item = ResourceContent(
        resource_id=uuid4(),
        resource=Resource(
            uri=url,
            metadata={"mime_type": "image/png", "role": "tactile_map"},
        ),
    )
    return reply, [resource_item]


# ── Main chat handler ─────────────────────────────────────────────────────────

def handle_chat(
    ctx, sender: str, msg: ChatMessage
) -> tuple[str, list[AgentContent]]:
    """
    Images are ALWAYS collected first, before any intent check, so attachments
    are never lost to intent mis-classification.
    """
    no_extra: list[AgentContent] = []

    # ── Step 1: collect image refs from this message and add to queue ─────────
    new_refs, collect_errors = _collect_image_refs(msg)
    if new_refs:
        queue = _load_queue(ctx, sender) + new_refs
        _save_queue(ctx, sender, queue)
        log.info(
            "queued %d new ref(s) for %s — total %d",
            len(new_refs), sender[:16], len(queue),
        )
    else:
        queue = _load_queue(ctx, sender)

    # ── Step 2: classify text intent ─────────────────────────────────────────
    user_text = _normalize(
        "".join(item.text for item in msg.content if isinstance(item, TextContent))
    )
    intent = _intent(user_text)

    # ── Step 3: if new images arrived, acknowledge them ───────────────────────
    if new_refs:
        n_new = len(new_refs)
        n_total = len(queue)
        s_new = "s" if n_new != 1 else ""
        s_total = "s" if n_total != 1 else ""

        # Generate immediately if they also typed the trigger word.
        if intent == "generate":
            return _do_generate(ctx, sender, queue, collect_errors)

        reply = (
            f"Added {n_new} photo{s_new}. "
            f"Queue now has **{n_total} image{s_total}**.\n\n"
            "Send more photos or say **'generate'** to create the tactile map."
        )
        if collect_errors:
            reply += "\n\nWarnings:\n" + "\n".join(f"- {e}" for e in collect_errors)
        return reply, no_extra

    # ── Step 4: no new images — handle text intent ────────────────────────────
    if intent == "help":
        return _HELP_TEXT, no_extra

    if intent == "hi":
        return (
            "Hi! Attach room photos and I'll generate a tactile map. "
            "Say `help` for instructions.",
            no_extra,
        )

    if intent == "status":
        n = len(queue)
        if n == 0:
            return "Your queue is empty. Attach room photos to get started.", no_extra
        return (
            f"You have **{n}** image{'s' if n != 1 else ''} queued. "
            "Say **'generate'** when ready, or keep sending more photos.",
            no_extra,
        )

    if intent == "clear":
        _clear_queue(ctx, sender)
        return "Queue cleared. Send new room photos whenever you're ready.", no_extra

    if intent == "generate":
        return _do_generate(ctx, sender, queue, [])

    # ── Step 5: fallback ──────────────────────────────────────────────────────
    if collect_errors:
        return (
            "Could not retrieve images:\n" + "\n".join(f"- {e}" for e in collect_errors),
            no_extra,
        )

    if queue:
        n = len(queue)
        return (
            f"You have {n} image{'s' if n != 1 else ''} queued. "
            "Say **'generate'** to create the tactile map, or send more photos.",
            no_extra,
        )

    return (
        "Attach room photos or paste image URLs to get started. "
        "Say `help` for instructions.",
        no_extra,
    )


# ── uagent plumbing ───────────────────────────────────────────────────────────

def _reply(text: str, extra: list[AgentContent] | None = None) -> ChatMessage:
    content: list[AgentContent] = [TextContent(type="text", text=text)]
    if extra:
        content.extend(extra)
    content.append(EndSessionContent(type="end-session"))
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=content,
    )


agent = Agent(
    name="roomplan-tactile",
    seed=AGENT_SEED,
    port=8002,
    mailbox=True,
    handle="roomplan-tactile",
    description=(
        "Send room photos to build up a queue, then say 'generate' to receive "
        "a single tactile accessibility map synthesised from all views."
    ),
    publish_agent_details=True,
)

protocol = Protocol(spec=chat_protocol_spec)


@protocol.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(
        sender,
        ChatAcknowledgement(acknowledged_msg_id=msg.msg_id),
    )
    ctx.logger.info("chat from %s", sender[:16] + "…")
    try:
        loop = asyncio.get_event_loop()
        reply_text, extra_content = await loop.run_in_executor(
            None, handle_chat, ctx, sender, msg
        )
    except Exception as exc:
        ctx.logger.exception("handle_chat crashed")
        reply_text, extra_content = f"Something went wrong: {exc}", []
    await ctx.send(sender, _reply(reply_text, extra_content))


@protocol.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.debug("ack from %s for %s", sender[:16] + "…", msg.acknowledged_msg_id)


agent.include(protocol, publish_manifest=True)

if __name__ == "__main__":
    log.info("RoomPlan Tactile agent starting.")
    agent.run()
