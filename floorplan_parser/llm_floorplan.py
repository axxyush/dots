"""
End-to-end floor-plan parsing via a single OpenAI vision call.

Instead of running OpenCV region extraction first and then asking an LLM to
label each box, we let GPT-4o inspect the whole plan and return the complete
structured FloorPlan in one shot. This tends to be MORE accurate for
well-drawn architectural plans because:

  * The model sees global context (building type, label text, legends).
  * It can reason about room boundaries without being constrained to noisy
    CV bounding boxes that sometimes merge two rooms or miss thin partitions.
  * It reads printed labels verbatim instead of guessing.

Trade-off: spatial precision on bbox coordinates is only as good as the
model's vision. We clamp outputs to the schema (0-100 percent) and run
light validation to catch obviously-bad numbers.

Returns the FloorPlan dict that's compatible with `render_floor_plan` and
`generate_ada_recommendations`.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
from typing import Any

log = logging.getLogger("llm_floorplan")


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

_ALLOWED_TYPE_SET = frozenset(_ALLOWED_TYPES)


def has_openai_key() -> bool:
    return bool((os.environ.get("OPENAI_API_KEY") or "").strip())


def _extract_json(text: str) -> Any:
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


def _image_to_data_url(image_path: str, *, max_long_side: int = 1792) -> tuple[str, int, int]:
    """Load an image, downscale if huge, return (data_url, width, height).

    GPT-4o accepts up to ~2048px on a side; we downscale above 1792 to stay
    safely in the high-detail regime without blowing up token cost.
    """
    import cv2

    img = cv2.imread(image_path, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"could not read image: {image_path}")
    h, w = img.shape[:2]
    long_side = max(h, w)
    if long_side > max_long_side:
        scale = max_long_side / float(long_side)
        img = cv2.resize(
            img,
            (int(round(w * scale)), int(round(h * scale))),
            interpolation=cv2.INTER_AREA,
        )
        h, w = img.shape[:2]
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    b64 = base64.b64encode(buf.tobytes()).decode("ascii")
    return f"data:image/png;base64,{b64}", w, h


def _build_prompt() -> str:
    allowed = ", ".join(_ALLOWED_TYPES)
    return (
        "You are a senior architect reading a single floor plan / venue map "
        "image. Output the complete structured JSON for every labeled "
        "room / space / seating section / vertical circulation element / "
        "entrance visible.\n\n"
        "WORKS FOR: building floor plans (schools, offices, hospitals, "
        "houses, malls) AND venue / stadium / arena seating diagrams "
        "(Pauley Pavilion, theaters, concert halls).\n\n"
        "GUIDELINES\n"
        "  * Read printed text VERBATIM from the plan and return it in "
        "`label` (preserve capitalization, numbers, suffixes). Examples of "
        "labels you MUST read: '101', '126A', 'STUDENTS', 'FACULTY / "
        "STAFF', 'COURTSIDE', 'VISITOR', 'MEDIA', 'Classroom 201'.\n"
        "  * For numbered seating sections in arenas/stadiums, `label` is "
        "the section number EXACTLY as printed (e.g. '223A', '126A', "
        "'101'). Use `type` = 'rest_area' for seating sections, 'corridor' "
        "for walkways/concourses, 'entrance' for labeled entry points, "
        "'office' for administrative areas, 'service_counter' for MEDIA "
        "or similar.\n"
        "  * For `type`, pick the single best canonical category from: "
        f"{allowed}. NEVER return 'unknown' unless you genuinely cannot "
        "tell what the region is. If you can read a printed label, you can "
        "almost always pick a type.\n"
        "  * Coordinates are PERCENTAGES of the full image (0-100) from "
        "the top-left corner. `x`/`y` are the top-left corner of the "
        "bounding box; `w`/`h` are width/height. Boxes must tightly "
        "enclose the region's outline.\n"
        "  * BE EXHAUSTIVE. Cover every distinct colored/labeled region "
        "you can see. For an arena with ~50 seating sections, return ~50 "
        "room entries.\n"
        "  * Include hallways, corridors, concourses, stairs, elevators, "
        "restrooms, lobbies, entrances — not just named rooms.\n"
        "  * `confidence`: 'high' if you clearly read the label; 'medium' "
        "if inferred from icons/shape/color; 'low' if uncertain.\n"
        "  * DO NOT invent regions. DO NOT output overlapping duplicate "
        "boxes for the same region.\n"
        "  * Assign ids of the form 'room_0', 'room_1', ... in reading "
        "order (top-left to bottom-right).\n\n"
        "SCHEMA (return this JSON object only, no prose, no fences):\n"
        "{\n"
        "  \"building_type\": \"<e.g. school, arena, office, mall, house>\",\n"
        "  \"rooms\": [\n"
        "    {\n"
        "      \"id\": \"room_0\",\n"
        "      \"type\": \"rest_area\",\n"
        "      \"label\": \"223A\",\n"
        "      \"position\": {\"x\": 12.3, \"y\": 45.6, \"w\": 8.1, \"h\": 6.7},\n"
        "      \"confidence\": \"high\"\n"
        "    }\n"
        "  ],\n"
        "  \"corridors\": [\n"
        "    {\n"
        "      \"id\": \"corridor_0\",\n"
        "      \"type\": \"primary_corridor\",\n"
        "      \"centerline\": [\n"
        "        {\"x\": 5.0, \"y\": 50.0},\n"
        "        {\"x\": 95.0, \"y\": 50.0}\n"
        "      ]\n"
        "    }\n"
        "  ],\n"
        "  \"entrances\": [\n"
        "    {\n"
        "      \"id\": \"entrance_0\",\n"
        "      \"type\": \"entrance\",\n"
        "      \"label\": \"Main Entrance\",\n"
        "      \"position\": {\"x\": 48.0, \"y\": 96.0, \"w\": 4.0, \"h\": 2.0}\n"
        "    }\n"
        "  ],\n"
        "  \"verticals\": [\n"
        "    {\n"
        "      \"id\": \"vert_0\",\n"
        "      \"type\": \"stairs\",\n"
        "      \"label\": \"Stair A\",\n"
        "      \"position\": {\"x\": 30.0, \"y\": 20.0, \"w\": 4.0, \"h\": 8.0}\n"
        "    }\n"
        "  ]\n"
        "}\n"
    )


def _clamp_pct(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(100.0, f))


def _normalize_type(raw: Any) -> str:
    if not isinstance(raw, str):
        return "unknown"
    t = raw.strip().lower().replace(" ", "_").replace("-", "_")
    return t if t in _ALLOWED_TYPE_SET else "unknown"


def _build_floor_object(
    raw: dict,
    *,
    idx: int,
    default_type: str,
    id_prefix: str,
    default_source: str = "llm",
) -> dict | None:
    if not isinstance(raw, dict):
        return None
    pos = raw.get("position") or {}
    if not isinstance(pos, dict):
        return None
    x = _clamp_pct(pos.get("x"))
    y = _clamp_pct(pos.get("y"))
    w = _clamp_pct(pos.get("w"))
    h = _clamp_pct(pos.get("h"))
    if w <= 0.25 or h <= 0.25:
        return None
    if x + w > 100.0:
        w = max(0.0, 100.0 - x)
    if y + h > 100.0:
        h = max(0.0, 100.0 - y)

    otype = _normalize_type(raw.get("type") or default_type)
    label_raw = raw.get("label")
    label = str(label_raw).strip() if isinstance(label_raw, str) else None
    if label and label.lower() in {"null", "none", ""}:
        label = None
    display_label = label or otype.replace("_", " ").title()

    confidence = raw.get("confidence", "medium")
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium"

    obj_id = str(raw.get("id") or f"{id_prefix}_{idx}").strip() or f"{id_prefix}_{idx}"

    return {
        "id": obj_id,
        "type": otype,
        "label": display_label,
        "position": {
            "x": round(x, 2),
            "y": round(y, 2),
            "w": round(w, 2),
            "h": round(h, 2),
        },
        "partial": False,
        "confidence": confidence,
        "door_type": None,
        "door_swing": None,
        "accessible": True,
        "width_m": None,
        "notes": None,
        "seen_in_tiles": 1,
        "source": default_source,
    }


def _dedup_by_iou(objs: list[dict], iou_thresh: float = 0.55) -> list[dict]:
    def iou(a: dict, b: dict) -> float:
        ax, ay, aw, ah = a["x"], a["y"], a["w"], a["h"]
        bx, by, bw, bh = b["x"], b["y"], b["w"], b["h"]
        ix0, iy0 = max(ax, bx), max(ay, by)
        ix1, iy1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
        iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
        inter = iw * ih
        if inter <= 0:
            return 0.0
        union = aw * ah + bw * bh - inter
        return inter / union if union > 0 else 0.0

    kept: list[dict] = []
    for o in objs:
        dup = False
        for k in kept:
            if iou(o["position"], k["position"]) >= iou_thresh:
                dup = True
                break
        if not dup:
            kept.append(o)
    return kept


def _call_with_fallbacks(
    client, model: str, prompt: str, data_url: str,
) -> tuple[str, str | None]:
    """Invoke chat.completions with graceful degradation on per-model quirks.

    Known quirks we handle:
      * gpt-5.x rejects `max_tokens` → we always send `max_completion_tokens`.
      * gpt-5.5+ rejects `temperature != 1` → retry without temperature.
      * Some models reject `response_format={"type": "json_object"}` → retry
        without the response_format hint.
    """
    messages = [
        {
            "role": "system",
            "content": (
                "You are a careful floor-plan annotation assistant. Return "
                "JSON only. Read printed labels verbatim. Never invent rooms."
            ),
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": data_url, "detail": "high"},
                },
            ],
        },
    ]
    base: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": 16384,
        "response_format": {"type": "json_object"},
    }
    # Start with temperature=0 for determinism; drop it on models that reject.
    attempts: list[dict[str, Any]] = [
        {**base, "temperature": 0.0},
        base,
        {k: v for k, v in base.items() if k != "response_format"},
    ]
    last_exc: Exception | None = None
    for i, kwargs in enumerate(attempts):
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as exc:
            msg = str(exc)
            log.warning("LLM attempt %d/%d failed (%s)", i + 1, len(attempts), msg[:200])
            last_exc = exc
            # Only keep retrying on recoverable 400s.
            if "temperature" not in msg and "response_format" not in msg:
                raise
            continue
        raw = (resp.choices[0].message.content or "") if resp.choices else ""
        finish = resp.choices[0].finish_reason if resp.choices else None
        return raw, finish
    assert last_exc is not None
    raise last_exc


def parse_floorplan_with_llm(
    image_path: str,
    *,
    api_key: str | None = None,
    model_name: str | None = None,
    source_image_name: str | None = None,
    max_long_side: int = 1792,
) -> dict:
    """
    Run a single OpenAI vision call and return a FloorPlan dict (ready for
    render_floor_plan + generate_ada_recommendations).

    Raises on unrecoverable errors (no key, no response, unparseable JSON);
    callers are expected to catch and fall back to the CV pipeline.
    """
    key = (api_key or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")

    model = model_name or os.environ.get("OPENAI_PARSE_MODEL", "gpt-5.4")
    data_url, w, h = _image_to_data_url(image_path, max_long_side=max_long_side)

    # Local import so callers without the openai package can still import the
    # module (e.g. during static checks).
    from openai import OpenAI  # type: ignore

    client = OpenAI(api_key=key)
    prompt = _build_prompt()

    log.info("parse_floorplan_with_llm: calling %s (img %dx%d)", model, w, h)
    raw, finish = _call_with_fallbacks(client, model, prompt, data_url)
    if not raw:
        raise RuntimeError(
            f"LLM returned empty content (finish_reason={finish}). "
            "Consider raising max_completion_tokens or using a different model."
        )
    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"LLM returned non-object JSON (first 200ch): {raw[:200]!r}")

    building_type = parsed.get("building_type") or "unknown"

    rooms: list[dict] = []
    for i, r in enumerate(parsed.get("rooms") or []):
        obj = _build_floor_object(r, idx=i, default_type="unknown", id_prefix="room")
        if obj is not None:
            rooms.append(obj)

    verticals: list[dict] = []
    for i, v in enumerate(parsed.get("verticals") or []):
        obj = _build_floor_object(
            v, idx=i, default_type="stairs", id_prefix="vert", default_source="llm",
        )
        if obj is not None:
            verticals.append(obj)

    entrances: list[dict] = []
    for i, e in enumerate(parsed.get("entrances") or []):
        obj = _build_floor_object(
            e, idx=i, default_type="entrance", id_prefix="entrance", default_source="llm",
        )
        if obj is not None:
            entrances.append(obj)

    # Corridors: accept as list of {centerline: [{x,y}, ...]}.
    corridors: list[dict] = []
    for i, c in enumerate(parsed.get("corridors") or []):
        if not isinstance(c, dict):
            continue
        pts = c.get("centerline") or []
        if not isinstance(pts, list) or len(pts) < 2:
            continue
        cline = []
        for p in pts:
            if not isinstance(p, dict):
                continue
            cline.append({"x": _clamp_pct(p.get("x")), "y": _clamp_pct(p.get("y"))})
        if len(cline) < 2:
            continue
        ctype = c.get("type") if c.get("type") in ("primary_corridor", "secondary_corridor") else "primary_corridor"
        corridors.append(
            {
                "id": str(c.get("id") or f"corridor_{i}"),
                "type": ctype,
                "centerline": cline,
                "width_m": None,
                "accessible": True,
                "direction_arrows": [],
                "seen_in_tiles": 1,
                "source": "llm",
            }
        )

    # Remove near-duplicate rooms the model may have emitted twice.
    rooms = _dedup_by_iou(rooms, iou_thresh=0.55)

    floor_plan = {
        "id": "floor_1",
        "source_image": source_image_name or os.path.basename(image_path),
        "dimensions_px": {"width": int(w), "height": int(h)},
        "coordinate_system": "normalized_0_to_100",
        "parse_metadata": {
            "tile_grid": f"openai:{model}",
            "overlap_pct": 0.0,
            "tiles_parsed": 1,
            "total_objects_before_dedup": len(rooms) + len(verticals) + len(entrances),
            "total_objects_after_dedup": len(rooms) + len(verticals) + len(entrances),
        },
        "rooms": rooms,
        "corridors": corridors,
        "doors": [],
        "verticals": verticals,
        "emergency": [],
        "labels": [],
        "low_confidence_flags": [
            r for r in rooms if r.get("confidence") == "low"
        ],
        "navigation_graph": {"nodes": [], "edges": []},
    }

    log.info(
        "parse_floorplan_with_llm: model=%s rooms=%d corridors=%d verticals=%d entrances=%d building=%s",
        model, len(rooms), len(corridors), len(verticals), len(entrances), building_type,
    )
    return {"floor_plan": floor_plan, "building_type": building_type}
