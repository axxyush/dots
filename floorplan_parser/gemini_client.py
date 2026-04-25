from __future__ import annotations
import asyncio
import json
import logging
import re
from typing import Optional

from google import genai
from google.genai import types

log = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _extract_json(text: str) -> Optional[dict]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    m = _JSON_FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    return None


def _sync_call(client: genai.Client, model_name: str, prompt: str, image_bytes: bytes) -> str:
    import PIL.Image, io
    img = PIL.Image.open(io.BytesIO(image_bytes))
    response = client.models.generate_content(
        model=model_name,
        contents=[prompt, img],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
            max_output_tokens=65536,
        ),
    )
    return response.text


async def _call_once(client: genai.Client, model_name: str, prompt: str, image_bytes: bytes) -> dict:
    loop = asyncio.get_event_loop()
    raw = await loop.run_in_executor(None, _sync_call, client, model_name, prompt, image_bytes)
    parsed = _extract_json(raw)
    if parsed is None:
        raise ValueError(f"Could not extract JSON (first 200 chars): {raw[:200]}")
    return parsed


async def call_gemini(
    client: genai.Client,
    model_name: str,
    prompt: str,
    image_bytes: bytes,
    tile_id: str,
) -> dict:
    for attempt in range(2):
        try:
            return await _call_once(client, model_name, prompt, image_bytes)
        except Exception as exc:
            if attempt == 0:
                log.warning("Tile %s: attempt 1 failed (%s), retrying…", tile_id, exc)
                await asyncio.sleep(1)
            else:
                log.error("Tile %s: both attempts failed (%s). Returning empty skeleton.", tile_id, exc)
                return {
                    "tile_id": tile_id,
                    "objects": [],
                    "corridors": [],
                    "scale_detected": {"px_per_meter": None, "scale_bar_found": False},
                    "_parse_error": str(exc),
                }
    return {}


def make_client(api_key: str) -> genai.Client:
    return genai.Client(api_key=api_key)
