"""Tools the SenseGrid agent can call.

Each tool is a plain Python function + a JSON-schema spec for the LLM. The
chat agent decides when to invoke them; `dispatch` executes by name.
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

# Load .env so GEMINI_API_KEY is available even when this module is imported
# outside the agent (e.g. by tests). override=False → shell env wins.
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(REPO_ROOT / ".env", override=False)
except ImportError:
    pass

import cv2  # noqa: E402

from cv_rooms import (  # noqa: E402
    extract_rooms_cv_multiscale,
    regions_to_floor_objects,
)
from render_map import render_floor_plan  # noqa: E402

try:
    from cv_gemini_refine import label_regions_global  # noqa: E402
    _GEMINI_IMPORT_OK = True
except Exception as _gemini_import_exc:  # pragma: no cover
    label_regions_global = None  # type: ignore
    _GEMINI_IMPORT_OK = False
    logging.getLogger("sensegrid.tools").warning(
        "Gemini labeling unavailable: %s", _gemini_import_exc
    )

log = logging.getLogger("sensegrid.tools")

ARTIFACT_DIR = Path(tempfile.gettempdir()) / "sensegrid_artifacts"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)


# ── Tool 1: download_image ───────────────────────────────────────────────────


def download_image(url: str) -> dict:
    """Download a public image URL to a local temp file."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return {"error": f"Unsupported URL scheme: {parsed.scheme!r}. Use http/https."}

    suffix = Path(parsed.path).suffix or ".png"
    fd, path_str = tempfile.mkstemp(prefix="sensegrid_in_", suffix=suffix, dir=ARTIFACT_DIR)
    os.close(fd)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SenseGrid/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp, open(path_str, "wb") as f:
            f.write(resp.read())
    except Exception as exc:
        return {"error": f"Download failed: {exc}"}

    size = os.path.getsize(path_str)
    log.info("downloaded %s → %s (%d bytes)", url, path_str, size)
    return {"image_path": path_str, "bytes": size}


# ── Tool 2: parse_floorplan ──────────────────────────────────────────────────


def parse_floorplan(image_path: str) -> dict:
    """Run the CV floor-plan pipeline on a local image. Writes a result.json
    and returns its path + a small summary the LLM can reason over.

    When GEMINI_API_KEY is available, also runs the one-shot Gemini labeling
    pass so rooms get real types ("laboratory", "classroom", …) instead of
    "unknown".
    """
    p = Path(image_path)
    if not p.exists():
        return {"error": f"image not found: {image_path}"}

    img = cv2.imread(str(p), cv2.IMREAD_COLOR)
    if img is None:
        return {"error": f"could not read image: {image_path}"}
    h, w = img.shape[:2]

    regions, _ = extract_rooms_cv_multiscale(img)

    gemini_status = "skipped"
    gemini_error: str | None = None
    gemini_labeled = 0
    gemini_key = os.environ.get("GEMINI_API_KEY")

    if not gemini_key:
        log.warning("parse_floorplan: GEMINI_API_KEY not set — rooms will be 'unknown'")
        gemini_error = "GEMINI_API_KEY not set"
    elif not _GEMINI_IMPORT_OK:
        log.warning("parse_floorplan: cv_gemini_refine import failed — skipping labeling")
        gemini_error = "cv_gemini_refine import failed"
    elif not regions:
        gemini_error = "no regions to label"
    else:
        try:
            from google import genai  # type: ignore

            client = genai.Client(api_key=gemini_key)
            model_name = os.environ.get("GEMINI_LABEL_MODEL", "gemini-2.5-pro")
            log.info("parse_floorplan: calling Gemini global labeling via %s…", model_name)
            regions = label_regions_global(img, regions, client=client, model_name=model_name)
            gemini_labeled = sum(
                1 for r in regions if (r.label_text or (r.label_hint and r.label_hint != "unknown"))
            )
            gemini_status = "ok" if gemini_labeled > 0 else "returned_no_labels"
            log.info(
                "parse_floorplan: Gemini labeled %d/%d rooms (%s)",
                gemini_labeled, len(regions), gemini_status,
            )
        except Exception as exc:
            log.warning("parse_floorplan: Gemini labeling raised %s", exc)
            gemini_status = "error"
            gemini_error = str(exc)

    objs = regions_to_floor_objects(regions)

    floor_plan = {
        "id": "floor_1",
        "source_image": p.name,
        "dimensions_px": {"width": w, "height": h},
        "coordinate_system": "normalized_0_to_100",
        "parse_metadata": {
            "tile_grid": "cv",
            "overlap_pct": 0.0,
            "tiles_parsed": 1,
            "total_objects_before_dedup": len(objs),
            "total_objects_after_dedup": len(objs),
        },
        "rooms": objs,
        "corridors": [],
        "doors": [],
        "verticals": [],
        "emergency": [],
        "labels": [],
        "low_confidence_flags": [],
        "navigation_graph": {"nodes": [], "edges": []},
    }
    result = {"floor_plan": floor_plan}

    json_path = ARTIFACT_DIR / (p.stem + "_result.json")
    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    by_type: dict[str, int] = {}
    for o in objs:
        by_type[o["type"]] = by_type.get(o["type"], 0) + 1

    log.info(
        "parsed %s → %d rooms (%s); gemini=%s labeled=%d/%d",
        image_path, len(objs), by_type, gemini_status, gemini_labeled, len(regions),
    )
    result: dict = {
        "json_path": str(json_path),
        "room_count": len(objs),
        "rooms_by_type": by_type,
        "dimensions_px": {"width": w, "height": h},
        "gemini_labeling_status": gemini_status,
        "gemini_rooms_labeled": gemini_labeled,
    }
    if gemini_error:
        result["gemini_labeling_note"] = gemini_error
    return result


# ── Tool 3: reconstruct_floorplan ────────────────────────────────────────────


def reconstruct_floorplan(json_path: str, blurred: bool = False) -> dict:
    """Render the parsed JSON back to a PNG so the user can verify accuracy.

    When `blurred=True`, applies a heavy Gaussian blur — used as a teaser
    preview before payment is settled.
    """
    p = Path(json_path)
    if not p.exists():
        return {"error": f"json not found: {json_path}"}

    data = json.loads(p.read_text(encoding="utf-8"))
    fp = data.get("floor_plan") or data

    img = render_floor_plan(fp, show_nav_graph=False, show_grid=True, show_low_conf=True)
    if blurred:
        from PIL import ImageFilter  # type: ignore

        img = img.filter(ImageFilter.GaussianBlur(radius=18))

    suffix = "_rendered_preview.png" if blurred else "_rendered.png"
    out_path = ARTIFACT_DIR / (p.stem.replace("_result", "") + suffix)
    img.save(out_path, format="PNG")
    log.info("reconstructed %s → %s (blurred=%s)", json_path, out_path, blurred)
    return {
        "png_path": str(out_path),
        "width": img.width,
        "height": img.height,
        "blurred": blurred,
    }


# ── Tool 4: upload_artifact ──────────────────────────────────────────────────


def upload_artifact(file_path: str) -> dict:
    """Upload a local file to a public no-auth host and return a direct URL.

    Uses tmpfiles.org (no API key, ~60 min retention). The API's returned URL
    points to an HTML preview; inserting `/dl/` produces the direct-download
    URL that ASI:One / curl / browsers render as the file itself.
    """
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
                headers={"User-Agent": "SenseGrid/1.0"},
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

    # Convert "http://tmpfiles.org/<id>/<name>" → "http://tmpfiles.org/dl/<id>/<name>".
    direct_url = preview_url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)
    log.info("uploaded %s → %s", file_path, direct_url)
    return {"url": direct_url, "preview_url": preview_url, "filename": p.name}


# ── Tool specs (OpenAI / ASI:One function-calling schema) ────────────────────


TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "download_image",
            "description": (
                "Download a public floor-plan image from a URL to a local file. "
                "Always run this before parse_floorplan when the user provides a URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Public http(s) URL of the image."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_floorplan",
            "description": (
                "Run the CV floor-plan pipeline on a local image file. Returns the "
                "path to a JSON file describing the rooms plus a summary by type."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "Absolute path to a local image file."},
                },
                "required": ["image_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reconstruct_floorplan",
            "description": (
                "Render a parsed floor-plan JSON back to a PNG so the user can "
                "visually verify the extraction was correct."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "json_path": {"type": "string", "description": "Absolute path to a result.json produced by parse_floorplan."},
                },
                "required": ["json_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upload_artifact",
            "description": (
                "Upload a local file (PNG/JSON) to a public no-auth host and return "
                "a shareable URL. Use this to give the user links to the "
                "reconstructed PNG and the structured JSON."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path to the file to share."},
                },
                "required": ["file_path"],
            },
        },
    },
]


_DISPATCH = {
    "download_image": download_image,
    "parse_floorplan": parse_floorplan,
    "reconstruct_floorplan": reconstruct_floorplan,
    "upload_artifact": upload_artifact,
}


def dispatch(name: str, arguments: dict) -> dict:
    """Execute a tool by name with a dict of arguments. Returns a JSON-safe dict."""
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
