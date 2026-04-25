"""Diagnose the OpenAI floor-plan parse pipeline.

Runs a sequence of checks designed to pin-point why `parse_floorplan_llm`
might be silently failing. Safe to run repeatedly; costs ~a few cents in
tokens total.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "floorplan_parser"))
sys.path.insert(0, str(REPO_ROOT / "agents"))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env", override=False)
except ImportError:
    pass


def banner(msg: str) -> None:
    print(f"\n{'=' * 70}\n{msg}\n{'=' * 70}")


def check_env() -> None:
    banner("1. Environment")
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        print("  [FAIL] OPENAI_API_KEY is NOT set in the environment.")
        print("         Put it in .env (no quotes, no trailing spaces).")
        return
    print(f"  [ok]   OPENAI_API_KEY is set (prefix {key[:10]}..., len {len(key)})")
    print(f"  [ok]   OPENAI_PARSE_MODEL = {os.environ.get('OPENAI_PARSE_MODEL', '(default gpt-5.4)')}")
    print(f"  [ok]   OPENAI_LABEL_MODEL = {os.environ.get('OPENAI_LABEL_MODEL', '(default gpt-5.4)')}")


def check_import() -> None:
    banner("2. Import the `openai` package")
    try:
        import openai
        print(f"  [ok]   openai v{openai.__version__} installed at {openai.__file__}")
    except ImportError as exc:
        print(f"  [FAIL] {exc}")
        print("         Run: pip install 'openai>=1.0.0'")
        sys.exit(1)


def check_connect() -> None:
    banner("3. Auth + list available models")
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "").strip())
        models = list(client.models.list())
        ids = sorted({m.id for m in models})
    except Exception as exc:
        print(f"  [FAIL] models.list() raised: {type(exc).__name__}: {exc}")
        return
    print(f"  [ok]   API key authenticated, account can see {len(ids)} models.")
    interesting = [
        mid for mid in ids
        if any(tag in mid for tag in ("gpt-5", "gpt-4.1", "gpt-4o", "o3", "o1"))
    ]
    print(f"  [info] Vision-capable candidates you have access to:")
    for mid in interesting:
        print(f"           • {mid}")


def check_vision_call(model: str) -> bool:
    import base64
    import io
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("  [skip] Pillow not installed — skipping vision test. `pip install pillow`.")
        return False

    img = Image.new("RGB", (256, 256), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([40, 40, 216, 216], outline="black", width=4)
    d.text((100, 120), "TEST", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    from openai import OpenAI
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "").strip())

    print(f"\n  Trying model: {model}")
    try:
        # Use `max_completion_tokens` so gpt-5.x/o* accept the call. This
        # mirrors the parameter shape used by the real parser in
        # floorplan_parser/llm_floorplan.py.
        resp = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Return JSON: {\"saw_text\": \"<word you see>\"}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_completion_tokens=2048,
        )
        content = (resp.choices[0].message.content or "") if resp.choices else ""
        finish = resp.choices[0].finish_reason if resp.choices else "?"
        print(f"    [ok]   finish={finish}  content={content!r}")
        return True
    except Exception as exc:
        print(f"    [FAIL] {type(exc).__name__}: {exc}")
        return False


def check_vision() -> None:
    banner("4. Tiny vision call (does this model work with the exact call shape our code makes?)")
    for model in ("gpt-5.4", "gpt-5.5", "gpt-4.1", "gpt-4o"):
        check_vision_call(model)


def check_end_to_end() -> None:
    banner("5. End-to-end: parse the Pauley Pavilion plan (if present)")
    candidates = [
        REPO_ROOT / "tests" / "pauley.png",
        REPO_ROOT / "samples" / "pauley.png",
        REPO_ROOT / "2010-design-standards.pdf",
    ]
    image = next((c for c in candidates if c.exists() and c.suffix in {".png", ".jpg", ".jpeg"}), None)
    if image is None:
        print("  [skip] No local sample image found at tests/ or samples/ — skipping.")
        print("         Drop a floor-plan PNG at samples/pauley.png to run this test.")
        return
    try:
        from llm_floorplan import parse_floorplan_with_llm
        result = parse_floorplan_with_llm(str(image))
        fp = result["floor_plan"]
        print(f"  [ok]   Parsed {image.name} → {len(fp['rooms'])} rooms, building={result.get('building_type')}")
        for r in fp["rooms"][:5]:
            print(f"           - {r['id']}: {r['type']:<15s} {r['label']!r}")
    except Exception as exc:
        print(f"  [FAIL] {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    check_env()
    check_import()
    check_connect()
    check_vision()
    check_end_to_end()
    print("\nDone.\n")
