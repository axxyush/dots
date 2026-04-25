from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger("roomplan_to_floorplan")


def _extract_json_lenient(text: str) -> Optional[dict]:
    """
    Best-effort JSON extractor for model output.
    Accepts raw JSON or fenced ```json blocks.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    if "```" in text:
        start = text.find("```")
        end = text.rfind("```")
        if start != -1 and end != -1 and end > start:
            inner = text[start + 3 : end].strip()
            if inner.lower().startswith("json"):
                inner = inner[4:].strip()
            try:
                return json.loads(inner)
            except Exception:
                pass
    s = text.find("{")
    e = text.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(text[s : e + 1])
        except Exception:
            return None
    return None


_ALLOWED_TYPES = [
    "store", "restaurant", "restroom", "elevator", "stairs", "door",
    "corridor", "fire_exit", "fire_extinguisher", "fire_alarm", "rest_area",
    "office", "cafe", "service_counter", "label", "entrance",
    "bedroom", "bathroom", "living_room", "kitchen", "dining_room", "hallway",
    "classroom", "laboratory", "library", "auditorium", "gym",
    "music_room", "art_room", "staff_room", "reading_room",
    "computer_lab", "courtyard", "multimedia_room", "general_office",
    "utility", "lobby", "reception", "unknown",
]


def roomplan_json_to_floorplan(
    roomplan: dict[str, Any],
    *,
    source_name: str = "roomplan.json",
    use_llm: bool = True,
) -> dict[str, Any]:
    """
    Convert Apple RoomPlan (LiDAR) JSON output into this repo's normalized floorplan schema.

    RoomPlan exports vary by app/version and often encode geometry in 3D world coordinates.
    For now, we use an LLM-assisted conversion to produce a conservative 2D top-down layout:
      - output coordinate system is normalized 0..100
      - geometry must be axis-aligned bounding boxes
      - only include salient rooms + doors + stairs/elevator if clearly present
    """
    if not use_llm:
        raise ValueError("Non-LLM RoomPlan conversion is not implemented yet.")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is required to convert RoomPlan JSON → floorplan JSON.")

    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except Exception as exc:
        raise ValueError(f"Gemini client not available: {exc}") from exc

    # Keep prompt small-ish but include enough structure.
    raw = json.dumps(roomplan)
    if len(raw) > 120_000:
        # RoomPlan payloads can be huge; truncate safely.
        raw = raw[:120_000] + "…"

    prompt = (
        "You convert Apple RoomPlan JSON into a simple 2D accessibility floorplan.\n"
        "Return STRICT JSON only with this shape:\n"
        "{\n"
        "  \"floor_plan\": {\n"
        "    \"id\": \"floor_1\",\n"
        "    \"source_image\": \"<string>\",\n"
        "    \"dimensions_px\": {\"width\": 1000, \"height\": 1000},\n"
        "    \"coordinate_system\": \"normalized_0_to_100\",\n"
        "    \"parse_metadata\": {\"tile_grid\":\"roomplan\",\"overlap_pct\":0.0,\"tiles_parsed\":1,\"total_objects_before_dedup\":N,\"total_objects_after_dedup\":N},\n"
        "    \"rooms\": [FloorObject...],\n"
        "    \"corridors\": [],\n"
        "    \"doors\": [FloorObject...],\n"
        "    \"verticals\": [FloorObject...],\n"
        "    \"emergency\": [],\n"
        "    \"labels\": [],\n"
        "    \"low_confidence_flags\": [],\n"
        "    \"navigation_graph\": {\"nodes\": [], \"edges\": []}\n"
        "  }\n"
        "}\n\n"
        "FloorObject rules:\n"
        "- id: stable string\n"
        f"- type: one of {json.dumps(_ALLOWED_TYPES)}\n"
        "- label: optional short label\n"
        "- position: {x,y,w,h} where x,y,w,h are floats in [0,100]\n"
        "- partial: false unless clearly incomplete\n"
        "- confidence: \"high\"|\"medium\"|\"low\" (use medium if unsure)\n"
        "- door_type and door_swing: null unless explicitly known\n"
        "- accessible: true if likely accessible else null\n"
        "- notes: optional, keep short\n\n"
        "Mapping guidance:\n"
        "- Produce a TOP-DOWN 2D layout. Use the RoomPlan geometry to place rooms relative to each other.\n"
        "- If geometry is rotated/non-axis-aligned, output axis-aligned bounding boxes.\n"
        "- Scale/translate so the overall building fits within 0..100 with small margins.\n"
        "- Do NOT invent rooms. If you can't infer a space type, use type \"unknown\".\n"
        "- Doors should be small rectangles centered on openings between rooms.\n\n"
        f"source_image: {source_name}\n"
        f"RoomPlan JSON:\n{raw}\n"
    )

    client = genai.Client(api_key=api_key)
    model_name = os.environ.get("GEMINI_ROOMPLAN_MODEL", "gemini-2.5-pro")
    resp = client.models.generate_content(
        model=model_name,
        contents=[prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.0,
            max_output_tokens=16384,
        ),
    )
    parsed = _extract_json_lenient(resp.text or "")
    if not parsed:
        raise ValueError("Could not parse model output as JSON.")
    return parsed

