"""SenseGrid — Fetch.ai chat agent for generating tactile floorplan maps with a test-FET paywall.

Flow (deterministic state machine; payment is NOT trusted to an LLM):
  1. User sends room/floorplan images — attached directly OR as http(s) URLs.
  2. Agent quotes a price in test FET (Dorado), waits for "yes".
  3. On "yes", processes images (ADA report preview), posts pay address + faucet link.
  4. On "yes" again, executes autonomous on-chain payment, delivers:
       - tactile PNG URL (tmpfiles.org, ~60 min)
       - tactile PNG embedded as image attachment in the reply

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
import base64
import json
import logging
import os
import re
import secrets
import sys
import tempfile
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
from agents.wayfind_protocol import VenueLookup, VenueInfo
from backend.map_store import MapStore
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

ARTIFACT_DIR = Path(tempfile.gettempdir()) / "sensegrid_artifacts"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)


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

# ── Config ───────────────────────────────────────────────────────────────────

AGENT_SEED = os.environ.get("SENSEGRID_AGENT_SEED", "braille-map-seed-phrase")

USER_PROXY_SEED = os.environ.get("SENSEGRID_USER_SEED", f"{AGENT_SEED}-user-proxy")
user_wallet = make_wallet_from_seed(USER_PROXY_SEED)

# Persistent venue store, shared with backend/main.py so Wayfind can look up
# scenes uploaded via either entry point.
DEFAULT_DIMENSION_M = 15.0
MAP_DB_PATH = Path(
    os.environ.get(
        "SENSEGRID_DB_PATH",
        str(Path(tempfile.gettempdir()) / "dots_backend" / "maps.db"),
    )
)
map_store = MapStore(MAP_DB_PATH)


def _new_venue_id() -> str:
    """Short, URL-safe id printable on a QR/NFC tag."""
    return f"venue-{secrets.token_hex(3)}"

# ── Two-step paywall state machine ───────────────────────────────────────────
# ── State machine ─────────────────────────────────────────────────────────────
#
#   idle ─send images/urls─► quoted ─"yes"─► previewed ─"yes" + tx confirmed─► idle
#                               │                  │
#                               └─"no"─────────────┴──────────► idle (cancelled)

STATE_IDLE = "idle"
STATE_QUOTED = "quoted"
STATE_AWAITING_DIMENSION = "awaiting_dimension"
STATE_PREVIEWED = "previewed"

URL_REGEX = re.compile(r"https?://[^\s<>\"]+")
_MENTION_REGEX = re.compile(r"@\S+")
_TRAILING_PUNCT = ".,;:!?)\"']"

_YES = {"yes", "y", "proceed", "ok", "okay", "go", "continue", "sure", "confirm", "yep", "yeah"}
_NO = {"no", "n", "cancel", "stop", "nevermind", "abort", "nope"}
_PAID = {"paid", "sent", "done"}
_DIMENSION_SKIP = {"idk", "dunno", "skip", "default", "unknown"}
_NUMBER_REGEX = re.compile(r"(\d+(?:\.\d+)?)")


def _normalize(text: str) -> str:
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


# ── Image collection ──────────────────────────────────────────────────────────

def _save_data_uri(uri: str, index: int) -> str | None:
    """Decode a data: URI and save to a temp file. Returns path or None."""
    try:
        header, encoded = uri.split(",", 1)
        mime = header.split(":")[1].split(";")[0] if ":" in header else "image/jpeg"
        ext = "." + mime.split("/")[-1].replace("jpeg", "jpg")
        out = ARTIFACT_DIR / f"upload_{index}_{uuid4().hex[:8]}{ext}"
        out.write_bytes(base64.b64decode(encoded))
        return str(out)
    except Exception as exc:
        log.warning("failed to decode data URI at index %d: %s", index, exc)
        return None


async def _collect_image_sources(msg: ChatMessage) -> tuple[list[str], list[str]]:
    """
    Returns (urls, pre_paths).

    urls       — http(s) URLs found in text (for display / re-download later).
    pre_paths  — local paths already saved from ResourceContent attachments.

    Priority: ResourceContent attachments first; text URLs used only when no
    attachments are present.
    """
    pre_paths: list[str] = []

    for i, item in enumerate(msg.content):
        if not isinstance(item, ResourceContent):
            continue
        resources = item.resource if isinstance(item.resource, list) else [item.resource]
        for res in resources:
            uri = res.uri
            if uri.startswith("data:"):
                path = _save_data_uri(uri, i)
                if path:
                    pre_paths.append(path)
                    log.info("saved inline attachment #%d → %s", i, path)
            elif uri.startswith("http://") or uri.startswith("https://"):
                dl = await asyncio.to_thread(dispatch, "download_image", {"url": uri})
                if "error" not in dl:
                    pre_paths.append(dl["image_path"])
                    log.info("downloaded attachment #%d → %s", i, dl["image_path"])
                else:
                    log.warning("could not download attachment #%d: %s", i, dl["error"])

    # Only fall back to text URLs when no attachments were found.
    user_text = "".join(
        item.text for item in msg.content if isinstance(item, TextContent)
    )
    urls = _extract_urls(user_text) if not pre_paths else []

    return urls, pre_paths


# ── Core processing ───────────────────────────────────────────────────────────

async def _process_plans(
    urls: list[str],
    pre_paths: list[str],
    logger: logging.Logger,
    ctx: Context | None = None,
    sender: str | None = None,
) -> tuple[
    list[str],
    dict[str, int],
    list[dict],
    dict[str, int],
    str,
    list[str],
    dict | None,
    dict | None,
    str | None,
]:
    """Download each URL (pre_paths are already local) and run ADA + CV parse.

    Returns:
        (image_paths, room_summary, ada_findings, ada_summary,
         ada_report_text, ada_pdf_urls, first_floor_plan, first_dimensions_px,
         error_or_None)

    ``first_floor_plan`` is the structured scene from the first image — used to
    persist the venue for blind-user navigation.
    """
    image_paths: list[str] = list(pre_paths)  # already-downloaded attachments
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
    first_floor_plan: dict | None = None
    first_dimensions_px: dict | None = None

    # Download URL-sourced images.
    for url in urls:
        logger.info("downloading %s", url)
        dl = await asyncio.to_thread(dispatch, "download_image", {"url": url})
        if "error" in dl:
            return [], {}, [], {}, "", [], None, None, f"Couldn't download {url}: {dl['error']}"
        image_paths.append(dl["image_path"])

    if ctx and sender:
        await ctx.send(sender, _reply("Processing images and computing ADA compliance report..."))

    # Parse each image.
    for img_path in image_paths:
        parsed = await asyncio.to_thread(dispatch, "parse_floorplan", {"image_path": img_path})
        if "error" in parsed:
            return [], {}, [], {}, "", [], None, None, f"Couldn't compute ADA report for {img_path}: {parsed['error']}"

        if first_floor_plan is None:
            first_dimensions_px = parsed.get("dimensions_px") or None
            json_path = parsed.get("json_path")
            if isinstance(json_path, str) and Path(json_path).exists():
                try:
                    blob = json.loads(Path(json_path).read_text(encoding="utf-8"))
                    fp = blob.get("floor_plan") if isinstance(blob, dict) else None
                    if isinstance(fp, dict):
                        first_floor_plan = fp
                except Exception as exc:
                    logger.warning("could not load floor_plan from %s: %s", json_path, exc)

        for k, v in (parsed.get("rooms_by_type") or {}).items():
            room_summary[k] = room_summary.get(k, 0) + int(v)

        for k in ("total_findings", "high_severity", "medium_severity", "low_severity"):
            ada_summary[k] = ada_summary.get(k, 0) + int(
                (parsed.get("ada_summary") or {}).get(k, 0)
            )

        for rec in parsed.get("ada_findings", []) or []:
            if isinstance(rec, dict):
                ada_findings.append(rec)

        if not ada_report_text and isinstance(parsed.get("ada_report_text"), str):
            ada_report_text = parsed["ada_report_text"]

        pdf_path = parsed.get("ada_report_pdf_path")
        if isinstance(pdf_path, str) and pdf_path.strip():
            up_pdf = await asyncio.to_thread(dispatch, "upload_artifact", {"file_path": pdf_path})
            if "error" not in up_pdf and up_pdf.get("url"):
                ada_pdf_urls.append(up_pdf["url"])

    # De-duplicate findings across multi-image batches.
    seen: set = set()
    uniq: list[dict] = []
    for f in ada_findings:
        key = (f.get("category"), f.get("ada_reference"), f.get("observed_condition"))
        if key not in seen:
            seen.add(key)
            uniq.append(f)

    return (
        image_paths,
        room_summary,
        uniq,
        ada_summary,
        ada_report_text,
        ada_pdf_urls,
        first_floor_plan,
        first_dimensions_px,
        None,
    )


async def _build_tactile_content(
    image_paths: list[str],
    logger: logging.Logger,
    ctx: Context | None = None,
    sender: str | None = None,
) -> tuple[list[str], list[AgentContent], list[str]]:
    """
    Generate tactile PNGs via Nano Banana Pro for each image.

    Returns (tactile_urls, resource_content_items, errors).
    resource_content_items can be appended directly to a ChatMessage.content list.
    """
    tactile_urls: list[str] = []
    resource_items: list[AgentContent] = []
    errors: list[str] = []

    for i, img_path in enumerate(image_paths):
        name = Path(img_path).name
        if ctx and sender:
            await ctx.send(sender, _reply(f"Generating tactile map for image {i+1} of {len(image_paths)}..."))

        t = await asyncio.to_thread(
            dispatch,
            "tactile_map_from_image_nanobanana",
            {"image_path": img_path, "model": "gemini-3-pro-image-preview"},
        )
        if "error" in t:
            errors.append(f"{name}: {t['error']}")
            logger.warning("tactile nanobanana failed for %s: %s", img_path, t["error"])
            continue

        up = await asyncio.to_thread(dispatch, "upload_artifact", {"file_path": t["png_path"]})
        if "error" in up:
            errors.append(f"{name}: upload failed — {up['error']}")
            continue

        url = up["url"]
        tactile_urls.append(url)

        # Attach the image so clients render it inline, not just as a link.
        resource_items.append(
            ResourceContent(
                resource_id=uuid4(),
                resource=Resource(
                    uri=url,
                    metadata={"mime_type": "image/png", "role": "tactile_map"},
                ),
            )
        )

    return tactile_urls, resource_items, errors

# ── Main chat handler ─────────────────────────────────────────────────────────

async def handle_chat(
    ctx: Context, sender: str, msg: ChatMessage
) -> tuple[str, list[AgentContent]]:
    """
    Deterministic two-step paywall.
    Returns (reply_text, extra_content_items).
    extra_content_items are appended to the ChatMessage before EndSession.
    """
    user_text = _normalize(
        "".join(item.text for item in msg.content if isinstance(item, TextContent))
    )
    sess = _load_session(ctx, sender)
    state = sess.get("state", STATE_IDLE)
    intent = _intent(user_text)
    logger = ctx.logger

    no_extra: list[AgentContent] = []

    if intent == "no":
        _clear_session(ctx, sender)
        return "Cancelled. Send images whenever you're ready.", no_extra

    if state != STATE_IDLE and time.time() > sess.get("expires_at", 0):
        logger.info("session for %s expired — resetting", sender[:12])
        _clear_session(ctx, sender)
        sess, state = {"state": STATE_IDLE}, STATE_IDLE

    # Fresh images while mid-flow restart the quote.
    urls, pre_paths = await _collect_image_sources(msg)
    has_new_images = bool(urls or pre_paths)
    if state != STATE_IDLE and has_new_images:
        _clear_session(ctx, sender)
        sess, state = {"state": STATE_IDLE}, STATE_IDLE

    # ── STATE_IDLE ────────────────────────────────────────────────────────────
    if state == STATE_IDLE:
        if not has_new_images:
            return (
                "Send room or floor-plan images — attach them directly or paste http(s) URLs. "
                "I'll quote a price in test FET, then deliver a tactile map once payment lands.",
                no_extra,
            )

        n_images = len(urls) + len(pre_paths)
        q = quote_for(n_images)
        sess = {
            "state": STATE_QUOTED,
            "urls": urls,
            "pre_paths": pre_paths,
            "quoted_atestfet": q.total_atestfet,
            "quoted_fet_str": q.total_fet_str,
            "n_images": n_images,
            "expires_at": time.time() + PAYMENT_TIMEOUT_S,
        }
        _save_session(ctx, sender, sess)
        plural = "s" if n_images > 1 else ""
        return (
            f"Quote: **{q.total_fet_str} test FET** for {n_images} image{plural}.\n\n"
            "Reply 'yes' to proceed, or 'no' to cancel.",
            no_extra,
        )

    # ── STATE_QUOTED ──────────────────────────────────────────────────────────
    if state == STATE_QUOTED:
        if intent != "yes":
            return (
                f"Reply 'yes' to proceed with the quote of "
                f"{sess['quoted_fet_str']} test FET, or 'no' to cancel.",
                no_extra,
            )

        (
            image_paths,
            room_summary,
            ada_findings,
            ada_summary,
            ada_report_text,
            ada_pdf_urls,
            first_floor_plan,
            first_dimensions_px,
            err,
        ) = await _process_plans(
            sess.get("urls", []),
            sess.get("pre_paths", []),
            logger=logger,
            ctx=ctx,
            sender=sender,
        )
        if err:
            _clear_session(ctx, sender)
            return err, no_extra

        pay_addr = str(agent.wallet.address())
        try:
            start_balance = await asyncio.to_thread(address_balance_atestfet, pay_addr)
        except Exception as exc:
            logger.exception("ledger query failed")
            _clear_session(ctx, sender)
            return f"Couldn't reach Fetch.ai testnet ledger: {exc}", no_extra

        sess.update(
            {
                "state": STATE_AWAITING_DIMENSION,
                "image_paths": image_paths,
                "room_summary": room_summary,
                "ada_findings": ada_findings,
                "ada_summary": ada_summary,
                "ada_report_text": ada_report_text,
                "ada_pdf_urls": ada_pdf_urls,
                "floor_plan": first_floor_plan,
                "dimensions_px": first_dimensions_px,
                "start_balance_atestfet": start_balance,
                "pay_address": pay_addr,
                "expires_at": time.time() + PAYMENT_TIMEOUT_S,
            }
        )
        _save_session(ctx, sender, sess)

        return (
            "Parse complete. One quick question to make the navigation map "
            "metric: **do you know the longest wall length in meters?** "
            f"Reply with a number (e.g. `12`), or `no` / `skip` to use the "
            f"default of {DEFAULT_DIMENSION_M:g} m.",
            no_extra,
        )

    if state == STATE_AWAITING_DIMENSION:
        longest_wall_m: float | None = None
        normalized = _normalize(user_text).lower()
        words = set(re.findall(r"[a-z']+", normalized))

        if intent == "no" or words & _DIMENSION_SKIP:
            longest_wall_m = DEFAULT_DIMENSION_M
        else:
            m = _NUMBER_REGEX.search(normalized)
            if m:
                try:
                    val = float(m.group(1))
                    if 0.5 <= val <= 500.0:
                        longest_wall_m = val
                except ValueError:
                    pass

        if longest_wall_m is None:
            return (
                "I need a number in meters for the longest wall — e.g. `12` — "
                f"or reply `skip` to use the default of {DEFAULT_DIMENSION_M:g} m.",
                no_extra,
            )

        sess["longest_wall_m"] = longest_wall_m
        sess["state"] = STATE_PREVIEWED
        sess["expires_at"] = time.time() + PAYMENT_TIMEOUT_S
        _save_session(ctx, sender, sess)

        room_summary = sess.get("room_summary") or {}
        ada_findings = sess.get("ada_findings") or []
        ada_summary = sess.get("ada_summary") or {}
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

        n = sess["n_images"]
        plural = "s" if n > 1 else ""
        user_addr = str(user_wallet.address())
        return (
            f"Parsed {sum(room_summary.values())} rooms across {n} image{plural} "
            f"({rooms_str}). Longest wall: **{longest_wall_m:g} m**.\n\n"
            f"{pdf_block}"
            f"{ada_block}"
            f"Reply **'yes'** to authorize payment of **{sess['quoted_fet_str']} test FET** "
            f"— I'll execute the transfer on-chain (Dorado) automatically, deliver the "
            f"tactile map, and issue a navigation **venue ID** you can post at the "
            f"entrance for blind visitors. Reply 'no' to cancel.\n\n"
            f"_Demo wallet: `{user_addr}` — top up at {FAUCET_URL}/{user_addr} if needed._",
            no_extra,
        )

    # ── STATE_PREVIEWED ───────────────────────────────────────────────────────
    if state == STATE_PREVIEWED:
        if intent not in {"yes", "paid"}:
            return (
                "Reply 'yes' to authorize the autonomous payment, or 'no' to cancel.",
                no_extra,
            )

        amount = int(sess["quoted_atestfet"])
        user_addr = str(user_wallet.address())
        try:
            user_balance = await asyncio.to_thread(address_balance_atestfet, user_addr)
        except Exception as exc:
            logger.exception("ledger query failed before send")
            return f"Couldn't reach the ledger: {exc}. Try again in a few seconds.", no_extra

        if user_balance < amount + GAS_BUFFER_ATESTFET:
            return (
                f"Demo user wallet `{user_addr}` doesn't have enough test FET "
                f"(has {atestfet_to_fet_str(user_balance)}, needs ≥ "
                f"{atestfet_to_fet_str(amount + GAS_BUFFER_ATESTFET)} including gas).\n\n"
                f"Top it up once at {FAUCET_URL}/{user_addr} and reply 'yes' again.",
                no_extra,
            )

        try:
            tx_hash = await asyncio.to_thread(send_tokens, user_wallet, sess["pay_address"], amount)
            logger.info("payment broadcast: %s atestfet → %s (tx %s)",
                        amount, sess["pay_address"], tx_hash)
        except Exception as exc:
            logger.exception("send_tokens failed")
            return (
                f"Payment broadcast failed: {exc}. Reply 'yes' to retry or 'no' to cancel.",
                no_extra,
            )

        try:
            received, current = await asyncio.to_thread(
                payment_received,
                sess["pay_address"],
                int(sess["start_balance_atestfet"]),
                amount,
            )
        except Exception as exc:
            logger.exception("ledger query failed during verify")
            return (
                f"Tx {tx_hash} broadcast but couldn't verify on-chain receipt: {exc}. "
                "Check the Dorado explorer and reply 'yes' to retry.",
                no_extra,
            )

        if not received:
            got = max(0, current - int(sess["start_balance_atestfet"]))
            return (
                f"Tx {tx_hash} broadcast but balance hasn't reflected yet "
                f"(saw {atestfet_to_fet_str(got)} of {sess['quoted_fet_str']} expected). "
                "Reply 'yes' in a few seconds to retry.",
                no_extra,
            )

        # Build the final image list: prefer already-processed paths, re-download URLs if needed.
        img_paths: list[str] = list(sess.get("image_paths") or [])
        if not img_paths:
            for p in sess.get("pre_paths", []) or []:
                if Path(p).exists():
                    img_paths.append(p)
            for url in sess.get("urls", []) or []:
                dl = await asyncio.to_thread(dispatch, "download_image", {"url": url})
                if "error" not in dl:
                    img_paths.append(dl["image_path"])

        tactile_urls, resource_items, gen_errors = await _build_tactile_content(
            img_paths, logger, ctx=ctx, sender=sender
        )

        # Persist the venue so the Wayfind agent can answer questions about
        # this floor plan for blind visitors.
        venue_id: str | None = None
        floor_plan = sess.get("floor_plan") if isinstance(sess.get("floor_plan"), dict) else None
        if floor_plan:
            venue_id = _new_venue_id()
            try:
                map_store.put_map(
                    map_id=venue_id,
                    layout_2d=floor_plan,
                    metadata={
                        "source": "sensegrid_agent",
                        "longest_wall_m": float(sess.get("longest_wall_m") or DEFAULT_DIMENSION_M),
                        "dimensions_px": sess.get("dimensions_px") or {},
                        "rooms_by_type": sess.get("room_summary") or {},
                        "owner": sender,
                    },
                    tactile_png_url=tactile_urls[0] if tactile_urls else None,
                )
                logger.info("persisted venue %s for sender %s", venue_id, sender[:12] + "…")
            except Exception as exc:
                logger.exception("failed to persist venue: %s", exc)
                venue_id = None

        _clear_session(ctx, sender)
        lines = [
            f"Payment of {sess['quoted_fet_str']} test FET confirmed on-chain (tx `{tx_hash}`)."
        ]
        if tactile_urls:
            lines.append("\nTactile map URL(s):")
            lines.extend(f"- {u}" for u in tactile_urls)
        if gen_errors and not tactile_urls:
            lines.append("\nTactile map generation failed:")
            lines.extend(f"- {e}" for e in gen_errors[:6])

        if venue_id:
            lines.append(
                f"\n**Navigation venue ID:** `{venue_id}`\n"
                f"Print this on a QR code or NFC tag at the entrance. Blind "
                f"visitors give this id to the Wayfind agent on ASI:One to ask "
                f"voice questions about your space."
            )

        lines.append("\nThanks!")

        return "\n".join(lines), resource_items

    return "Send images to get started.", no_extra


# ── uagent + chat protocol plumbing ──────────────────────────────────────────

agent = Agent(
    name="sensegrid",
    seed=AGENT_SEED,
    port=8001,
    mailbox=True,
    handle="sensegrid",
    description="Send room/floorplan images and receive a tactile accessibility map.",
    readme_path=str(Path(__file__).resolve().parent / "SENSEGRID_README.md"),
    publish_agent_details=True,
)

protocol = Protocol(spec=chat_protocol_spec)


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


@protocol.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc),
            acknowledged_msg_id=msg.msg_id,
        ),
    )

    ctx.logger.info("chat from %s", sender[:12] + "…")

    try:
        reply_text, extra_content = await handle_chat(ctx, sender, msg)
    except Exception as exc:
        ctx.logger.exception("payment flow crashed")
        reply_text, extra_content = f"Something went wrong: {exc}", []

    await ctx.send(sender, _reply(reply_text, extra_content))


@protocol.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.debug("ack from %s for %s", sender[:12] + "…", msg.acknowledged_msg_id)


agent.include(protocol, publish_manifest=True)


# ── Wayfind venue lookup protocol (agent-to-agent) ──────────────────────────

wayfind_protocol = Protocol(name="wayfind-venue", version="0.1.0")


def _venue_info_for(venue_id: str) -> VenueInfo:
    rec = map_store.get_map(venue_id)
    if rec is None or not rec.layout_2d:
        return VenueInfo(found=False, error="venue not found", venue_id=venue_id)

    meta = rec.metadata or {}
    longest_wall_m = float(meta.get("longest_wall_m") or DEFAULT_DIMENSION_M)
    dims_px = meta.get("dimensions_px") or {}
    width_px = float(dims_px.get("width") or 1.0)
    height_px = float(dims_px.get("height") or 1.0)

    # Map the longer pixel dimension to longest_wall_m, scale the shorter
    # proportionally. Image y is flipped in the meter frame: (norm_x, norm_y)
    # in 0–100 maps to (room_width_m * x/100, room_height_m * (1 - y/100)).
    if width_px >= height_px:
        room_width_m = longest_wall_m
        room_height_m = longest_wall_m * (height_px / width_px)
    else:
        room_height_m = longest_wall_m
        room_width_m = longest_wall_m * (width_px / height_px)

    return VenueInfo(
        found=True,
        venue_id=venue_id,
        venue_label=str(meta.get("label") or ""),
        scene_json=json.dumps(rec.layout_2d, separators=(",", ":")),
        room_width_m=room_width_m,
        room_height_m=room_height_m,
        # Bottom-center of the image = front of the room, on the meter floor.
        entrance_x_m=room_width_m / 2.0,
        entrance_y_m=0.0,
        entrance_heading_deg=90.0,
    )


@wayfind_protocol.on_message(VenueLookup, replies={VenueInfo})
async def on_venue_lookup(ctx: Context, sender: str, msg: VenueLookup):
    ctx.logger.info("VenueLookup from %s for %s", sender[:12] + "…", msg.venue_id)
    info = _venue_info_for(msg.venue_id.strip())
    await ctx.send(sender, info)


agent.include(wayfind_protocol, publish_manifest=True)


if __name__ == "__main__":
    log.info("SenseGrid starting.")
    log.info("Agent address (set as SENSEGRID_AGENT_ADDRESS for Wayfind): %s", agent.address)
    log.info("Service wallet (receives payments): %s", agent.wallet.address())
    log.info("Demo user-proxy wallet (auto-pays):  %s", user_wallet.address())
    log.info("Fund the user-proxy once at: %s/%s", FAUCET_URL, user_wallet.address())
    agent.run()
