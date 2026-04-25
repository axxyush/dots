"""RoomPlanBraille — Fetch.ai chat agent for RoomPlan JSON → tactile Braille map.

This agent is intended to run on a laptop (or server) but be reachable from
Agentverse via the mailbox relay (mailbox=True).

Flow:
  - User sends either:
    - a URL to a RoomPlan JSON file, or
    - pastes the RoomPlan JSON directly
  - Agent converts RoomPlan JSON → normalized floorplan JSON (LLM-assisted)
  - Agent generates Braille map artifacts and returns shareable URLs

Env:
  - GEMINI_API_KEY (required for RoomPlan→floorplan conversion)
  - ROOMPLAN_BRAILLE_AGENT_SEED (stable seed phrase)
  - GEMINI_ROOMPLAN_MODEL (optional, default gemini-2.5-pro)

Run:
  .venv/bin/python agents/roomplan_braille_agent.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# Ensure repo root is importable when running as a script:
# `.venv/bin/python agents/roomplan_braille_agent.py`
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(_REPO_ROOT / ".env", override=False)
except ImportError:
    pass

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    TextContent,
    chat_protocol_spec,
)

from agents.roomplan_tools import dispatch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
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

URL_REGEX = re.compile(r"https?://[^\s<>\"]+")
_MENTION_REGEX = re.compile(r"@\S+")
_TRAILING_PUNCT = ".,;:!?)\"']"
_HI = {"hi", "hello", "hey", "yo", "sup", "hola"}
_HELP = {"help", "/help", "?", "instructions", "usage"}


def _normalize(text: str) -> str:
    return _MENTION_REGEX.sub("", text).strip()


def _extract_urls(text: str) -> list[str]:
    urls = []
    for raw in URL_REGEX.findall(text):
        while raw and raw[-1] in _TRAILING_PUNCT:
            raw = raw[:-1]
        if raw:
            urls.append(raw)
    # preserve order, dedupe
    out, seen = [], set()
    for u in urls:
        if u not in seen:
            out.append(u)
            seen.add(u)
    return out


def _reply(text: str) -> ChatMessage:
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[
            TextContent(type="text", text=text),
            EndSessionContent(type="end-session"),
        ],
    )


def _summarize_result(res: dict) -> str:
    if "error" in res:
        return f"Error: {res['error']}"

    parts: list[str] = []
    parts.append("Generated Braille map artifacts:")

    for label, key in (
        ("Floorplan JSON", "floorplan_json_url"),
        ("Braille map (text)", "txt_url"),
        ("Braille legend", "legend_url"),
        ("Braille map (PNG preview)", "png_url"),
    ):
        url = res.get(key)
        if url:
            parts.append(f"- {label}: {url}")

    meta = []
    if res.get("cols") and res.get("rows"):
        meta.append(f"grid={res['cols']}×{res['rows']}")
    if res.get("legend_counts"):
        meta.append(f"legend_types={len(res['legend_counts'])}")
    if meta:
        parts.append("")
        parts.append("Details: " + ", ".join(meta))

    if res.get("warnings"):
        parts.append("")
        parts.append("Warnings:")
        for w in res["warnings"][:8]:
            parts.append(f"- {w}")

    return "\n".join(parts)


def handle_chat(text: str) -> str:
    text = _normalize(text)
    if not text:
        return "Send a RoomPlan JSON URL or paste the JSON."

    low = text.lower().strip()
    if low in _HELP or any(w in _HELP for w in re.findall(r"[a-z/\\?]+", low)):
        return (
            "Send **either**:\n"
            "- a public URL to a RoomPlan `.json`, or\n"
            "- paste the RoomPlan JSON directly.\n\n"
            "I will return links to the converted floorplan JSON and the tactile output.\n\n"
            "Tip: if your message starts with `{` or `[`, I'll treat it as JSON."
        )
    if low in _HI or any(w in _HI for w in re.findall(r"[a-z]+", low)):
        return "Hi! Paste your RoomPlan JSON (or send a public URL to it). Send `help` for details."

    urls = _extract_urls(text)
    roomplan_json: str | None = None
    source_name = "roomplan.json"

    if urls:
        dl = dispatch("download_json", {"url": urls[0]})
        if "error" in dl:
            return f"Download failed: {dl['error']}"
        p = Path(dl["json_path"])
        source_name = p.name
        roomplan_json = p.read_text(encoding="utf-8")
    else:
        # Only treat as JSON if it looks like JSON; otherwise show guidance.
        stripped = text.lstrip()
        if not (stripped.startswith("{") or stripped.startswith("[")):
            return "I didn't see a URL or JSON. Send `help` for instructions."
        roomplan_json = text
        # Try to parse quickly to derive a nicer source_name.
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and "id" in obj and isinstance(obj["id"], str):
                source_name = f"roomplan_{obj['id']}.json"
        except Exception:
            pass

    res = dispatch(
        "braille_map_from_roomplan_json",
        {"roomplan_json": roomplan_json, "source_name": source_name, "cols": 90},
    )
    if "error" in res:
        return res["error"]

    # Upload artifacts and return URLs.
    uploads: dict[str, str] = {}
    for k, out_key in (
        ("floorplan_json_path", "floorplan_json_url"),
        ("txt_path", "txt_url"),
        ("legend_path", "legend_url"),
        ("png_path", "png_url"),
    ):
        pth = res.get(k)
        if not pth:
            continue
        up = dispatch("upload_artifact", {"file_path": pth})
        if "error" not in up:
            uploads[out_key] = up["url"]

    res2 = {**res, **uploads}
    return _summarize_result(res2)


agent = Agent(
    name="roomplan-tactile",
    seed=AGENT_SEED,
    port=8002,
    mailbox=True,
    handle="roomplan-tactile",
    description="Convert Apple RoomPlan JSON into an accessibility tactile map output.",
    readme_path=str(Path(__file__).resolve().parent / "ROOMPLAN_TACTILE_README.md"),
    publish_agent_details=True,
)

protocol = Protocol(spec=chat_protocol_spec)


@protocol.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc),
            acknowledged_msg_id=msg.msg_id,
        ),
    )
    user_text = "".join(item.text for item in msg.content if isinstance(item, TextContent)).strip()
    ctx.logger.info("chat from %s: %r", sender[:12] + "…", user_text[:200])
    answer = handle_chat(user_text)
    await ctx.send(sender, _reply(answer))


@protocol.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.debug("ack from %s for %s", sender[:12] + "…", msg.acknowledged_msg_id)


agent.include(protocol, publish_manifest=True)


if __name__ == "__main__":
    log.info("RoomPlanBraille starting.")
    agent.run()

