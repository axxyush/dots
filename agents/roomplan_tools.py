"""Tools for the RoomPlan→Braille agent.

Accepts Apple RoomPlan JSON (LiDAR output) and converts it into this repo's
normalized floorplan schema, then generates tactile Braille artifacts.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

# Make floorplan_parser importable (flat package, bare imports).
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "floorplan_parser"))

try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(REPO_ROOT / ".env", override=False)
except ImportError:
    pass

from braille_map import generate_braille_map  # noqa: E402
from roomplan_to_floorplan import roomplan_json_to_floorplan  # noqa: E402

log = logging.getLogger("roomplan.tools")

ARTIFACT_DIR = Path(tempfile.gettempdir()) / "roomplan_braille_artifacts"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)


def download_json(url: str) -> dict:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return {"error": f"Unsupported URL scheme: {parsed.scheme!r}. Use http/https."}
    fd, path_str = tempfile.mkstemp(prefix="roomplan_in_", suffix=".json", dir=ARTIFACT_DIR)
    os.close(fd)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "RoomPlanBraille/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp, open(path_str, "wb") as f:
            f.write(resp.read())
    except Exception as exc:
        return {"error": f"Download failed: {exc}"}
    size = os.path.getsize(path_str)
    log.info("downloaded %s → %s (%d bytes)", url, path_str, size)
    return {"json_path": path_str, "bytes": size}


def convert_roomplan_json(roomplan_json: str, source_name: str = "roomplan.json") -> dict:
    """
    Convert RoomPlan JSON (string) → normalized floorplan result.json written to disk.
    """
    try:
        roomplan = json.loads(roomplan_json)
    except Exception as exc:
        return {"error": f"Invalid JSON: {exc}"}

    try:
        converted = roomplan_json_to_floorplan(roomplan, source_name=source_name, use_llm=True)
    except Exception as exc:
        return {"error": f"Conversion failed: {exc}"}

    out_path = ARTIFACT_DIR / (Path(source_name).stem + "_floorplan_result.json")
    out_path.write_text(json.dumps(converted, indent=2), encoding="utf-8")
    fp = (converted or {}).get("floor_plan") or {}
    rooms = fp.get("rooms") or []
    doors = fp.get("doors") or []
    return {
        "json_path": str(out_path),
        "room_count": len(rooms),
        "door_count": len(doors),
        "source_name": source_name,
    }


def braille_map_from_roomplan_json(roomplan_json: str, source_name: str = "roomplan.json", cols: int = 90) -> dict:
    conv = convert_roomplan_json(roomplan_json, source_name=source_name)
    if "error" in conv:
        return conv
    json_path = conv["json_path"]
    stem = Path(json_path).stem.replace("_floorplan_result", "")
    out_txt = ARTIFACT_DIR / f"{stem}_braille_map.txt"
    out_leg = ARTIFACT_DIR / f"{stem}_braille_map.legend.txt"
    out_png = ARTIFACT_DIR / f"{stem}_braille_map.png"
    res = generate_braille_map(
        Path(json_path),
        out_txt=out_txt,
        out_legend=out_leg,
        out_png=out_png,
        cols=int(cols),
        llm_refine=True,
    )
    res["floorplan_json_path"] = json_path
    res["source_name"] = source_name
    return res


def upload_artifact(file_path: str) -> dict:
    p = Path(file_path)
    if not p.exists():
        return {"error": f"file not found: {file_path}"}
    try:
        import requests  # type: ignore
    except ImportError:
        return {"error": "The `requests` package is required. `pip install requests`."}
    try:
        with open(p, "rb") as f:
            resp = requests.post(
                "https://tmpfiles.org/api/v1/upload",
                files={"file": (p.name, f, "application/octet-stream")},
                headers={"User-Agent": "RoomPlanBraille/1.0"},
                timeout=60,
            )
    except Exception as exc:
        return {"error": f"upload failed: {exc}"}
    if resp.status_code != 200:
        return {"error": f"upload failed: HTTP {resp.status_code} — {resp.text[:200]}"}
    try:
        payload = resp.json()
    except Exception:
        return {"error": f"upload response was not JSON: {resp.text[:200]}"}
    preview_url = (payload.get("data") or {}).get("url", "")
    if not preview_url:
        return {"error": f"upload response missing url: {payload}"}
    direct_url = preview_url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)
    return {"url": direct_url, "preview_url": preview_url, "filename": p.name}


TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "download_json",
            "description": "Download a public RoomPlan JSON URL to a local file.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "braille_map_from_roomplan_json",
            "description": (
                "Convert Apple RoomPlan JSON (string) into a normalized floorplan and "
                "generate Braille map artifacts (txt + legend + png)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "roomplan_json": {"type": "string", "description": "The raw RoomPlan JSON string."},
                    "source_name": {"type": "string", "description": "Filename label for the source JSON.", "default": "roomplan.json"},
                    "cols": {"type": "integer", "default": 90},
                },
                "required": ["roomplan_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upload_artifact",
            "description": "Upload a local file and return a shareable URL.",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
    },
]


_DISPATCH = {
    "download_json": download_json,
    "braille_map_from_roomplan_json": braille_map_from_roomplan_json,
    "upload_artifact": upload_artifact,
}


def dispatch(name: str, arguments: dict) -> dict:
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return fn(**arguments)
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    except Exception as exc:
        log.exception("tool %s failed", name)
        return {"error": f"{name} raised: {exc}"}

