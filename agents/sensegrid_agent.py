"""SenseGrid — Fetch.ai chat agent for generating tactile floorplan maps with a test-FET paywall.

Flow (deterministic state machine; payment is NOT trusted to an LLM):
  1. User sends a ChatMessage with one or more http(s) floor-plan URLs.
  2. Agent quotes a price in test FET (Dorado), waits for "yes".
  3. On "yes", downloads the image(s), posts the agent's pay address + a faucet
     link, and waits for "paid".
  4. On "paid", verifies the on-chain balance delta against the unique
     per-session quoted amount; only on success does it deliver the tactile map
     output.

Run:
    pip install -r requirements.txt
    # In .env at repo root:
    #   GEMINI_API_KEY=...
    #   SENSEGRID_AGENT_SEED=<any long random phrase, keep stable>
    #   SENSEGRID_PRICE_PER_PLAN_FET=0.5     # optional, default 0.5
    #   SENSEGRID_PAYMENT_TIMEOUT_S=600      # optional, default 600
    python agents/sensegrid_agent.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# Ensure repo root is importable when running as a script:
# `.venv/bin/python agents/sensegrid_agent.py`
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# Load .env from the repo root BEFORE we read any os.environ values. Without
# override=False existing shell vars still win, so `export ASI_ONE_API_KEY=...`
# in your shell keeps working alongside .env.
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(_REPO_ROOT / ".env", override=False)
except ImportError:
    pass  # python-dotenv is optional; env vars can still be set manually.

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    TextContent,
    chat_protocol_spec,
)

from agents.tools import dispatch
from agents.payment import (
    GAS_BUFFER_ATESTFET,
    PAYMENT_TIMEOUT_S,
    FAUCET_URL,
    address_balance_atestfet,
    atestfet_to_fet_str,
    make_wallet_from_seed,
    payment_received,
    quote_for,
    send_tokens,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("sensegrid.agent")


def _ensure_event_loop() -> None:
    """Python 3.14+ no longer creates a default event loop implicitly."""
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


# ── Config ───────────────────────────────────────────────────────────────────

AGENT_SEED = os.environ.get(
    "SENSEGRID_AGENT_SEED",
    "braille-map-seed-phrase",
)

# Deterministic "user-proxy" wallet: the agent pays itself on the user's
# behalf so the demo is fully autonomous. Funded once from the Dorado faucet.
USER_PROXY_SEED = os.environ.get(
    "SENSEGRID_USER_SEED",
    f"{AGENT_SEED}-user-proxy",
)
user_wallet = make_wallet_from_seed(USER_PROXY_SEED)

# ── Two-step paywall state machine ───────────────────────────────────────────
#
# Payment is settled deterministically in Python. The LLM/ASI:One client is
# NOT trusted to gate payment — it would happily hallucinate a "paid" status.
#
#   idle ─send url(s)─► quoted ─"yes"─► previewed ─"paid" + tx confirmed─► idle
#                          │                  │
#                          └─"no"─────────────┴──────────► idle (cancelled)

STATE_IDLE = "idle"
STATE_QUOTED = "quoted"
STATE_PREVIEWED = "previewed"

URL_REGEX = re.compile(r"https?://[^\s<>\"]+")
_MENTION_REGEX = re.compile(r"@\S+")
_TRAILING_PUNCT = ".,;:!?)\"']"

_YES = {"yes", "y", "proceed", "ok", "okay", "go", "continue", "sure", "confirm", "yep", "yeah"}
_NO = {"no", "n", "cancel", "stop", "nevermind", "abort", "nope"}
_PAID = {"paid", "sent", "done"}


def _normalize(text: str) -> str:
    """Strip @mentions and trim — ASI:One prepends @agent1qx... to user replies."""
    return _MENTION_REGEX.sub("", text).strip()


def _extract_urls(text: str) -> list[str]:
    urls = []
    for raw in URL_REGEX.findall(text):
        while raw and raw[-1] in _TRAILING_PUNCT:
            raw = raw[:-1]
        if raw:
            urls.append(raw)
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _intent(text: str) -> str:
    """Word-set membership beats exact match: tolerates 'yes please',
    '@agent yes', 'ok lets go', etc."""
    t = _normalize(text).lower().rstrip(".!?,;:")
    if not t:
        return "other"
    words = set(re.findall(r"[a-z']+", t))
    if t in _YES or words & _YES:
        return "yes"
    if t in _NO or words & _NO:
        return "no"
    if words & _PAID or "i paid" in t or "i've paid" in t:
        return "paid"
    if URL_REGEX.search(text):
        return "urls"
    return "other"


def _session_key(sender: str) -> str:
    return f"sess:{sender}"


def _load_session(ctx, sender: str) -> dict:
    raw = ctx.storage.get(_session_key(sender))
    if not raw:
        return {"state": STATE_IDLE}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"state": STATE_IDLE}


def _save_session(ctx, sender: str, sess: dict) -> None:
    ctx.storage.set(_session_key(sender), json.dumps(sess))


def _clear_session(ctx, sender: str) -> None:
    ctx.storage.set(_session_key(sender), json.dumps({"state": STATE_IDLE}))


def _process_plans(
    urls: list[str],
    logger: logging.Logger,
) -> tuple[list[str], dict[str, int], list[dict], dict[str, int], str, list[str], str | None]:
    """Download each URL and compute ADA compliance report inputs via CV parse.

    Returns (image_paths, room_summary, ada_findings, ada_summary, ada_report_text, ada_pdf_urls, error_or_None).
    """
    image_paths: list[str] = []
    room_summary: dict[str, int] = {}
    ada_findings: list[dict] = []
    ada_report_text: str = ""
    ada_pdf_urls: list[str] = []
    ada_summary: dict[str, int] = {
        "total_findings": 0,
        "high_severity": 0,
        "medium_severity": 0,
        "low_severity": 0,
    }

    for url in urls:
        logger.info("processing %s", url)
        dl = dispatch("download_image", {"url": url})
        if "error" in dl:
            return [], {}, [], {}, "", [], f"Couldn't download {url}: {dl['error']}"
        image_paths.append(dl["image_path"])

        parsed = dispatch("parse_floorplan", {"image_path": dl["image_path"]})
        if "error" in parsed:
            return [], {}, [], {}, "", [], f"Couldn't compute ADA report for {url}: {parsed['error']}"

        for k, v in (parsed.get("rooms_by_type") or {}).items():
            room_summary[k] = room_summary.get(k, 0) + int(v)

        for k in ("total_findings", "high_severity", "medium_severity", "low_severity"):
            ada_summary[k] = ada_summary.get(k, 0) + int((parsed.get("ada_summary") or {}).get(k, 0))

        for rec in parsed.get("ada_findings", []) or []:
            if isinstance(rec, dict):
                ada_findings.append(rec)

        if not ada_report_text and isinstance(parsed.get("ada_report_text"), str):
            ada_report_text = parsed["ada_report_text"]

        pdf_path = parsed.get("ada_report_pdf_path")
        if isinstance(pdf_path, str) and pdf_path.strip():
            up_pdf = dispatch("upload_artifact", {"file_path": pdf_path})
            if "error" not in up_pdf and up_pdf.get("url"):
                ada_pdf_urls.append(up_pdf["url"])

    # De-duplicate similar findings across multi-plan batches.
    seen = set()
    uniq_findings: list[dict] = []
    for finding in ada_findings:
        key = (finding.get("category"), finding.get("ada_reference"), finding.get("observed_condition"))
        if key in seen:
            continue
        seen.add(key)
        uniq_findings.append(finding)

    return image_paths, room_summary, uniq_findings, ada_summary, ada_report_text, ada_pdf_urls, None


def handle_chat(ctx, sender: str, user_text: str) -> str:
    """Deterministic two-step paywall. See state diagram at top of this section."""
    sess = _load_session(ctx, sender)
    state = sess.get("state", STATE_IDLE)
    intent = _intent(user_text)
    logger = ctx.logger

    if intent == "no":
        _clear_session(ctx, sender)
        return "Cancelled. Send a floor-plan URL whenever you're ready."

    if state != STATE_IDLE and time.time() > sess.get("expires_at", 0):
        logger.info("session for %s expired — resetting", sender[:12])
        _clear_session(ctx, sender)
        sess, state = {"state": STATE_IDLE}, STATE_IDLE

    # Sending a fresh URL while mid-flow re-quotes from scratch.
    if state != STATE_IDLE and intent == "urls":
        _clear_session(ctx, sender)
        sess, state = {"state": STATE_IDLE}, STATE_IDLE

    if state == STATE_IDLE:
        urls = _extract_urls(user_text)
        if not urls:
            return (
                    "Send one or more floor-plan image URLs (http/https). "
                    "I'll quote a price in test FET, then deliver a tactile map "
                    "once payment lands."
            )
        q = quote_for(len(urls))
        sess = {
            "state": STATE_QUOTED,
            "urls": urls,
            "quoted_atestfet": q.total_atestfet,
            "quoted_fet_str": q.total_fet_str,
            "n_plans": len(urls),
            "expires_at": time.time() + PAYMENT_TIMEOUT_S,
        }
        _save_session(ctx, sender, sess)
        plural = "s" if len(urls) > 1 else ""
        return (
            f"Quote: **{q.total_fet_str} test FET** for {len(urls)} floor plan{plural}.\n\n"
            f"Reply 'yes' to proceed, "
            f"or 'no' to cancel."
        )

    if state == STATE_QUOTED:
        if intent != "yes":
            return (
                f"Reply 'yes' to proceed with the quote of "
                f"{sess['quoted_fet_str']} test FET, or 'no' to cancel."
            )

        image_paths, room_summary, ada_findings, ada_summary, ada_report_text, ada_pdf_urls, err = _process_plans(
            sess["urls"], logger=logger
        )
        if err:
            _clear_session(ctx, sender)
            return err

        pay_addr = str(agent.wallet.address())
        try:
            start_balance = address_balance_atestfet(pay_addr)
        except Exception as exc:
            logger.exception("ledger query failed")
            _clear_session(ctx, sender)
            return f"Couldn't reach Fetch.ai testnet ledger: {exc}"

        sess.update(
            {
                "state": STATE_PREVIEWED,
                "image_paths": image_paths,
                "room_summary": room_summary,
                "ada_findings": ada_findings,
                "ada_summary": ada_summary,
                "ada_report_text": ada_report_text,
                "ada_pdf_urls": ada_pdf_urls,
                "start_balance_atestfet": start_balance,
                "pay_address": pay_addr,
                "expires_at": time.time() + PAYMENT_TIMEOUT_S,
            }
        )
        _save_session(ctx, sender, sess)

        rooms_str = ", ".join(f"{k}: {v}" for k, v in sorted(room_summary.items()))
        pdfs = "\n".join(f"- {u}" for u in (sess.get("ada_pdf_urls") or []))
        pdf_block = f"**ADA report (PDF):**\n{pdfs}\n\n" if pdfs else ""
        ada_text = (sess.get("ada_report_text") or "").strip()
        ada_block = f"**Preliminary ADA Compliance Report**\n\n{ada_text}\n\n" if ada_text else ""
        if not ada_text and ada_findings:
            ada_block = (
                "**Preliminary ADA Compliance Report**\n\n"
                f"Findings: {ada_summary.get('total_findings', 0)} "
                f"(High {ada_summary.get('high_severity', 0)}, "
                f"Medium {ada_summary.get('medium_severity', 0)}, "
                f"Low {ada_summary.get('low_severity', 0)}).\n\n"
            )

        plural = "s" if len(sess["urls"]) > 1 else ""
        user_addr = str(user_wallet.address())
        return (
            f"Parsed {sum(room_summary.values())} rooms across {sess['n_plans']} plan{plural} "
            f"({rooms_str}).\n\n"
            f"{pdf_block}"
            f"{ada_block}"
            f"Reply **'yes'** to authorize payment of **{sess['quoted_fet_str']} test FET** "
            f"— I'll execute the transfer on-chain (Dorado) automatically and deliver the "
            f"tactile map. Reply 'no' to cancel.\n\n"
            f"_Demo wallet: `{user_addr}` — top up at {FAUCET_URL}/{user_addr} if needed._"
        )

    if state == STATE_PREVIEWED:
        if intent not in {"yes", "paid"}:
            return (
                "Reply 'yes' to authorize the autonomous payment, or 'no' to cancel."
            )

        # Autonomous payment: agent signs + broadcasts the transfer from the
        # user-proxy wallet to the service wallet.
        amount = int(sess["quoted_atestfet"])
        user_addr = str(user_wallet.address())
        try:
            user_balance = address_balance_atestfet(user_addr)
        except Exception as exc:
            logger.exception("ledger query failed before send")
            return f"Couldn't reach the ledger: {exc}. Try again in a few seconds."

        if user_balance < amount + GAS_BUFFER_ATESTFET:
            return (
                f"Demo user wallet `{user_addr}` doesn't have enough test FET "
                f"(has {atestfet_to_fet_str(user_balance)}, needs ≥ "
                f"{atestfet_to_fet_str(amount + GAS_BUFFER_ATESTFET)} including gas).\n\n"
                f"Top it up once at {FAUCET_URL}/{user_addr} and reply 'yes' again."
            )

        try:
            tx_hash = send_tokens(user_wallet, sess["pay_address"], amount)
            logger.info("autonomous payment broadcast: %s atestfet → %s (tx %s)",
                        amount, sess["pay_address"], tx_hash)
        except Exception as exc:
            logger.exception("send_tokens failed")
            return f"Payment broadcast failed: {exc}. Reply 'yes' to retry or 'no' to cancel."

        try:
            received, current = payment_received(
                sess["pay_address"],
                int(sess["start_balance_atestfet"]),
                amount,
            )
        except Exception as exc:
            logger.exception("ledger query failed during verify")
            return (
                f"Tx {tx_hash} broadcast but couldn't verify on-chain receipt: {exc}. "
                f"Check the Dorado explorer and reply 'yes' to retry."
            )

        if not received:
            got = max(0, current - int(sess["start_balance_atestfet"]))
            return (
                f"Tx {tx_hash} broadcast but balance hasn't reflected yet "
                f"(saw {atestfet_to_fet_str(got)} of {sess['quoted_fet_str']} expected). "
                f"Reply 'yes' in a few seconds to retry."
            )

        # Deliver tactile map.
        tactile_png_urls: list[str] = []
        tactile_errors: list[str] = []

        # Tactile map generated directly from the input image.
        # Prefer reusing local downloads from earlier steps, but fall back to re-downloading
        # from the original URLs so the tactile map is always produced.
        img_paths = list(sess.get("image_paths", []) or [])
        if not img_paths:
            for url in sess.get("urls", []) or []:
                dl = dispatch("download_image", {"url": url})
                if "error" in dl:
                    tactile_errors.append(f"{url}: download failed ({dl['error']})")
                    continue
                img_paths.append(dl["image_path"])

        for img_path in img_paths:
            t = dispatch(
                "tactile_map_from_image_nanobanana",
                {"image_path": img_path, "model": "gemini-3-pro-image-preview"},
            )
            if "error" in t:
                err = str(t.get("error"))
                tactile_errors.append(f"{Path(img_path).name}: {err}")
                logger.warning("tactile nanobanana failed for %s: %s", img_path, err)
                continue
            up_t = dispatch("upload_artifact", {"file_path": t["png_path"]})
            if "error" in up_t:
                tactile_errors.append(f"{Path(img_path).name}: upload failed ({up_t['error']})")
                continue
            tactile_png_urls.append(up_t["url"])

        _clear_session(ctx, sender)
        tactile = "\n".join(f"- {u}" for u in tactile_png_urls) if tactile_png_urls else ""
        msg = (
            f"Payment of {sess['quoted_fet_str']} test FET confirmed on-chain "
            f"(tx `{tx_hash}`)."
        )
        if tactile:
            msg += f"\n\nTactile map (PNG):\n{tactile}"
        if tactile_errors and not tactile_png_urls:
            # Surface failures so users don't think it's silently missing.
            msg += "\n\nTactile map generation failed:\n" + "\n".join(f"- {e}" for e in tactile_errors[:6])
        msg += "\n\nThanks!"
        return msg

    return "Send a floor-plan URL to get started."


# ── uagent + chat protocol plumbing ──────────────────────────────────────────

agent = Agent(
    name="sensegrid",
    seed=AGENT_SEED,
    port=8001,
    mailbox=True,
    handle="sensegrid",
    description="Turn floorplan image URLs into a tactile map output.",
    readme_path=str(Path(__file__).resolve().parent / "SENSEGRID_README.md"),
    publish_agent_details=True,
)

protocol = Protocol(spec=chat_protocol_spec)


def _reply(text: str) -> ChatMessage:
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[
            TextContent(type="text", text=text),
            EndSessionContent(type="end-session"),
        ],
    )


@protocol.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc),
            acknowledged_msg_id=msg.msg_id,
        ),
    )

    user_text = "".join(
        item.text for item in msg.content if isinstance(item, TextContent)
    ).strip()
    if not user_text:
        await ctx.send(sender, _reply("I received an empty message. Please send a floor-plan image URL."))
        return

    ctx.logger.info("chat from %s: %r", sender[:12] + "…", user_text[:160])

    try:
        answer = handle_chat(ctx, sender, user_text)
    except Exception as exc:
        ctx.logger.exception("payment flow crashed")
        answer = f"Something went wrong while processing your request: {exc}"

    await ctx.send(sender, _reply(answer))


@protocol.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.debug("ack from %s for %s", sender[:12] + "…", msg.acknowledged_msg_id)


agent.include(protocol, publish_manifest=True)


if __name__ == "__main__":
    log.info("SenseGrid starting.")
    log.info("Service wallet (receives payments): %s", agent.wallet.address())
    log.info("Demo user-proxy wallet (auto-pays):  %s", user_wallet.address())
    log.info("Fund the user-proxy once at: %s/%s", FAUCET_URL, user_wallet.address())
    agent.run()
