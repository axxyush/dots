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

try:
    import cv2  # type: ignore  # noqa: E402
    _CV2_OK = True
except Exception:
    cv2 = None  # type: ignore
    _CV2_OK = False

extract_rooms_cv_multiscale = None  # type: ignore
regions_to_floor_objects = None  # type: ignore
from ada_advisor import generate_ada_recommendations  # noqa: E402
from render_map import render_floor_plan  # noqa: E402

try:
    from cv_gemini_refine import label_regions_global as _gemini_label_global  # noqa: E402
    _GEMINI_IMPORT_OK = True
except Exception as _gemini_import_exc:  # pragma: no cover
    _gemini_label_global = None  # type: ignore
    _GEMINI_IMPORT_OK = False
    logging.getLogger("sensegrid.tools").warning(
        "Gemini labeling unavailable: %s", _gemini_import_exc
    )

_LLM_PARSE_IMPORT_OK = False
parse_floorplan_with_llm = None  # type: ignore

log = logging.getLogger("sensegrid.tools")

ARTIFACT_DIR = Path(tempfile.gettempdir()) / "sensegrid_artifacts"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

_NANOBANANA_TACTILE_PROMPT = """
You are generating an accessibility tactile map for blind/low-vision users.

INPUT: a floorplan image.
OUTPUT: a single high-contrast raster image representing a tactile plaque-style map.

Hard requirements:
- Produce a CLEAN 2D DIAGRAM (orthographic top-down). No perspective, no 3D, no camera angle.
- White background with ONLY black marks. No colors, no gray, no gradients, no shadows.
- Do NOT “stylize” into a plaque with dark background. This must look like a printable diagram.
- Everything must fit inside the canvas with a ~5% margin. Do not draw or place text outside the border.
- This is a tactile map, not a Braille-only map. You do NOT need to replicate every room boundary using dots.
- You MAY use both raised lines and tactile textures:
  - walls/boundaries: thick solid lines (continuous)
  - corridors: dotted/stipple texture
  - room areas: sparse dot texture
  - special areas (restroom/courtyard/etc): distinct hatch/wave pattern
  - icons/markers: simple line icons are allowed
- Preserve topology: rooms, walls, doors, and corridors must stay in correct relative position.
- Add clear door gaps. Mark the main entrance with a clear star marker.
- Do NOT include numeric labels (no digits 0-9) and do NOT include a numbered legend.
- If you include any labels at all, they must be in **Braille only** (no Latin letters, no digits).
- Prefer an unnumbered legend that uses distinct patterns/symbols only (e.g., hatch = seating, dots = concourse, thick line = wall).
- No decorative elements, no extra branding. Text should be minimal and aligned.
- Printable and legible at A4 size.

If the source image is cluttered, simplify while preserving navigation-critical structure.
""".strip()


_FLOORPLAN_QA_CONTEXT_PROMPT = """
You are extracting a compact, navigation-focused description from a floorplan image so a Q&A agent can answer questions.

Return plain text (no JSON) with these sections, each on its own line:
- MAP_NAME: <short guess, or 'unknown'>
- SIZE: <approx width x height in meters or 'unknown'>
- ENTRANCE: <where is the main entrance, or 'unknown'>
- FEATURES: <comma-separated: stairs, elevator, restroom, food court, check-in, seating, etc. Only if visible>
- OBJECTS: bullet list of key labeled areas/POIs with relative direction from entrance (e.g. "Food court — front-right", "Stairs — left side")
- NOTES: <1 short line with any uncertainty>

Rules:
- Do not invent features not visible.
- Use relative directions (front/back/left/right/center) and rough distances if you can infer scale; otherwise omit distances.
- If text labels exist in the image, use them.
""".strip()


_TACTILE_QA_CONTEXT_PROMPT = """
You are extracting a compact, navigation-focused description from a tactile map image (black marks on white background).
This tactile map may use patterns/symbols (dots, hatching) and may not contain readable text.

Return plain text (no JSON) with these sections, each on its own line:
- MAP_NAME: <if present, else 'unknown'>
- ENTRANCE: <where the entrance marker is, e.g. "top edge", "bottom-left", or 'unknown'>
- FEATURES: <comma-separated: stairs, elevator, restroom, food, check-in, seating, tunnel, etc. Only if clearly indicated by icons/symbols>
- REGIONS: bullet list of distinct regions/zones by pattern (e.g. "dense dots region — top side", "hatched region — right side")
- COUNTS: <counts of repeated symbols if obvious, e.g. "stairs: 2">
- NOTES: <1 short line with uncertainty>

Rules:
- Do not invent labels. If you can't confidently identify a feature, omit it or mark unknown.
- Use relative positions in the image (top/bottom/left/right/center).
- Keep it short and usable for Q&A like "where are the stairs" or "how many entrances".
""".strip()


def qa_context_from_tactile_image_gemini(image_path: str, model: str = "gemini-2.5-pro") -> dict:
    """Create a compact Q&A context blob from a tactile-map image (Gemini vision)."""
    p = Path(image_path)
    if not p.exists():
        return {"error": f"image not found: {image_path}"}

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {"error": "GEMINI_API_KEY not set"}

    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except Exception as exc:
        return {"error": f"Gemini client not available: {exc}"}

    def _guess_mime(path: Path) -> str:
        suf = path.suffix.lower()
        if suf in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if suf in {".webp"}:
            return "image/webp"
        if suf in {".gif"}:
            return "image/gif"
        return "image/png"

    def _extract_text(resp_obj) -> str:
        t = (getattr(resp_obj, "text", "") or "").strip()
        if t:
            return t
        for cand in getattr(resp_obj, "candidates", []) or []:
            content = getattr(cand, "content", None)
            if not content:
                continue
            for part in getattr(content, "parts", []) or []:
                pt = (getattr(part, "text", "") or "").strip()
                if pt:
                    return pt
        for part in getattr(resp_obj, "parts", []) or []:
            pt = (getattr(part, "text", "") or "").strip()
            if pt:
                return pt
        return ""

    client = genai.Client(api_key=api_key)
    try:
        img_part = types.Part.from_bytes(data=p.read_bytes(), mime_type=_guess_mime(p))
        resp = client.models.generate_content(
            model=model,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part(text=_TACTILE_QA_CONTEXT_PROMPT),
                        img_part,
                    ],
                )
            ],
            config=types.GenerateContentConfig(
                response_mime_type="text/plain",
                temperature=0.1,
                max_output_tokens=1200,
            ),
        )
    except Exception as exc:
        return {"error": f"context generation failed: {exc}"}

    text = _extract_text(resp)
    if not text:
        return {"error": "model returned empty context"}
    return {"context_text": text, "model": model}


def qa_context_from_floorplan_image_gemini(image_path: str, model: str = "gemini-2.5-pro") -> dict:
    """Create a compact Q&A context blob from a floorplan image (Gemini vision)."""
    p = Path(image_path)
    if not p.exists():
        return {"error": f"image not found: {image_path}"}

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {"error": "GEMINI_API_KEY not set"}

    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except Exception as exc:
        return {"error": f"Gemini client not available: {exc}"}

    def _guess_mime(path: Path) -> str:
        suf = path.suffix.lower()
        if suf in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if suf in {".webp"}:
            return "image/webp"
        if suf in {".gif"}:
            return "image/gif"
        return "image/png"

    def _extract_text(resp_obj) -> str:
        t = (getattr(resp_obj, "text", "") or "").strip()
        if t:
            return t
        for cand in getattr(resp_obj, "candidates", []) or []:
            content = getattr(cand, "content", None)
            if not content:
                continue
            for part in getattr(content, "parts", []) or []:
                pt = (getattr(part, "text", "") or "").strip()
                if pt:
                    return pt
        for part in getattr(resp_obj, "parts", []) or []:
            pt = (getattr(part, "text", "") or "").strip()
            if pt:
                return pt
        return ""

    client = genai.Client(api_key=api_key)
    try:
        img_part = types.Part.from_bytes(data=p.read_bytes(), mime_type=_guess_mime(p))
        resp = client.models.generate_content(
            model=model,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part(text=_FLOORPLAN_QA_CONTEXT_PROMPT),
                        img_part,
                    ],
                )
            ],
            config=types.GenerateContentConfig(
                response_mime_type="text/plain",
                temperature=0.1,
                max_output_tokens=1200,
            ),
        )
    except Exception as exc:
        return {"error": f"context generation failed: {exc}"}

    text = _extract_text(resp)
    if not text:
        return {"error": "model returned empty context"}
    return {"context_text": text, "model": model}


def tactile_map_from_image_nanobanana(image_path: str, model: str = "gemini-3-pro-image-preview") -> dict:
    """Generate a tactile-map PNG directly from the input image (Nano Banana Pro)."""
    p = Path(image_path)
    if not p.exists():
        return {"error": f"image not found: {image_path}"}

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {"error": "GEMINI_API_KEY not set"}

    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except Exception as exc:
        return {"error": f"google-genai not available: {exc}"}

    try:
        img_bytes = p.read_bytes()
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        img_part = types.Part.from_bytes(data=img_bytes, mime_type=mime)
        client = genai.Client(api_key=api_key)
        # Match the local test script behavior: don't force aspect/size unless needed.
        cfg = types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            temperature=0.0,
        )
        resp = client.models.generate_content(
            model=model,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part(text=_NANOBANANA_TACTILE_PROMPT),
                        img_part,
                    ],
                )
            ],
            config=cfg,
        )
    except Exception as exc:
        return {"error": f"generation failed: {exc}"}

    out_path = ARTIFACT_DIR / (p.stem + "_tactile_nanobanana.png")

    # Find first image part.
    parts = getattr(resp, "parts", None) or []
    for part in parts:
        if getattr(part, "inline_data", None) is not None:
            try:
                img = part.as_image()
                img.save(out_path)
                return {"png_path": str(out_path), "model": model}
            except Exception as exc:
                return {"error": f"could not save output image: {exc}"}

    # Fallback: search candidates if needed.
    for cand in getattr(resp, "candidates", []) or []:
        content = getattr(cand, "content", None)
        if not content:
            continue
        for part in getattr(content, "parts", []) or []:
            if getattr(part, "inline_data", None) is not None:
                try:
                    img = part.as_image()
                    img.save(out_path)
                    return {"png_path": str(out_path), "model": model}
                except Exception as exc:
                    return {"error": f"could not save output image: {exc}"}

    # No image returned.
    text = ""
    for part in parts:
        if getattr(part, "text", None):
            text += part.text + "\n"
    debug_path = ARTIFACT_DIR / (p.stem + "_tactile_nanobanana.txt")
    debug_path.write_text(text or "(no image returned)", encoding="utf-8")
    return {"error": f"model returned no image (see {debug_path})"}


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

    if not _CV2_OK or cv2 is None:
        return {"error": "OpenCV not installed."}

    global extract_rooms_cv_multiscale, regions_to_floor_objects
    if extract_rooms_cv_multiscale is None or regions_to_floor_objects is None:
        from cv_rooms import extract_rooms_cv_multiscale as _ex, regions_to_floor_objects as _r2o

        extract_rooms_cv_multiscale = _ex
        regions_to_floor_objects = _r2o

    img = cv2.imread(str(p), cv2.IMREAD_COLOR)
    if img is None:
        return {"error": f"could not read image: {image_path}"}
    h, w = img.shape[:2]

    regions, _ = extract_rooms_cv_multiscale(img)

    # Labeling pipeline: Gemini only (no OpenAI dependency).
    labeling_provider = "none"
    labeling_model: str | None = None
    labeling_status = "skipped"
    labeling_error: str | None = None
    labeling_labeled = 0
    gapfill_added = 0

    gemini_key = (os.environ.get("GEMINI_API_KEY") or "").strip()

    if not regions:
        labeling_error = "no regions to label"

    # Gemini labeling path (only).
    if gemini_key and _GEMINI_IMPORT_OK and regions:
        try:
            from google import genai  # type: ignore

            client = genai.Client(api_key=gemini_key)
            labeling_model = os.environ.get("GEMINI_LABEL_MODEL", "gemini-2.5-pro")
            log.info("parse_floorplan: calling Gemini global labeling via %s…", labeling_model)
            regions = _gemini_label_global(
                img, regions, client=client, model_name=labeling_model
            )
            labeling_labeled = sum(
                1 for r in regions
                if (r.label_text or (r.label_hint and r.label_hint != "unknown"))
            )
            labeling_status = "ok" if labeling_labeled > 0 else "returned_no_labels"
            labeling_provider = "gemini"
            labeling_error = None
            log.info(
                "parse_floorplan: Gemini labeled %d/%d rooms (%s)",
                labeling_labeled, len(regions), labeling_status,
            )
        except Exception as exc:
            log.warning("parse_floorplan: Gemini labeling raised %s", exc)
            labeling_status = "error"
            labeling_error = f"gemini: {exc}"

    if labeling_provider == "none" and not labeling_error:
        if not gemini_key:
            labeling_error = "GEMINI_API_KEY not set"
        elif not _GEMINI_IMPORT_OK:
            labeling_error = "Gemini labeling module unavailable"

    # Re-stamp IDs so output is consistent after the gap-fill merge.
    from cv_rooms import RoomRegion  # local import to avoid circular
    regions = [
        RoomRegion(
            id=f"room_{i}",
            bbox_px=r.bbox_px,
            bbox_pct=r.bbox_pct,
            area_px=r.area_px,
            source=r.source,
            label_hint=r.label_hint,
            label_text=r.label_text,
        )
        for i, r in enumerate(regions)
    ]

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
    base = _finalize_floor_plan(
        floor_plan=floor_plan,
        image_path_obj=p,
        image_width=w,
        image_height=h,
        objs=objs,
    )
    base.update(
        {
            "labeling_provider": labeling_provider,
            "labeling_model": labeling_model,
            "labeling_status": labeling_status,
            "labeling_rooms_labeled": labeling_labeled,
            "labeling_gapfill_added": gapfill_added,
            # Back-compat mirror of the old gemini_* keys.
            "gemini_labeling_status": labeling_status if labeling_provider == "gemini" else "skipped",
            "gemini_rooms_labeled": labeling_labeled if labeling_provider == "gemini" else 0,
        }
    )
    if labeling_error:
        base["labeling_note"] = labeling_error
        base["gemini_labeling_note"] = labeling_error

    log.info(
        "parsed %s → %d rooms (%s); provider=%s model=%s labeled=%d/%d gapfill=+%d",
        image_path, len(objs), base["rooms_by_type"], labeling_provider,
        labeling_model, labeling_labeled, len(regions), gapfill_added,
    )
    return base


def _finalize_floor_plan(
    *,
    floor_plan: dict,
    image_path_obj: Path,
    image_width: int,
    image_height: int,
    objs: list[dict],
) -> dict:
    """Write JSON + run ADA + render PDF; build the shared result dict."""
    json_path = ARTIFACT_DIR / (image_path_obj.stem + "_result.json")
    json_path.write_text(
        json.dumps({"floor_plan": floor_plan}, indent=2), encoding="utf-8"
    )

    ada = generate_ada_recommendations(floor_plan)
    ada_pdf_path: Path | None = ARTIFACT_DIR / (image_path_obj.stem + "_ada_report.pdf")
    ada_pdf_note: str | None = None
    try:
        from ada_report_pdf import write_ada_report_pdf  # noqa: WPS433

        write_ada_report_pdf(
            ada_report=ada,
            source_image=image_path_obj.name,
            output_path=ada_pdf_path,
        )
    except ModuleNotFoundError as exc:
        ada_pdf_path = None
        ada_pdf_note = (
            f"ADA PDF report not generated: missing dependency ({exc}). "
            "Install requirements to enable PDF export."
        )
    except Exception as exc:
        log.warning("failed to render ADA PDF report: %s", exc)
        ada_pdf_path = None
        ada_pdf_note = f"ADA PDF report generation failed: {exc}"

    by_type: dict[str, int] = {}
    for o in objs:
        by_type[o["type"]] = by_type.get(o["type"], 0) + 1

    out: dict = {
        "json_path": str(json_path),
        "room_count": len(objs),
        "rooms_by_type": by_type,
        "dimensions_px": {"width": image_width, "height": image_height},
        "ada_summary": ada.get("summary", {}),
        "ada_findings": ada.get("findings", []),
        "ada_report_text": ada.get("report_text", ""),
        "ada_report_pdf_path": str(ada_pdf_path) if ada_pdf_path else None,
        "ada_note": ada.get("note", ""),
    }
    if ada_pdf_note:
        out["ada_report_pdf_note"] = ada_pdf_note
    return out


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
            "name": "tactile_map_from_image_nanobanana",
            "description": (
                "Generate a tactile-map PNG directly from an input floorplan image "
                "using Gemini Nano Banana Pro image generation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "Absolute path to a local image file."},
                    "model": {"type": "string", "description": "Gemini image model id.", "default": "gemini-3-pro-image-preview"},
                },
                "required": ["image_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "qa_context_from_floorplan_image_gemini",
            "description": (
                "Extract a compact navigation-focused text context from a floorplan image "
                "to power Q&A about the map."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "Absolute path to a local image file."},
                    "model": {"type": "string", "default": "gemini-2.5-pro"},
                },
                "required": ["image_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "qa_context_from_tactile_image_gemini",
            "description": (
                "Extract a compact navigation-focused text context from a tactile map image "
                "to power Q&A about the map."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "Absolute path to a local image file."},
                    "model": {"type": "string", "default": "gemini-2.5-pro"},
                },
                "required": ["image_path"],
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
    "tactile_map_from_image_nanobanana": tactile_map_from_image_nanobanana,
    "qa_context_from_floorplan_image_gemini": qa_context_from_floorplan_image_gemini,
    "qa_context_from_tactile_image_gemini": qa_context_from_tactile_image_gemini,
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
