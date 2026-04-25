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
from ada_advisor import generate_ada_recommendations  # noqa: E402
from render_map import render_floor_plan  # noqa: E402

try:
    from braille_map import render_tactile_map  # noqa: E402
    _BRAILLE_IMPORT_OK = True
except Exception as _braille_import_exc:  # pragma: no cover
    render_tactile_map = None  # type: ignore
    _BRAILLE_IMPORT_OK = False
    logging.getLogger("sensegrid.tools").warning(
        "Tactile braille map module unavailable: %s", _braille_import_exc
    )

try:
    from cv_gemini_refine import label_regions_global as _gemini_label_global  # noqa: E402
    _GEMINI_IMPORT_OK = True
except Exception as _gemini_import_exc:  # pragma: no cover
    _gemini_label_global = None  # type: ignore
    _GEMINI_IMPORT_OK = False
    logging.getLogger("sensegrid.tools").warning(
        "Gemini labeling unavailable: %s", _gemini_import_exc
    )

try:
    from cv_openai_refine import (  # noqa: E402
        label_regions_global as _openai_label_global,
        find_missed_regions as _openai_find_missed,
    )
    _OPENAI_IMPORT_OK = True
except Exception as _openai_import_exc:  # pragma: no cover
    _openai_label_global = None  # type: ignore
    _openai_find_missed = None  # type: ignore
    _OPENAI_IMPORT_OK = False
    logging.getLogger("sensegrid.tools").warning(
        "OpenAI labeling unavailable: %s", _openai_import_exc
    )

try:
    from llm_floorplan import parse_floorplan_with_llm  # noqa: E402
    _LLM_PARSE_IMPORT_OK = True
except Exception as _llm_parse_import_exc:  # pragma: no cover
    parse_floorplan_with_llm = None  # type: ignore
    _LLM_PARSE_IMPORT_OK = False
    logging.getLogger("sensegrid.tools").warning(
        "End-to-end LLM parser unavailable: %s", _llm_parse_import_exc
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

    # Labeling pipeline: prefer OpenAI (GPT-4o vision) when the key is set,
    # fall back to Gemini, then leave rooms as "unknown" if neither is
    # available.  We record the provider actually used so the caller can
    # surface it in the UI / debug logs.
    labeling_provider = "none"
    labeling_model: str | None = None
    labeling_status = "skipped"
    labeling_error: str | None = None
    labeling_labeled = 0
    gapfill_added = 0

    openai_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    gemini_key = (os.environ.get("GEMINI_API_KEY") or "").strip()

    if not regions:
        labeling_error = "no regions to label"
    elif openai_key and _OPENAI_IMPORT_OK:
        labeling_provider = "openai"
        labeling_model = os.environ.get("OPENAI_LABEL_MODEL", "gpt-5.4")
        try:
            log.info("parse_floorplan: calling OpenAI global labeling via %s…", labeling_model)
            regions = _openai_label_global(
                img, regions, model_name=labeling_model, api_key=openai_key,
            )
            labeling_labeled = sum(
                1 for r in regions
                if (r.label_text or (r.label_hint and r.label_hint != "unknown"))
            )
            labeling_status = "ok" if labeling_labeled > 0 else "returned_no_labels"
            log.info(
                "parse_floorplan: OpenAI labeled %d/%d rooms (%s)",
                labeling_labeled, len(regions), labeling_status,
            )

            # Gap-fill: ask OpenAI for rooms the CV missed. Opt-in (default on
            # for OpenAI because it costs ~1 extra call).
            if os.environ.get("OPENAI_GAPFILL", "1").lower() not in ("0", "false", "no"):
                try:
                    extra = _openai_find_missed(
                        img, regions,
                        model_name=os.environ.get("OPENAI_GAPFILL_MODEL", labeling_model),
                        api_key=openai_key,
                    )
                    if extra:
                        regions = list(regions) + list(extra)
                        gapfill_added = len(extra)
                        log.info("parse_floorplan: OpenAI gap-fill added %d regions", gapfill_added)
                except Exception as exc:
                    log.warning("parse_floorplan: OpenAI gap-fill raised %s", exc)
        except Exception as exc:
            log.warning("parse_floorplan: OpenAI labeling raised %s — falling back to Gemini", exc)
            labeling_status = "error"
            labeling_error = f"openai: {exc}"
            labeling_provider = "openai_failed"

    # Gemini fallback path: either no OpenAI key, or OpenAI raised.
    if (
        labeling_provider in ("none", "openai_failed")
        and gemini_key
        and _GEMINI_IMPORT_OK
        and regions
    ):
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
        if not openai_key and not gemini_key:
            labeling_error = "no labeling key set (OPENAI_API_KEY / GEMINI_API_KEY)"
        elif not (_OPENAI_IMPORT_OK or _GEMINI_IMPORT_OK):
            labeling_error = "no labeling module importable"

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


# ── Tool 2b: parse_floorplan_llm (OpenAI end-to-end) ─────────────────────────


def parse_floorplan_llm(image_path: str) -> dict:
    """End-to-end floor-plan parse using an OpenAI vision model (GPT-4o).

    Bypasses the OpenCV region extractor entirely and asks the LLM to parse
    the full plan in one call. This tends to produce more accurate rooms
    and labels on well-drawn architectural plans.

    Returns a dict shaped like `parse_floorplan` so the agent can treat both
    tools interchangeably. Callers should fall back to `parse_floorplan` on
    error.
    """
    p = Path(image_path)
    if not p.exists():
        return {"error": f"image not found: {image_path}"}
    if not _LLM_PARSE_IMPORT_OK or parse_floorplan_with_llm is None:
        return {"error": "llm_floorplan module unavailable"}
    if not (os.environ.get("OPENAI_API_KEY") or "").strip():
        return {"error": "OPENAI_API_KEY not set"}

    try:
        parsed = parse_floorplan_with_llm(
            str(p),
            source_image_name=p.name,
        )
    except Exception as exc:
        log.warning("parse_floorplan_llm: LLM extraction failed: %s", exc)
        return {"error": f"LLM extraction failed: {exc}"}

    floor_plan = parsed["floor_plan"]
    building_type = parsed.get("building_type", "unknown")
    dims = floor_plan.get("dimensions_px") or {}
    w = int(dims.get("width", 0))
    h = int(dims.get("height", 0))
    objs = list(floor_plan.get("rooms", []))

    base = _finalize_floor_plan(
        floor_plan=floor_plan,
        image_path_obj=p,
        image_width=w,
        image_height=h,
        objs=objs,
    )
    base.update(
        {
            "labeling_provider": "openai_end_to_end",
            "labeling_model": os.environ.get("OPENAI_PARSE_MODEL", "gpt-5.4"),
            "labeling_status": "ok" if objs else "returned_no_rooms",
            "labeling_rooms_labeled": sum(
                1 for o in objs if o.get("type") and o["type"] != "unknown"
            ),
            "labeling_gapfill_added": 0,
            "building_type": building_type,
        }
    )
    log.info(
        "parse_floorplan_llm: %s → %d rooms, %d corridors, %d verticals (building=%s)",
        image_path,
        len(objs),
        len(floor_plan.get("corridors", [])),
        len(floor_plan.get("verticals", [])),
        building_type,
    )
    return base


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


# ── Tool 3b: generate_braille_map ────────────────────────────────────────────


def generate_braille_map(json_path: str, blurred: bool = False) -> dict:
    """Render the parsed JSON as a *tactile Braille map* for a blind user.

    Produces two files:
      * ``<stem>_tactile.png`` — high-contrast map with distinct hatched
        textures per room category, real Braille dots for every room
        label, a Braille-labelled legend panel, compass rose + scale bar.
        Designed to be printed on swell paper or sent to a tactile
        embosser.
      * ``<stem>_tactile.txt`` — plain-text screen-reader companion that
        walks through the building by compass zone.

    When ``blurred=True`` the PNG is returned with a heavy Gaussian blur
    so it can be shared as a teaser preview before payment is settled;
    the text companion is omitted in that case.
    """
    p = Path(json_path)
    if not p.exists():
        return {"error": f"json not found: {json_path}"}
    if not _BRAILLE_IMPORT_OK or render_tactile_map is None:
        return {"error": "braille_map module unavailable (see server logs)"}

    data = json.loads(p.read_text(encoding="utf-8"))
    fp = data.get("floor_plan") or data

    stem = p.stem.replace("_result", "")
    suffix_png = "_tactile_preview.png" if blurred else "_tactile.png"
    png_path = ARTIFACT_DIR / f"{stem}{suffix_png}"
    txt_path = ARTIFACT_DIR / f"{stem}_tactile.txt"

    title = fp.get("source_image") or stem
    try:
        render_tactile_map(
            floor_plan=fp,
            output_png=png_path,
            output_txt=txt_path,
            title=title,
        )
    except Exception as exc:
        log.exception("tactile map render failed")
        return {"error": f"tactile map render failed: {exc}"}

    if blurred:
        try:
            from PIL import Image, ImageFilter  # noqa: WPS433
            img = Image.open(png_path)
            # Scale blur radius with image width so the paywall preview
            # stays obviously blurred at any display resolution.
            radius = max(30, img.width // 40)
            img = img.filter(ImageFilter.GaussianBlur(radius=radius))
            img.save(png_path, format="PNG")
        except Exception as exc:
            log.warning("tactile preview blur failed: %s", exc)

    log.info("tactile map: %s → %s / %s (blurred=%s)",
             json_path, png_path, txt_path, blurred)
    return {
        "png_path": str(png_path),
        "txt_path": str(txt_path) if not blurred else None,
        "rooms_rendered": len(fp.get("rooms", []) or []),
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
            "name": "parse_floorplan_llm",
            "description": (
                "End-to-end floor-plan parse using an OpenAI vision model. Preferred "
                "over parse_floorplan when OPENAI_API_KEY is available — tends to "
                "produce more accurate rooms and labels for architectural plans."
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
            "name": "generate_braille_map",
            "description": (
                "Generate a tactile Braille map (PNG + text companion) from a "
                "parsed floor-plan JSON. Use this instead of reconstruct_floorplan "
                "when the user wants an accessibility/tactile output for a blind "
                "reader. The PNG is print-ready for swell paper / tactile embossers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "json_path": {"type": "string", "description": "Absolute path to a result.json produced by parse_floorplan."},
                    "blurred": {"type": "boolean", "description": "If true, return a blurred teaser preview (paywall-safe)."},
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
    "parse_floorplan_llm": parse_floorplan_llm,
    "reconstruct_floorplan": reconstruct_floorplan,
    "generate_braille_map": generate_braille_map,
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
