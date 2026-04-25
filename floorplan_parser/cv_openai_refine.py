"""
OpenAI (GPT-4o vision) refinement for CV-extracted room regions.

Mirror of `cv_gemini_refine.label_regions_global` but uses OpenAI's vision
API. Produces higher-quality labels on dense institutional plans in many
cases because of stronger OCR-style label reading.

The module is intentionally optional — callers should feature-detect it
with `has_openai_key()` / try/except the import.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
from typing import Any

import numpy as np

from cv_rooms import RoomRegion, _bbox_pct, _dedup_regions

log = logging.getLogger("cv_openai_refine")


_ALLOWED_TYPES = (
    "store", "restaurant", "restroom", "elevator", "stairs", "door",
    "corridor", "fire_exit", "fire_extinguisher", "fire_alarm", "rest_area",
    "office", "cafe", "service_counter", "label", "entrance",
    "bedroom", "bathroom", "living_room", "kitchen", "dining_room", "hallway",
    "classroom", "laboratory", "library", "auditorium", "gym",
    "music_room", "art_room", "staff_room", "reading_room",
    "computer_lab", "courtyard", "multimedia_room", "general_office",
    "utility", "lobby", "reception", "unknown",
)


def has_openai_key() -> bool:
    return bool((os.environ.get("OPENAI_API_KEY") or "").strip())


def _encode_png(image_bgr: np.ndarray) -> bytes:
    import cv2

    ok, buf = cv2.imencode(".png", image_bgr)
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    return buf.tobytes()


def _png_to_data_url(png_bytes: bytes) -> str:
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _extract_json(text: str) -> Any:
    """Robust JSON extraction that tolerates markdown fences + prose."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        s, e = text.find(open_c), text.rfind(close_c)
        if s != -1 and e != -1 and e > s:
            try:
                return json.loads(text[s : e + 1])
            except json.JSONDecodeError:
                continue
    return None


def _make_openai_client(api_key: str | None = None):
    """Lazy import so the module is optional."""
    from openai import OpenAI  # type: ignore

    return OpenAI(api_key=(api_key or os.environ.get("OPENAI_API_KEY")))


def _vision_generate(
    client,
    model_name: str,
    prompt: str,
    png_bytes: bytes,
    *,
    temperature: float = 0.0,
    max_completion_tokens: int = 4096,
    force_json_object: bool = True,
) -> str:
    """Call the Chat Completions API with an inline image attachment.

    Uses `max_completion_tokens` (new param name; required for gpt-5.x/o*
    models and accepted by gpt-4.x/gpt-4o). Automatically retries without
    `temperature` on models that reject non-default temps (gpt-5.5+), and
    without `response_format` on models that reject it.
    """
    messages = [
        {
            "role": "system",
            "content": (
                "You are a careful floor-plan annotation assistant. Prefer reading "
                "text printed inside regions verbatim over guessing. Return JSON only."
            ),
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": _png_to_data_url(png_bytes), "detail": "high"},
                },
            ],
        },
    ]
    base: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
        "max_completion_tokens": max_completion_tokens,
    }
    if force_json_object:
        base["response_format"] = {"type": "json_object"}
    attempts: list[dict[str, Any]] = [
        {**base, "temperature": temperature},
        base,
        {k: v for k, v in base.items() if k != "response_format"},
    ]
    last_exc: Exception | None = None
    for i, kwargs in enumerate(attempts):
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as exc:
            msg = str(exc)
            log.warning("cv_openai_refine attempt %d/%d failed (%s)",
                        i + 1, len(attempts), msg[:200])
            last_exc = exc
            if "temperature" not in msg and "response_format" not in msg:
                raise
            continue
        return (resp.choices[0].message.content or "") if resp.choices else ""
    assert last_exc is not None
    raise last_exc


# ── Numbered overlay for global labeling (matches Gemini implementation) ────


_GLOBAL_LABEL_CAPTION_COLOR = (0, 0, 255)   # red numbers (BGR) for contrast
_GLOBAL_LABEL_BOX_COLOR = (255, 165, 0)     # orange boxes (BGR-ish)


def _render_numbered_overlay(
    image_bgr: np.ndarray,
    regions: list[RoomRegion],
    *,
    min_long_side: int = 1600,
) -> np.ndarray:
    """Draw numbered orange boxes on a copy of the floor plan (upscaled if tiny)."""
    import cv2

    h, w = image_bgr.shape[:2]
    long_side = max(h, w)
    scale = max(1.0, min_long_side / float(long_side))
    vis = image_bgr.copy()
    if scale > 1.0:
        vis = cv2.resize(
            vis,
            (int(round(w * scale)), int(round(h * scale))),
            interpolation=cv2.INTER_CUBIC,
        )
    for r in regions:
        x = int(round(r.bbox_px["x"] * scale))
        y = int(round(r.bbox_px["y"] * scale))
        bw = int(round(r.bbox_px["w"] * scale))
        bh = int(round(r.bbox_px["h"] * scale))
        cv2.rectangle(vis, (x, y), (x + bw, y + bh), _GLOBAL_LABEL_BOX_COLOR, 2)
        text = r.id
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
        tx = x + 4
        ty = y + th + 6
        cv2.putText(vis, text, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (255, 255, 255), 5, cv2.LINE_AA)
        cv2.putText(vis, text, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    _GLOBAL_LABEL_CAPTION_COLOR, 2, cv2.LINE_AA)
    return vis


# ── Public: global labeling (preferred) ─────────────────────────────────────


def label_regions_global(
    image_bgr: np.ndarray,
    regions: list[RoomRegion],
    *,
    client=None,
    model_name: str = "gpt-5.4",
    api_key: str | None = None,
) -> list[RoomRegion]:
    """
    Single OpenAI vision call over the whole plan (with numbered boxes).
    Returns a new region list. Unlabeled regions pass through unchanged.
    """
    if not regions:
        return regions
    if client is None:
        client = _make_openai_client(api_key=api_key)

    overlay = _render_numbered_overlay(image_bgr, regions)
    png = _encode_png(overlay)

    allowed_str = ", ".join(_ALLOWED_TYPES)
    ids = [r.id for r in regions]
    prompt = (
        "You are annotating a floor plan. Orange rectangles mark detected "
        "rooms/regions. Each rectangle has a red ID label at its top-left "
        "(e.g. 'cv_room_0').\n\n"
        "For EVERY ID in this list, return two things:\n"
        "  1. 'label' — the exact text printed INSIDE that box on the floor "
        "plan (e.g. 'Laboratory', 'Multi-Media Room', 'Staff Room'). Must be "
        "verbatim from the plan (preserve capitalization). If no text is "
        "visible, infer from icons/symbols (bed -> 'Bedroom', toilet -> "
        "'Restroom', stairs symbol -> 'Stairs'). Use null only if the box is "
        "clearly not a room (blank background, exterior, duplicate).\n"
        f"  2. 'type' — one canonical category from: {allowed_str}. Pick the "
        "closest match. Use 'unknown' only as a last resort.\n\n"
        "IDs to annotate: " + ", ".join(ids) + ".\n\n"
        "Return a single JSON object ONLY (no prose, no fences) in this schema:\n"
        "{\n"
        "  \"rooms\": [\n"
        "    {\"id\": \"cv_room_0\", \"label\": \"Laboratory\", \"type\": \"laboratory\"},\n"
        "    {\"id\": \"cv_room_1\", \"label\": \"Hallway\",    \"type\": \"hallway\"}\n"
        "  ]\n"
        "}\n"
        "Include every ID. If unreadable, set label to null and type to 'unknown'."
    )

    try:
        raw = _vision_generate(client, model_name, prompt, png)
    except Exception as exc:
        log.warning("OpenAI label_regions_global failed: %s", exc)
        return regions

    parsed = _extract_json(raw)
    lookup: dict[str, dict] = {}
    items: list = []
    if isinstance(parsed, dict) and isinstance(parsed.get("rooms"), list):
        items = parsed["rooms"]
    elif isinstance(parsed, list):
        items = parsed
    for item in items:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id", "")).strip()
        if rid:
            lookup[rid] = item

    out: list[RoomRegion] = []
    labeled_count = 0
    for r in regions:
        entry = lookup.get(r.id)
        if not entry:
            out.append(r)
            continue
        t_raw = str(entry.get("type", "") or "").strip().lower().replace(" ", "_")
        t = t_raw if t_raw in _ALLOWED_TYPES else None
        lbl_raw = entry.get("label")
        lbl = str(lbl_raw).strip() if isinstance(lbl_raw, str) else None
        if lbl == "" or (isinstance(lbl, str) and lbl.lower() in ("null", "none")):
            lbl = None
        if t or lbl:
            labeled_count += 1
        out.append(
            RoomRegion(
                id=r.id,
                bbox_px=r.bbox_px,
                bbox_pct=r.bbox_pct,
                area_px=r.area_px,
                source=r.source,
                label_hint=t or r.label_hint,
                label_text=lbl or r.label_text,
            )
        )
    log.info("OpenAI label_regions_global: %d/%d regions labeled", labeled_count, len(regions))
    return out


# ── Public: gap-fill pass (optional) ────────────────────────────────────────


def _draw_overlay(image_bgr: np.ndarray, regions: list[RoomRegion]) -> np.ndarray:
    import cv2

    vis = image_bgr.copy()
    for r in regions:
        x, y = r.bbox_px["x"], r.bbox_px["y"]
        w, h = r.bbox_px["w"], r.bbox_px["h"]
        cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 140, 255), 2)
    return vis


def find_missed_regions(
    image_bgr: np.ndarray,
    existing: list[RoomRegion],
    *,
    client=None,
    model_name: str = "gpt-5.4",
    api_key: str | None = None,
    max_new: int = 50,
    min_overlay_long_side: int = 1400,
) -> list[RoomRegion]:
    """Ask OpenAI for rooms/pathways the CV pass missed (bbox % coords)."""
    import cv2

    if client is None:
        client = _make_openai_client(api_key=api_key)

    h, w = image_bgr.shape[:2]
    overlay = _draw_overlay(image_bgr, existing)
    long_side = max(h, w)
    if long_side < min_overlay_long_side:
        scale = min_overlay_long_side / float(long_side)
        new_w = int(round(w * scale))
        new_h = int(round(h * scale))
        overlay = cv2.resize(overlay, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    png = _encode_png(overlay)

    prompt = (
        "You are helping complete a floor-plan parse. Orange rectangles mark "
        "rooms/regions already detected. List enclosed rooms or major "
        "pathways that are CLEARLY visible but NOT already inside an orange "
        "rectangle.\n\n"
        "Return ONLY this JSON shape (no prose, no fences):\n"
        "{\"rooms\": ["
        "  {\"type\": \"<one of: store, restaurant, restroom, elevator, stairs, "
        "corridor, rest_area, office, cafe, service_counter, entrance, "
        "bedroom, bathroom, living_room, kitchen, dining_room, hallway, "
        "unknown>\", "
        "  \"x\": <0-100>, \"y\": <0-100>, \"w\": <0-100>, \"h\": <0-100>}"
        "]}\n"
        "Coordinates are percentages from the top-left. Be conservative. "
        f"Report at most {max_new} items. Do NOT re-report regions already "
        "inside an orange box."
    )

    try:
        raw = _vision_generate(client, model_name, prompt, png)
    except Exception as exc:
        log.warning("OpenAI find_missed_regions failed: %s", exc)
        return []

    parsed = _extract_json(raw)
    items: list = []
    if isinstance(parsed, dict) and isinstance(parsed.get("rooms"), list):
        items = parsed["rooms"]
    elif isinstance(parsed, list):
        items = parsed
    if not items:
        log.info("OpenAI find_missed_regions: no rooms (raw=%r)", (raw or "")[:160])
        return []

    new_regions: list[RoomRegion] = []
    for i, item in enumerate(items[:max_new]):
        if not isinstance(item, dict):
            continue
        try:
            x = float(item.get("x", 0))
            y = float(item.get("y", 0))
            bw = float(item.get("w", 0))
            bh = float(item.get("h", 0))
            t = str(item.get("type", "unknown")).strip().lower()
        except Exception:
            continue
        if bw <= 0.5 or bh <= 0.5:
            continue
        if t not in _ALLOWED_TYPES:
            t = "unknown"

        px_x = int(round(max(0.0, min(100.0, x)) / 100.0 * w))
        px_y = int(round(max(0.0, min(100.0, y)) / 100.0 * h))
        px_w = int(round(max(0.0, min(100.0, bw)) / 100.0 * w))
        px_h = int(round(max(0.0, min(100.0, bh)) / 100.0 * h))
        px_w = max(4, min(w - px_x, px_w))
        px_h = max(4, min(h - px_y, px_h))

        bbox_px = {"x": px_x, "y": px_y, "w": px_w, "h": px_h}
        new_regions.append(
            RoomRegion(
                id=f"openai_room_{i}",
                bbox_px=bbox_px,
                bbox_pct=_bbox_pct(bbox_px, w, h),
                area_px=px_w * px_h,
                source="openai_gapfill",
                label_hint=t if t != "unknown" else None,
            )
        )

    merged = _dedup_regions(existing + new_regions)
    only_new = [r for r in merged if r.source == "openai_gapfill"]
    log.info("OpenAI find_missed_regions: %d candidates → %d new after dedup",
             len(new_regions), len(only_new))
    return only_new
