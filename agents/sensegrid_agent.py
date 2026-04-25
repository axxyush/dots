"""SenseGrid — Fetch.ai chat agent for floor-plan parsing with a test-FET paywall.

Flow (deterministic state machine; payment is NOT trusted to an LLM):
  1. User sends a ChatMessage with one or more http(s) floor-plan URLs.
  2. Agent quotes a price in test FET (Dorado), waits for "yes".
  3. On "yes", parses + renders a BLURRED preview, posts the agent's pay
     address + a faucet link, and waits for "paid".
  4. On "paid", verifies the on-chain balance delta against the unique
     per-session quoted amount; only on success does it deliver the final
     unblurred PNG + structured JSON.

Run:
    pip install -r requirements.txt
    # In .env at repo root:
    #   OPENAI_API_KEY=sk-...                # preferred — GPT-4o end-to-end parse
    #   GEMINI_API_KEY=...                   # optional fallback labeler
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
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# Load .env from the repo root BEFORE we read any os.environ values. Without
# override=False existing shell vars still win, so `export ASI_ONE_API_KEY=...`
# in your shell keeps working alongside .env.
try:
    from dotenv import load_dotenv  # type: ignore

    _REPO_ROOT = Path(__file__).resolve().parents[1]
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

from tools import dispatch
from payment import (
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
    blurred: bool,
    logger: logging.Logger,
) -> tuple[list[str], list[str], dict[str, int], list[dict], dict[str, int], str, list[str], str | None]:
    """Run download → parse → generate_braille_map(blurred) → upload for each URL.
    Returns (json_paths, png_urls, room_summary, ada_findings, ada_summary, ada_report_text, ada_pdf_urls, error_or_None).

    ``png_urls`` contains the *tactile* braille map URLs — the primary
    deliverable for a blind user. A separate text companion is produced
    (and uploaded post-payment) so screen readers can narrate the layout.
    """
    json_paths: list[str] = []
    png_urls: list[str] = []
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
            return [], [], {}, [], {}, "", [], f"Couldn't download {url}: {dl['error']}"

        parsed = dispatch("parse_floorplan_llm", {"image_path": dl["image_path"]})
        if "error" in parsed:
            logger.warning(
                "⚠️  LLM extractor failed — falling back to CV pipeline.\n"
                "    reason: %s\n"
                "    Check that OPENAI_API_KEY is set in .env AND that "
                "python-dotenv is installed (`pip install python-dotenv`).",
                parsed["error"],
            )
            parsed = dispatch("parse_floorplan", {"image_path": dl["image_path"]})
        else:
            logger.info(
                "✅  LLM extractor succeeded — %s rooms via %s",
                parsed.get("room_count"), parsed.get("labeling_model"),
            )
        if "error" in parsed:
            return [], [], {}, [], {}, "", [], f"Couldn't parse {url}: {parsed['error']}"

        json_paths.append(parsed["json_path"])
        for k, v in parsed.get("rooms_by_type", {}).items():
            room_summary[k] = room_summary.get(k, 0) + v
        for k in ("total_findings", "high_severity", "medium_severity", "low_severity"):
            ada_summary[k] = ada_summary.get(k, 0) + int((parsed.get("ada_summary") or {}).get(k, 0))
        for rec in parsed.get("ada_findings", []):
            if isinstance(rec, dict):
                ada_findings.append(rec)
        if not ada_report_text and isinstance(parsed.get("ada_report_text"), str):
            ada_report_text = parsed["ada_report_text"]
        pdf_path = parsed.get("ada_report_pdf_path")
        if isinstance(pdf_path, str) and pdf_path.strip():
            up_pdf = dispatch("upload_artifact", {"file_path": pdf_path})
            if "error" not in up_pdf and up_pdf.get("url"):
                ada_pdf_urls.append(up_pdf["url"])

        rec = dispatch(
            "generate_braille_map",
            {"json_path": parsed["json_path"], "blurred": blurred},
        )
        if "error" in rec:
            return [], [], {}, [], {}, "", [], f"Tactile map render failed: {rec['error']}"

        up = dispatch("upload_artifact", {"file_path": rec["png_path"]})
        if "error" in up:
            return [], [], {}, [], {}, "", [], f"Upload failed: {up['error']}"
        png_urls.append(up["url"])

    # De-duplicate similar findings across multi-plan batches.
    seen = set()
    uniq_findings: list[dict] = []
    for finding in ada_findings:
        key = (finding.get("category"), finding.get("ada_reference"), finding.get("observed_condition"))
        if key in seen:
            continue
        seen.add(key)
        uniq_findings.append(finding)

    return json_paths, png_urls, room_summary, uniq_findings, ada_summary, ada_report_text, ada_pdf_urls, None


def _format_ada_report_block(
    ada_summary: dict[str, int],
    ada_findings: list[dict],
    ada_report_text: str,
) -> str:
    if ada_report_text:
        return f"**Preliminary ADA Compliance Report**\n\n{ada_report_text}"
    if not ada_findings:
        return (
            "**Preliminary ADA Compliance Report**\n\n"
            "No reportable findings were generated from current parsed data."
        )
    return (
        "**Preliminary ADA Compliance Report**\n\n"
        f"Findings: {ada_summary.get('total_findings', 0)} "
        f"(High {ada_summary.get('high_severity', 0)}, "
        f"Medium {ada_summary.get('medium_severity', 0)}, "
        f"Low {ada_summary.get('low_severity', 0)})."
    )


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
                "I'll quote a price in test FET, show you a blurred preview, "
                "then deliver the full reconstruction once payment lands."
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
            f"Reply 'yes' to proceed (you'll see a blurred preview before paying), "
            f"or 'no' to cancel."
        )

    if state == STATE_QUOTED:
        if intent != "yes":
            return (
                f"Reply 'yes' to proceed with the quote of "
                f"{sess['quoted_fet_str']} test FET, or 'no' to cancel."
            )

        json_paths, blurred_urls, room_summary, ada_findings, ada_summary, ada_report_text, ada_pdf_urls, err = _process_plans(
            sess["urls"], blurred=True, logger=logger
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
                "json_paths": json_paths,
                "blurred_urls": blurred_urls,
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
        previews = "\n".join(f"- {u}" for u in blurred_urls)
        pdfs = "\n".join(f"- {u}" for u in sess.get("ada_pdf_urls", []))
        pdf_block = f"**ADA report (PDF):**\n{pdfs}\n\n" if pdfs else ""
        ada_text = _format_ada_report_block(
            ada_summary=sess.get("ada_summary", {}),
            ada_findings=sess.get("ada_findings", []),
            ada_report_text=sess.get("ada_report_text", ""),
        )
        ada_fallback = f"{ada_text}\n\n" if not pdfs else ""
        plural = "s" if len(sess["urls"]) > 1 else ""
        user_addr = str(user_wallet.address())
        return (
            f"Parsed {sum(room_summary.values())} rooms across {sess['n_plans']} plan{plural} "
            f"({rooms_str}).\n\n"
            f"**Tactile Braille map — blurred preview:**\n{previews}\n\n"
            f"{pdf_block}"
            f"{ada_fallback}"
            f"Reply **'yes'** to authorize payment of **{sess['quoted_fet_str']} test FET** "
            f"— I'll execute the transfer on-chain (Dorado) automatically, then deliver the "
            f"sharp tactile map (print-ready for swell paper / embosser) plus a plain-text "
            f"screen-reader companion. Reply 'no' to cancel.\n\n"
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

        # Render unblurred tactile map + text companion + upload + deliver.
        final_urls: list[str] = []
        txt_urls: list[str] = []
        json_urls: list[str] = []
        for jp in sess["json_paths"]:
            rec = dispatch("generate_braille_map", {"json_path": jp, "blurred": False})
            if "error" in rec:
                return f"Tactile map render failed after payment: {rec['error']}"
            up = dispatch("upload_artifact", {"file_path": rec["png_path"]})
            if "error" in up:
                return f"Upload failed after payment: {up['error']}"
            final_urls.append(up["url"])
            if rec.get("txt_path"):
                up_txt = dispatch("upload_artifact", {"file_path": rec["txt_path"]})
                if "error" not in up_txt:
                    txt_urls.append(up_txt["url"])
            up_json = dispatch("upload_artifact", {"file_path": jp})
            if "error" not in up_json:
                json_urls.append(up_json["url"])

        _clear_session(ctx, sender)
        finals = "\n".join(f"- {u}" for u in final_urls)
        txts = "\n".join(f"- {u}" for u in txt_urls) if txt_urls else ""
        jsons = "\n".join(f"- {u}" for u in json_urls) if json_urls else ""
        plural = "s" if len(final_urls) > 1 else ""
        msg = (
            f"Payment of {sess['quoted_fet_str']} test FET confirmed on-chain "
            f"(tx `{tx_hash}`).\n\n"
            f"**Tactile Braille map{plural}** (print on swell paper or send to a "
            f"tactile embosser):\n{finals}"
        )
        if txts:
            msg += f"\n\n**Screen-reader companion (plain text):**\n{txts}"
        if jsons:
            msg += f"\n\n**Structured JSON:**\n{jsons}"
        if sess.get("ada_pdf_urls"):
            msg += "\n\nADA report (PDF):\n" + "\n".join(f"- {u}" for u in sess["ada_pdf_urls"])
        else:
            msg += "\n\n" + _format_ada_report_block(
                ada_summary=sess.get("ada_summary", {}),
                ada_findings=sess.get("ada_findings", []),
                ada_report_text=sess.get("ada_report_text", ""),
            )
        msg += "\n\nThanks!"
        return msg

    return "Send a floor-plan URL to get started."


# ── uagent + chat protocol plumbing ──────────────────────────────────────────

agent = Agent(
    name="sensegrid",
    seed=AGENT_SEED,
    port=8001,
    mailbox=True,
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


def _log_parser_health() -> None:
    """At startup, print which parser path will run and flag any gotchas.

    Without this banner, `parse_floorplan_llm` errors silently get swallowed
    by the agent's CV fallback, and the user has no idea their LLM path is
    broken until all rooms come back as "Unknown".
    """
    import sys
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parents[1]
    parser_dir = repo_root / "floorplan_parser"
    if str(parser_dir) not in sys.path:
        sys.path.insert(0, str(parser_dir))

    bar = "═" * 68
    log.info(bar)
    log.info(" SenseGrid parser self-check")
    log.info(bar)

    openai_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    gemini_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    log.info(" OPENAI_API_KEY ....... %s",
             f"set (prefix {openai_key[:10]}…, len {len(openai_key)})"
             if openai_key else "MISSING — LLM path disabled")
    log.info(" GEMINI_API_KEY ....... %s",
             f"set (len {len(gemini_key)})" if gemini_key else "not set")
    log.info(" OPENAI_PARSE_MODEL ... %s", os.environ.get("OPENAI_PARSE_MODEL", "gpt-5.4 (default)"))

    try:
        import openai  # noqa: F401
        log.info(" openai package ....... ok (v%s)", openai.__version__)
    except Exception as exc:
        log.error(" openai package ....... MISSING (%s) — run: pip install -r requirements.txt", exc)

    try:
        import dotenv  # noqa: F401
        log.info(" python-dotenv ........ ok")
    except Exception:
        log.error(" python-dotenv ........ MISSING — .env will NOT load. "
                  "Run: pip install python-dotenv")

    try:
        from llm_floorplan import parse_floorplan_with_llm  # noqa: F401
        log.info(" parse_floorplan_llm .. importable")
    except Exception as exc:
        log.error(" parse_floorplan_llm .. IMPORT FAILED (%s)", exc)

    if openai_key:
        log.info(" → Primary parser:  OpenAI end-to-end (parse_floorplan_llm)")
    elif gemini_key:
        log.info(" → Primary parser:  CV + Gemini labeling (fallback path)")
    else:
        log.warning(" → Primary parser:  CV only — all rooms will be labeled 'Unknown'!")
    log.info(bar)


if __name__ == "__main__":
    log.info("SenseGrid starting.")
    _log_parser_health()
    log.info("Service wallet (receives payments): %s", agent.wallet.address())
    log.info("Demo user-proxy wallet (auto-pays):  %s", user_wallet.address())
    log.info("Fund the user-proxy once at: %s/%s", FAUCET_URL, user_wallet.address())
    agent.run()
