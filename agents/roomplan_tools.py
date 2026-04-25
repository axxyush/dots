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

from roomplan_to_layout_2d import roomplan_json_to_layout_2d  # noqa: E402
from connectdots_pdf import generate_tactile_pdf  # noqa: E402

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


def tactile_pdf_from_roomplan_json(roomplan_json: str, source_name: str = "roomplan.json") -> dict:
    """Convert RoomPlan JSON (string) → tactile PDF (ConnectDots-style)."""
    try:
        roomplan = json.loads(roomplan_json)
    except Exception as exc:
        return {"error": f"Invalid JSON: {exc}"}

    try:
        layout, metadata = roomplan_json_to_layout_2d(roomplan)
    except Exception as exc:
        return {"error": f"Conversion failed: {exc}"}

    out_pdf = ARTIFACT_DIR / (Path(source_name).stem + "_tactile.pdf")
    # For tactile maps, avoid numeric legends; keep single page.
    pdf_path = generate_tactile_pdf(
        output_pdf_path=str(out_pdf),
        layout=layout,
        metadata=metadata,
        room_id=Path(source_name).stem,
        number_objects=False,
        include_legend_page=False,
    )

    return {
        "pdf_path": str(pdf_path),
        "layout_2d": layout,
        "metadata": metadata,
        "source_name": source_name,
        "object_count": len(layout.get("objects") or []),
    }


def tactile_map_from_roomplan_json(roomplan_json: str, source_name: str = "roomplan.json") -> dict:
    """Alias name for agent/tooling."""
    return tactile_pdf_from_roomplan_json(roomplan_json, source_name=source_name)


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
            "name": "tactile_map_from_roomplan_json",
            "description": (
                "Convert Apple RoomPlan JSON (string) into a tactile PDF map using "
                "a deterministic geometry conversion (no LLM)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "roomplan_json": {"type": "string", "description": "The raw RoomPlan JSON string."},
                    "source_name": {"type": "string", "description": "Filename label for the source JSON.", "default": "roomplan.json"},
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
    "tactile_map_from_roomplan_json": tactile_map_from_roomplan_json,
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

