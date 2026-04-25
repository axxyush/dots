"""
Gemini helpers that refine CV-extracted room regions.

Two passes:

1) label_regions(image, regions)
   Crop each region from the original image, send to Gemini, ask for a single-
   word room type. Types are constrained to the project's FloorObject schema.

2) find_missed_regions(image, existing_regions)
   Render the original image with orange boxes over existing regions, ask
   Gemini to report any clearly-visible enclosed rooms / stores / pathways
   that are NOT inside any orange box. Gemini returns {x,y,w,h} in percent.

Both functions tolerate Gemini unavailability — they degrade silently rather
than crashing the pipeline.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
from typing import Any

import numpy as np

from cv_rooms import RoomRegion, _bbox_pct, _dedup_regions

log = logging.getLogger("cv_gemini_refine")


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


def _encode_png(image_bgr: np.ndarray) -> bytes:
    import cv2

    ok, buf = cv2.imencode(".png", image_bgr)
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    return buf.tobytes()


def _extract_json(text: str) -> Any:
    """Robust-ish JSON extraction (tolerates fences + surrounding prose)."""
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


def _sync_generate(client, model_name: str, prompt: str, png_bytes: bytes) -> str:
    from google.genai import types
    import PIL.Image

    img = PIL.Image.open(io.BytesIO(png_bytes))
    resp = client.models.generate_content(
        model=model_name,
        contents=[prompt, img],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.0,
            max_output_tokens=4096,
        ),
    )
    return resp.text or ""


# ── Pass 1a: single global labeling call (preferred) ────────────────────────

_GLOBAL_LABEL_CAPTION_COLOR = (0, 0, 255)       # red numbers for high contrast
_GLOBAL_LABEL_BOX_COLOR     = (255, 165, 0)     # orange boxes


def _render_numbered_overlay(
    image_bgr: np.ndarray,
    regions: list[RoomRegion],
    *,
    min_long_side: int = 1600,
) -> np.ndarray:
    """Draw numbered orange boxes on a copy of the floor plan. Upscales small
    images so the numbers stay legible for the model."""
    import cv2

    h, w = image_bgr.shape[:2]
    long_side = max(h, w)
    scale = max(1.0, min_long_side / float(long_side))
    vis = image_bgr.copy()
    if scale > 1.0:
        vis = cv2.resize(
            vis, (int(round(w * scale)), int(round(h * scale))),
            interpolation=cv2.INTER_CUBIC,
        )

    for r in regions:
        x = int(round(r.bbox_px["x"] * scale))
        y = int(round(r.bbox_px["y"] * scale))
        bw = int(round(r.bbox_px["w"] * scale))
        bh = int(round(r.bbox_px["h"] * scale))
        cv2.rectangle(vis, (x, y), (x + bw, y + bh), _GLOBAL_LABEL_BOX_COLOR, 2)
        # Big red number for the LLM to refer to. Placed slightly inside the
        # top-left corner so it doesn't collide with the floor-plan border.
        text = r.id
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
        tx = x + 4
        ty = y + th + 6
        # white halo behind for legibility on busy plans
        cv2.putText(vis, text, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (255, 255, 255), 5, cv2.LINE_AA)
        cv2.putText(vis, text, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    _GLOBAL_LABEL_CAPTION_COLOR, 2, cv2.LINE_AA)
    return vis


def label_regions_global(
    image_bgr: np.ndarray,
    regions: list[RoomRegion],
    *,
    client,
    model_name: str = "gemini-2.5-pro",
) -> list[RoomRegion]:
    """
    One Gemini call for the whole plan: send the full image with numbered
    orange boxes, ask Gemini to return {id → {label, type}} for every box by
    reading the printed text inside. This is much more accurate than per-crop
    labeling because the model sees:
       • building type (school vs hotel vs hospital) — disambiguates types
       • printed room labels — OCR-style reading, not visual guessing
       • relative positions — understands which rooms are likely classrooms
         vs offices based on layout.

    Returns a new region list. Regions Gemini couldn't label are left
    unchanged (so the per-crop pass can still take a shot).
    """
    if not regions:
        return regions

    overlay = _render_numbered_overlay(image_bgr, regions)
    png = _encode_png(overlay)

    allowed_str = ", ".join(_ALLOWED_TYPES)
    ids = [r.id for r in regions]
    prompt = (
        "You are annotating a floor plan. Orange rectangles mark rooms/regions "
        "that have been detected. Each rectangle has a red ID label at its "
        "top-left (e.g. `cv_room_0`).\n\n"
        "For EVERY ID in this list, return two things:\n"
        "  1. \"label\" — the exact text printed INSIDE that box on the floor "
        "plan (e.g. \"Laboratory\", \"Multi-Media Room\", \"Staff Room\", "
        "\"General Office\"). This must be verbatim from the plan — preserve "
        "capitalization and spelling. If no text is visible inside the box, "
        "infer from the icons/symbols (e.g. bed → \"Bedroom\", toilet → "
        "\"Restroom\", stairs symbol → \"Stairs\"). Return null only if the "
        "box is clearly not a room (blank background, exterior, duplicate).\n"
        f"  2. \"type\" — one canonical category from: {allowed_str}. Pick "
        "the single closest match to the label/contents. Use \"unknown\" only "
        "as a last resort.\n\n"
        "IDs to annotate: " + ", ".join(ids) + ".\n\n"
        "Return JSON ONLY — no prose, no markdown fences. Schema:\n"
        "{\n"
        "  \"rooms\": [\n"
        "    {\"id\": \"cv_room_0\", \"label\": \"Laboratory\", \"type\": \"laboratory\"},\n"
        "    {\"id\": \"cv_room_1\", \"label\": \"Hallway\",    \"type\": \"hallway\"},\n"
        "    ...\n"
        "  ]\n"
        "}\n"
        "Include every ID in the list. If you cannot read a specific box, set "
        "its label to null and type to \"unknown\" — do NOT omit it."
    )

    try:
        raw = _sync_generate(client, model_name, prompt, png)
    except Exception as exc:
        log.warning("label_regions_global: Gemini call failed: %s", exc)
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
    log.info("label_regions_global: %d/%d regions labeled", labeled_count, len(regions))
    return out


# ── Pass 1b: per-crop labeling fallback ─────────────────────────────────────


def _crop_region(image_bgr: np.ndarray, region: RoomRegion, pad: int = 6) -> np.ndarray:
    h, w = image_bgr.shape[:2]
    x = max(0, region.bbox_px["x"] - pad)
    y = max(0, region.bbox_px["y"] - pad)
    x1 = min(w, region.bbox_px["x"] + region.bbox_px["w"] + pad)
    y1 = min(h, region.bbox_px["y"] + region.bbox_px["h"] + pad)
    return image_bgr[y:y1, x:x1].copy()


def label_regions(
    image_bgr: np.ndarray,
    regions: list[RoomRegion],
    *,
    client,
    model_name: str = "gemini-2.5-flash",
    only_missing: bool = False,
) -> list[RoomRegion]:
    """
    Per-crop classifier. Sends a wider context crop and asks Gemini to READ
    any printed text label inside the room before returning a type.

    If `only_missing` is True, skip regions that already have a label (used
    as a fallback after the global pass).
    """
    if not regions:
        return regions

    h_img, w_img = image_bgr.shape[:2]
    allowed_str = ", ".join(_ALLOWED_TYPES)
    prompt = (
        "You are labeling one room in a floor plan. A wider context crop is "
        "included so you can see the surrounding labels.\n\n"
        "Return JSON only (no fences, no prose): "
        "{\"label\": \"<text printed inside the room, verbatim, or null>\", "
        "\"type\": \"<one of: " + allowed_str + ">\"}\n\n"
        "Instructions:\n"
        "  • READ any text printed INSIDE the room's outline. Return it "
        "verbatim as \"label\" (preserve capitalization). If no text is "
        "visible, set label to null and infer type from icons/symbols.\n"
        "  • For \"type\", pick the single closest canonical category. Use "
        "\"unknown\" ONLY as a last resort — never guess \"restaurant\" or "
        "\"store\" for an unidentified room in a school/institutional plan."
    )

    labeled: list[RoomRegion] = []
    for r in regions:
        if only_missing and (r.label_hint or r.label_text):
            labeled.append(r)
            continue
        try:
            # Wider crop (≈40 % extra on each side) so any text label printed
            # just outside the tight bbox is also visible.
            pad_x = max(12, int(r.bbox_px["w"] * 0.4))
            pad_y = max(12, int(r.bbox_px["h"] * 0.4))
            x0 = max(0, r.bbox_px["x"] - pad_x)
            y0 = max(0, r.bbox_px["y"] - pad_y)
            x1 = min(w_img, r.bbox_px["x"] + r.bbox_px["w"] + pad_x)
            y1 = min(h_img, r.bbox_px["y"] + r.bbox_px["h"] + pad_y)
            crop = image_bgr[y0:y1, x0:x1].copy()
            png = _encode_png(crop)
            raw = _sync_generate(client, model_name, prompt, png)
            parsed = _extract_json(raw)
            if isinstance(parsed, dict):
                t_raw = str(parsed.get("type", "") or "").strip().lower().replace(" ", "_")
                t = t_raw if t_raw in _ALLOWED_TYPES else None
                lbl_raw = parsed.get("label")
                lbl = str(lbl_raw).strip() if isinstance(lbl_raw, str) else None
                if lbl == "" or (isinstance(lbl, str) and lbl.lower() in ("null", "none")):
                    lbl = None
                if t or lbl:
                    labeled.append(
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
                    continue
            log.debug("label_regions: %s unlabeled (raw=%r)", r.id, raw[:120])
            labeled.append(r)
        except Exception as exc:
            log.warning("label_regions: %s failed: %s", r.id, exc)
            labeled.append(r)

    return labeled


# ── Pass 2: find missed regions ─────────────────────────────────────────────


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
    client,
    model_name: str = "gemini-2.5-flash",
    max_new: int = 50,
    min_overlay_long_side: int = 1400,
) -> list[RoomRegion]:
    """
    Ask Gemini for rooms/pathways the CV pass missed.

    The image sent is an overlay with detected rooms highlighted in orange;
    Gemini is asked to report any clearly-visible enclosed rooms or major
    pathways that are NOT already marked.  Small floor-plan images are
    upscaled before being sent so Gemini can actually see the overlay.
    """
    import cv2

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
        "You are helping complete a floor-plan parse.\n"
        "Orange rectangles mark rooms/regions already detected.\n"
        "List rooms, stores, restrooms, offices, elevators, stairs, cafes, "
        "restaurants, hallways, or corridors that are CLEARLY visible but "
        "are NOT already inside an orange rectangle.\n\n"
        "Return JSON array (no prose, no fences) of:\n"
        '  {"type": "<one of: store, restaurant, restroom, elevator, stairs, '
        'corridor, rest_area, office, cafe, service_counter, entrance, '
        'bedroom, bathroom, living_room, kitchen, dining_room, hallway, '
        'unknown>", '
        '"x": <0-100>, "y": <0-100>, "w": <0-100>, "h": <0-100>}\n\n'
        "Coordinates are percentages of image width/height from the top-left. "
        f"Report at most {max_new} items. Be conservative: only include "
        "regions you are confident are enclosed rooms/pathways. "
        "Do NOT re-report regions that are already inside an orange box."
    )

    try:
        raw = _sync_generate(client, model_name, prompt, png)
    except Exception as exc:
        log.warning("find_missed_regions: Gemini call failed: %s", exc)
        return []

    parsed = _extract_json(raw)
    if not isinstance(parsed, list):
        log.info("find_missed_regions: no JSON array (raw=%r)", (raw or "")[:160])
        return []

    new_regions: list[RoomRegion] = []
    for i, item in enumerate(parsed[:max_new]):
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
                id=f"gemini_room_{i}",
                bbox_px=bbox_px,
                bbox_pct=_bbox_pct(bbox_px, w, h),
                area_px=px_w * px_h,
                source="gemini_gapfill",
                label_hint=t if t != "unknown" else None,
            )
        )

    # De-dup against existing so Gemini can't re-report a CV room
    merged = _dedup_regions(existing + new_regions)
    only_new = [r for r in merged if r.source == "gemini_gapfill"]
    log.info("find_missed_regions: Gemini returned %d candidates, %d new after dedup",
             len(new_regions), len(only_new))
    return only_new
