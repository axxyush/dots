from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

# Ensure repo root importable when running from anywhere
REPO_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PYTHONPATH", str(REPO_ROOT))

try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(REPO_ROOT / ".env", override=False)
except Exception:
    pass

from agents.tools import dispatch  # noqa: E402

app = FastAPI(title="dots backend", version="0.1.0")


class UrlRequest(BaseModel):
    image_url: str = Field(..., description="Public http(s) URL to a floorplan image.")
    model: str = Field("gemini-3-pro-image-preview", description="Gemini image model id.")


class TactileResponse(BaseModel):
    tactile_png_url: str
    tactile_png_path: str


class AdaResponse(BaseModel):
    ada_pdf_url: str
    ada_report_pdf_path: str
    ada_summary: dict[str, int] = Field(default_factory=dict)
    ada_report_text: str = ""
    ada_findings_count: int = 0


@app.get("/health")
def health() -> dict:
    return {"ok": True}


def _save_upload(upload: UploadFile) -> Path:
    suffix = Path(upload.filename or "").suffix or ".png"
    out_dir = Path(tempfile.gettempdir()) / "dots_backend"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"upload_{os.urandom(6).hex()}{suffix}"
    data = upload.file.read()
    out_path.write_bytes(data)
    return out_path


def _generate_ada_response(image_path: Path | str) -> AdaResponse:
    parsed = dispatch("parse_floorplan", {"image_path": str(image_path)})
    if "error" in parsed:
        raise HTTPException(status_code=500, detail=str(parsed["error"]))

    pdf_path = parsed.get("ada_report_pdf_path")
    if not isinstance(pdf_path, str) or not pdf_path.strip():
        note = (
            parsed.get("ada_report_pdf_note")
            or parsed.get("ada_note")
            or "ADA report PDF was not generated."
        )
        raise HTTPException(status_code=500, detail=str(note))

    up = dispatch("upload_artifact", {"file_path": pdf_path})
    if "error" in up:
        raise HTTPException(status_code=502, detail=str(up["error"]))

    return AdaResponse(
        ada_pdf_url=up["url"],
        ada_report_pdf_path=pdf_path,
        ada_summary=parsed.get("ada_summary") or {},
        ada_report_text=str(parsed.get("ada_report_text") or ""),
        ada_findings_count=len(parsed.get("ada_findings") or []),
    )


@app.post("/tactile/from-url", response_model=TactileResponse)
def tactile_from_url(req: UrlRequest) -> TactileResponse:
    dl = dispatch("download_image", {"url": req.image_url})
    if "error" in dl:
        raise HTTPException(status_code=400, detail=str(dl["error"]))

    t = dispatch(
        "tactile_map_from_image_nanobanana",
        {"image_path": dl["image_path"], "model": req.model},
    )
    if "error" in t:
        raise HTTPException(status_code=500, detail=str(t["error"]))

    up = dispatch("upload_artifact", {"file_path": t["png_path"]})
    if "error" in up:
        raise HTTPException(status_code=502, detail=str(up["error"]))

    return TactileResponse(tactile_png_url=up["url"], tactile_png_path=t["png_path"])


@app.post("/tactile/from-upload", response_model=TactileResponse)
def tactile_from_upload(
    file: UploadFile = File(...),
    model: str = "gemini-3-pro-image-preview",
) -> TactileResponse:
    p = _save_upload(file)
    t = dispatch("tactile_map_from_image_nanobanana", {"image_path": str(p), "model": model})
    if "error" in t:
        raise HTTPException(status_code=500, detail=str(t["error"]))
    up = dispatch("upload_artifact", {"file_path": t["png_path"]})
    if "error" in up:
        raise HTTPException(status_code=502, detail=str(up["error"]))
    return TactileResponse(tactile_png_url=up["url"], tactile_png_path=t["png_path"])


@app.post("/ada/from-url", response_model=AdaResponse)
def ada_from_url(req: UrlRequest) -> AdaResponse:
    dl = dispatch("download_image", {"url": req.image_url})
    if "error" in dl:
        raise HTTPException(status_code=400, detail=str(dl["error"]))
    return _generate_ada_response(dl["image_path"])


@app.post("/ada/from-upload", response_model=AdaResponse)
def ada_from_upload(file: UploadFile = File(...)) -> AdaResponse:
    p = _save_upload(file)
    return _generate_ada_response(p)
