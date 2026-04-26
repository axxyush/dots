"""SenseGrid — Fetch.ai chat agent for generating tactile floorplan maps with a test-FET paywall.

Flow (deterministic state machine; payment is NOT trusted to an LLM):
  1. User sends room/floorplan images — attached directly OR as http(s) URLs.
  2. Agent quotes a price in test FET (Dorado), waits for "yes".
  3. On "yes", processes images, shows ADA report preview + payment instructions.
  4. On "yes" again, executes autonomous on-chain payment, delivers:
       - tactile PNG URL (tmpfiles.org, ~60 min)
       - tactile PNG embedded as image attachment in the reply
       - venue ID for the Wayfind navigation agent

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

# ── Config ────────────────────────────────────────────────────────────────────

AGENT_SEED = os.environ.get("SENSEGRID_AGENT_SEED", "braille-map-seed-phrase")

USER_PROXY_SEED = os.environ.get("SENSEGRID_USER_SEED", f"{AGENT_SEED}-user-proxy")
user_wallet = make_wallet_from_seed(USER_PROXY_SEED)

DEFAULT_DIMENSION_M = 15.0
MAP_DB_PATH = Path(
    os.environ.get(
        "SENSEGRID_DB_PATH",
        str(Path(tempfile.gettempdir()) / "dots_backend" / "maps.db"),
    )
)
map_store = MapStore(MAP_DB_PATH)


def _new_venue_id() -> str:
    return f"venue-{secrets.token_hex(3)}"


# ── State machine ─────────────────────────────────────────────────────────────
#
#   idle ─send images/urls─► quoted ─"yes"─► previewed ─"yes"─► idle (delivered)
#                               │                │
#                               └──"no"──────────┴──────────► idle (cancelled)

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


def _collect_image_sources(msg: ChatMessage) -> tuple[list[str], list[str]]:
    """
    Returns (urls, pre_paths).
    urls       — http(s) URLs found in text (only when no attachments present).
    pre_paths  — local paths saved from ResourceContent attachments.
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
                dl = dispatch("download_image", {"url": uri})
                if "error" not in dl:
                    pre_paths.append(dl["image_path"])
                    log.info("downloaded attachment #%d → %s", i, dl["image_path"])
                else:
                    log.warning("could not download attachment #%d: %s", i, dl["error"])

    user_text = "".join(
        item.text for item in msg.content if isinstance(item, TextContent)
    )
    urls = _extract_urls(user_text) if not pre_paths else []
    return urls, pre_paths


# ── Core processing ───────────────────────────────────────────────────────────

def _process_plans(
    urls: list[str],
    pre_paths: list[str],
    logger: logging.Logger,
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
    """Download each URL and run CV parse + ADA report.

    Returns:
        (image_paths, room_summary, ada_findings, ada_summary,
         ada_report_text, ada_pdf_urls, first_floor_plan,
         first_dimensions_px, error_or_None)
    """
    image_paths: list[str] = list(pre_paths)
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

    for url in urls:
        logger.info("downloading %s", url)
        dl = dispatch("download_image", {"url": url})
        if "error" in dl:
            return [], {}, [], {}, "", [], None, None, f"Couldn't download {url}: {dl['error']}"
        image_paths.append(dl["image_path"])

    for img_path in image_paths:
        parsed = dispatch("parse_floorplan", {"image_path": img_path})
        if "error" in parsed:
            return (
                [], {}, [], {}, "", [], None, None,
                f"Couldn't parse {Path(img_path).name}: {parsed['error']}",
            )

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
            up_pdf = dispatch("upload_artifact", {"file_path": pdf_path})
            if "error" not in up_pdf and up_pdf.get("url"):
                ada_pdf_urls.append(up_pdf["url"])

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


TACTILE_API_BASE = "http://66.42.127.155:8000"


def _tactile_via_upload(local_path: str, logger: logging.Logger) -> dict:
    """POST local file bytes directly to /tactile/from-upload (no tmpfiles middleman)."""
    import urllib.request, urllib.parse, json as _json, mimetypes, uuid as _uuid
    p = Path(local_path)
    data = p.read_bytes()
    mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"

    # Build a simple multipart/form-data body.
    boundary = f"----FormBoundary{_uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{p.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{TACTILE_API_BASE}/tactile/from-upload",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body_resp = _json.loads(resp.read())
            url = body_resp.get("tactile_png_url")
            if url:
                logger.info("tactile API (upload) returned: %s", url)
                return {"url": url}
            return {"error": f"API returned no URL: {body_resp}"}
    except Exception as exc:
        return {"error": str(exc)}


def _tactile_via_url(image_url: str, logger: logging.Logger) -> dict:
    """POST a public image URL to /tactile/from-url."""
    import urllib.request, json as _json
    payload = _json.dumps({
        "image_url": image_url,
        "model": "gemini-3-pro-image-preview",
    }).encode()
    req = urllib.request.Request(
        f"{TACTILE_API_BASE}/tactile/from-url",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = _json.loads(resp.read())
            url = body.get("tactile_png_url")
            if url:
                logger.info("tactile API (url) returned: %s", url)
                return {"url": url}
            return {"error": f"API returned no URL: {body}"}
    except Exception as exc:
        return {"error": str(exc)}


def _build_tactile_content(
    image_sources: list[str],  # may be local paths OR https:// URLs
    logger: logging.Logger,
) -> tuple[list[str], list[AgentContent], list[str]]:
    """Generate tactile PNGs via the remote Nano Banana API."""
    tactile_urls: list[str] = []
    resource_items: list[AgentContent] = []
    errors: list[str] = []

    for src in image_sources:
        if src.startswith("http"):
            # Remote URL — pass directly to the server.
            logger.info("tactile: calling API (url) for %s", src)
            result = _tactile_via_url(src, logger)
        else:
            # Local file — upload bytes directly, skip tmpfiles.org entirely.
            logger.info("tactile: calling API (upload) for %s", Path(src).name)
            result = _tactile_via_upload(src, logger)

        if "error" in result:
            errors.append(f"{src}: {result['error']}")
            logger.warning("tactile API failed for %s: %s", src, result["error"])
            continue

        url = result["url"]
        tactile_urls.append(url)
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

def handle_chat(
    ctx, sender: str, msg: ChatMessage
) -> tuple[str, list[AgentContent]]:
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

    urls, pre_paths = _collect_image_sources(msg)
    has_new_images = bool(urls or pre_paths)
    if state != STATE_IDLE and has_new_images:
        _clear_session(ctx, sender)
        sess, state = {"state": STATE_IDLE}, STATE_IDLE

    # ── manavsharma shortcut: skip payment/ADA, go straight to tactile ──────
    _BYPASS_KEYWORD = "manavsharma"
    if _BYPASS_KEYWORD in user_text.lower():
        if not has_new_images:
            return (
                "Send images along with the keyword to use the bypass shortcut.",
                no_extra,
            )
        # Pass URLs directly to the API; only download pre-attached local paths.
        image_sources: list[str] = list(urls) + list(pre_paths)
        if not image_sources:
            return "Could not find any images — please try again.", no_extra

        logger.info("manavsharma bypass: %d source(s) → tactile API", len(image_sources))
        tactile_urls, tactile_resources, t_errors = _build_tactile_content(image_sources, logger)

        if tactile_urls:
            up_list = "\n".join(f"- {u}" for u in tactile_urls)
            return (
                f"✅ **Bypass mode** — tactile map generated!\n\n"
                f"**Download link(s):**\n{up_list}",
                tactile_resources,
            )
        err_str = "; ".join(t_errors) if t_errors else "unknown error"
        return f"Bypass tactile generation failed: {err_str}", no_extra

    # ── STATE_IDLE normal flow ────────────────────────────────────────────────
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
        ) = _process_plans(
            sess.get("urls", []),
            sess.get("pre_paths", []),
            logger=logger,
        )
        if err:
            _clear_session(ctx, sender)
            return err, no_extra

        pay_addr = str(agent.wallet.address())
        try:
            start_balance = address_balance_atestfet(pay_addr)
        except Exception as exc:
            logger.exception("ledger query failed")
            _clear_session(ctx, sender)
            return f"Couldn't reach Fetch.ai testnet ledger: {exc}", no_extra

        sess.update(
            {
                "state": STATE_PREVIEWED,
                "longest_wall_m": DEFAULT_DIMENSION_M,
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

        rooms_str = ", ".join(f"{k}: {v}" for k, v in sorted(room_summary.items()))
        pdfs = "\n".join(f"- {u}" for u in ada_pdf_urls)
        pdf_block = f"**ADA report (PDF):**\n{pdfs}\n\n" if pdfs else ""
        ada_text = (ada_report_text or "").strip()
        if ada_text:
            ada_block = f"**Preliminary ADA Compliance Report**\n\n{ada_text}\n\n"
        elif ada_findings:
            ada_block = (
                "**Preliminary ADA Compliance Report**\n\n"
                f"Findings: {ada_summary.get('total_findings', 0)} "
                f"(High {ada_summary.get('high_severity', 0)}, "
                f"Medium {ada_summary.get('medium_severity', 0)}, "
                f"Low {ada_summary.get('low_severity', 0)}).\n\n"
            )
        else:
            ada_block = ""

        n = sess["n_images"]
        plural = "s" if n > 1 else ""
        user_addr = str(user_wallet.address())
        return (
            f"Parsed {sum(room_summary.values())} rooms across {n} image{plural} "
            f"({rooms_str}).\n\n"
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
            user_balance = address_balance_atestfet(user_addr)
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
            tx_hash = send_tokens(user_wallet, sess["pay_address"], amount)
            logger.info(
                "payment broadcast: %s atestfet → %s (tx %s)",
                amount, sess["pay_address"], tx_hash,
            )
        except Exception as exc:
            logger.exception("send_tokens failed")
            return (
                f"Payment broadcast failed: {exc}. Reply 'yes' to retry or 'no' to cancel.",
                no_extra,
            )

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

        img_paths: list[str] = list(sess.get("image_paths") or [])
        if not img_paths:
            for p in sess.get("pre_paths", []) or []:
                if Path(p).exists():
                    img_paths.append(p)
            for url in sess.get("urls", []) or []:
                dl = dispatch("download_image", {"url": url})
                if "error" not in dl:
                    img_paths.append(dl["image_path"])

        tactile_urls, resource_items, gen_errors = _build_tactile_content(img_paths, logger)

        # Persist the venue for the Wayfind navigation agent.
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
                "Print this on a QR code or NFC tag at the entrance. Blind visitors "
                "give this ID to the Wayfind agent on ASI:One to ask voice questions "
                "about your space."
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
        loop = asyncio.get_event_loop()
        reply_text, extra_content = await loop.run_in_executor(
            None, handle_chat, ctx, sender, msg
        )
    except Exception as exc:
        ctx.logger.exception("payment flow crashed")
        reply_text, extra_content = f"Something went wrong: {exc}", []
    await ctx.send(sender, _reply(reply_text, extra_content))


@protocol.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.debug("ack from %s for %s", sender[:12] + "…", msg.acknowledged_msg_id)


agent.include(protocol, publish_manifest=True)


# ── Wayfind venue lookup protocol (agent-to-agent) ───────────────────────────

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
    log.info("Agent address: %s", agent.address)
    log.info("Service wallet (receives payments): %s", agent.wallet.address())
    log.info("Demo user-proxy wallet (auto-pays):  %s", user_wallet.address())
    log.info("Fund the user-proxy once at: %s/%s", FAUCET_URL, user_wallet.address())
    agent.run()
