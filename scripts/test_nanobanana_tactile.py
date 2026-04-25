#!/usr/bin/env python3
"""
Quick experiment: use "Nano Banana" (Gemini native image models) to convert a
RoomPlan/floorplan image into a tactile dot map image.

This does NOT replace the deterministic PDF pipeline — it's just a quality test
to see if Gemini 3 Pro Image ("Nano Banana Pro") can produce a more readable
tactile layout directly from an image.

Usage:
  .venv/bin/python scripts/test_nanobanana_tactile.py \
    --image /path/to/roomplan.png \
    --out-dir out_nanobanana \
    --models gemini-3-pro-image-preview gemini-3.1-flash-image-preview

Env:
  GEMINI_API_KEY=...
"""

from __future__ import annotations

import argparse
import base64
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable

from google import genai  # type: ignore
from google.genai import types  # type: ignore
from PIL import Image


PROMPT = """
You are generating an accessibility tactile map for blind/low-vision users.

INPUT: a RoomPlan / floorplan image.
OUTPUT: a single high-contrast raster image representing a tactile map.

Hard requirements:
- Produce a CLEAN 2D DIAGRAM (orthographic top-down). No perspective, no 3D, no camera angle.
- White background with ONLY black marks. No colors, no gray, no gradients, no shadows.
- Do NOT “stylize” into a plaque with dark background. This must look like a printable diagram.
- Everything must fit inside the canvas with a ~5% margin. Do not draw or place text outside the border.
- You do NOT need to exactly replicate every room boundary using dots. Prioritize usability.
- You MAY use raised lines + tactile textures:
  - walls/boundaries: thick solid lines
  - corridors: dotted/stipple texture
  - room areas: sparse dot texture
  - special areas (restroom/courtyard/etc): distinct hatch/wave pattern
  - icons: simple line icons are allowed
- Preserve topology: rooms, walls, doors, and corridors must stay in correct relative position.
- Add clear door gaps. Mark the main entrance with a clear star marker.
- Add numbered markers for key rooms/objects, and include an embedded legend panel (right side or bottom-right):
  - show visual legend + Braille under each legend label if possible
  - legend must map numbers/patterns/icons → labels
- No decorative elements, no extra branding. Text should be minimal and aligned.
- The map should be printable and still legible at A4 size.

If the source image is cluttered, simplify while preserving navigation-critical structure.
""".strip()


def _b64_image(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return mime, base64.b64encode(data).decode("ascii")


def run_model(model: str, image_path: Path, out_dir: Path) -> Path:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is not set.")

    client = genai.Client(api_key=api_key)
    mime, b64 = _b64_image(image_path)
    img_part = types.Part.from_bytes(data=base64.b64decode(b64), mime_type=mime)

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
                    types.Part(text=PROMPT),
                    img_part,
                ],
            )
        ],
        config=cfg,
    )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_model = model.replace("/", "_")
    out_path = out_dir / f"tactile_{safe_model}_{ts}.png"

    # Prefer the first image part in the response.
    for part in getattr(resp, "parts", []) or []:
        if getattr(part, "inline_data", None) is not None:
            img = part.as_image()
            img.save(out_path)
            return out_path

    # Fallback: search candidates if needed.
    for cand in getattr(resp, "candidates", []) or []:
        content = getattr(cand, "content", None)
        if not content:
            continue
        for part in getattr(content, "parts", []) or []:
            if getattr(part, "inline_data", None) is not None:
                img = part.as_image()
                img.save(out_path)
                return out_path

    # If no image was returned, dump any text for debugging.
    txt = ""
    for part in getattr(resp, "parts", []) or []:
        if getattr(part, "text", None):
            txt += part.text + "\n"
    debug_path = out_dir / f"tactile_{safe_model}_{ts}.txt"
    debug_path.write_text(txt or "(no image returned)", encoding="utf-8")
    raise RuntimeError(f"No image returned by {model}. Debug written to {debug_path}")


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True, help="Path to the RoomPlan / floorplan image (png/jpg).")
    ap.add_argument("--out-dir", default="out_nanobanana", help="Directory to save outputs.")
    ap.add_argument(
        "--models",
        nargs="+",
        default=["gemini-3-pro-image-preview"],
        help="Models to try. Examples: gemini-3-pro-image-preview, gemini-3.1-flash-image-preview",
    )
    args = ap.parse_args(list(argv) if argv is not None else None)

    image_path = Path(args.image).expanduser().resolve()
    if not image_path.exists():
        raise SystemExit(f"Image not found: {image_path}")

    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Input : {image_path}")
    print(f"Outdir: {out_dir}")
    for m in args.models:
        print(f"- running {m}…")
        out = run_model(m, image_path, out_dir)
        print(f"  saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

