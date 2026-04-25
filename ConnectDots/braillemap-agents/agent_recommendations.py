"""Agent 6 — ADA Accessibility Recommendations Agent.

Receives a `RecommendationsRequest`, fetches the room's `layout_2d`, asks an
LLM to compare it against ADA accessibility guidelines (specifically the
provisions that affect blind and low-vision navigation), renders a PDF report
of recommendations, and patches `recommendations_pdf_url` +
`status_recommendations_done` back to the room document.

The ADA guidelines used in the LLM prompt are summarised inline (no external
PDF fetch) — they cover the parts of ADAAG / 2010 ADA Standards for Accessible
Design that are most relevant to a blind shopkeeper or building owner trying
to make their space safer and more usable: tactile signage, protruding
objects, accessible routes, detectable warnings, and audible/Braille cues at
controls and elevators.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Tuple

import requests
from dotenv import load_dotenv
from reportlab.lib.colors import Color
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from uagents import Agent, Context

from layout_brief import (
    format_object_lines,
    orient_relative_to_entrance,
    resolve_space_label,
)
from schemas import RecommendationsRequest

load_dotenv()

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
AGENT_SEED_6 = os.getenv("AGENT_SEED_6")
AGENT_PORT_6 = int(os.getenv("AGENT_PORT_6", "8006"))

ASI_API_URL = os.getenv("ASI_API_URL", "https://api.asi1.ai/v1/chat/completions")
ASI_API_KEY = os.getenv("ASI_API_KEY")
ASI_MODEL = os.getenv("ASI_MODEL", "asi1-mini")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")

OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "outputs", "recommendations"
)
os.makedirs(OUTPUT_DIR, exist_ok=True)

if not AGENT_SEED_6:
    raise SystemExit("Set AGENT_SEED_6 in .env")
if not GEMINI_API_KEY and not ASI_API_KEY:
    raise SystemExit("Set at least one of GEMINI_API_KEY or ASI_API_KEY in .env")

# Lazy-init so we don't pay the import cost when only ASI is available.
_gemini_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None and GEMINI_API_KEY:
        from google import genai
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


recommendations_agent = Agent(
    name="braillemap_recommendations",
    seed=AGENT_SEED_6,
    port=AGENT_PORT_6,
    endpoint=[f"http://localhost:{AGENT_PORT_6}/submit"],
)


# ── ADA guidance (curated for blind / low-vision navigation) ─────────────────
# Citations point at the 2010 ADA Standards for Accessible Design + ADAAG
# sections most relevant to blind-user safety and orientation. Phrased as
# checkable rules so the LLM can map each one to the floor-plan layout.

ADA_GUIDANCE = """
ADA STANDARDS — BLIND / LOW-VISION ACCESSIBILITY (curated from the 2010 ADA
Standards for Accessible Design + ADAAG):

1. ACCESSIBLE ROUTE (§403)
   - Continuous unobstructed path from the entrance to every public function.
   - Minimum clear width: 36 in (915 mm), reducible to 32 in (815 mm) at
     points no longer than 24 in.
   - No abrupt level changes greater than 1/4 in without a ramp.

2. PROTRUDING OBJECTS (§307)
   - Anything mounted between 27 in (685 mm) and 80 in (2030 mm) above the
     floor must not protrude more than 4 in (100 mm) into a circulation path
     — a long-cane user cannot detect it otherwise.
   - Free-standing posts/poles between 27 in and 80 in: max 12 in overhang
     between two posts.

3. SIGNAGE — TACTILE & BRAILLE (§703)
   - Permanent room identification signs must include raised characters AND
     Grade 2 Braille.
   - Mounting: centerline of tactile characters 48 in to 60 in above the
     finish floor; sign on the latch side of the door, with 18 in × 18 in of
     clear floor space in front so a blind user can stand and read.

4. DETECTABLE WARNINGS (§705)
   - Truncated-dome surface required at platform edges, transit boarding
     edges, and the curb ramps of any pedestrian-vehicle interface.
   - Color must contrast visually with the adjacent walking surface.

5. STAIRS (§504) & RAMPS (§405)
   - Stair nosings: visual contrast strip on tread + visual contrast on the
     leading 2 in of each tread.
   - Detectable warning at the top of any stair flight not enclosed by walls.
   - Handrails on both sides, extending 12 in beyond top and bottom risers.

6. ELEVATORS & LIFTS (§407)
   - Audible signals: one tone for "up" car, two tones for "down".
   - Tactile-and-Braille floor designations on both jambs, centered 60 in
     from the floor.
   - Audible voice annunciator inside the cab announces each floor.

7. CONTROLS & OPERABLE PARTS (§309)
   - Anything the public must operate (light switches, ATM keypads, elevator
     buttons, ticket machines): tactile separator and Braille label, mounted
     between 15 in and 48 in above the floor.

8. DOORS (§404)
   - Maneuvering clearances on both sides per §404.2.4.
   - Hardware operable with one hand without tight grasping/twisting.
   - Clearly perceivable (audible/tactile) door announcement at automatic
     doors so a blind user knows when they have opened.

9. AUDIBLE / VISUAL ALARMS (§702, §215)
   - Audible alarms required throughout; visual alarms support deaf-blind
     users but are NOT a substitute for audible coverage.

10. WAYFINDING FOR BLIND USERS (best practice)
    - Tactile maps at primary entrances, Braille + raised diagram, with the
      "you-are-here" arrow oriented to the user's actual heading.
    - High-contrast, non-glare flooring transitions between zones (corridor
      vs. open area) so a low-vision user perceives the change.
    - Acoustic landmarks (water feature, white-noise zone) at major decision
      points where geometry alone is ambiguous.
""".strip()


# ── LLM prompt construction ──────────────────────────────────────────────────

ANALYSIS_PROMPT_TEMPLATE = """You are an ADA accessibility consultant generating a recommendations report for the owner of a {space_label}.

Audience: a shopkeeper, facility manager, or designer. They want a practical, prioritised list of changes they should make so their space is safer and easier to navigate for **blind and low-vision** people.

Below is the floor-plan layout (entrance-relative; +forward is deeper into the space):

Overall extent: {room_w:.1f} m wide by {room_d:.1f} m deep.
Number of walls: {num_walls}
Number of doors: {num_doors}
Number of windows: {num_windows}
Number of objects/landmarks: {num_objects}

Object list:
{layout_brief}

Authoritative guidance to evaluate against:
{ada_guidance}

Produce a JSON object — and ONLY a JSON object — with this exact schema:

{{
  "summary": "<2-3 sentence overall accessibility assessment, plain language>",
  "compliance_score": <integer 0-100 — your subjective rating>,
  "recommendations": [
    {{
      "priority": "high" | "medium" | "low",
      "category": "<one of: Accessible Route | Protruding Objects | Signage | Detectable Warnings | Stairs & Ramps | Elevators | Controls | Doors | Alarms | Wayfinding>",
      "issue": "<what is wrong or missing in THIS specific layout — reference real objects from the list when applicable>",
      "recommendation": "<the concrete change to make, in 1-2 sentences>",
      "ada_reference": "<short citation, e.g. 'ADA §703.4 — tactile signage mounting'>"
    }}
  ]
}}

Rules:
- Generate 6-12 recommendations, ordered by priority (high first).
- Be specific: tie each issue to actual objects, doors, or zones in the layout (e.g. "the reception desk 3.5 m forward of the entrance has no detectable edge").
- Do NOT recommend things that are obviously already present (don't fabricate problems).
- If the layout has no information about a topic (e.g. no stairs visible), only include a recommendation if the absence itself is a problem (e.g. "no tactile signage was identified at the entrance").
- Plain English — the shopkeeper is not an architect.
- No preamble, no markdown, no commentary — JSON only.
"""


def build_analysis_prompt(layout: Dict[str, Any], metadata: Dict[str, Any]) -> str:
    _, rel_objects = orient_relative_to_entrance(layout)
    return ANALYSIS_PROMPT_TEMPLATE.format(
        space_label=resolve_space_label(layout, metadata),
        room_w=float(layout.get("room_width") or 0.0),
        room_d=float(layout.get("room_depth") or 0.0),
        num_walls=len(layout.get("walls") or []),
        num_doors=len(layout.get("doors") or []),
        num_windows=len(layout.get("windows") or []),
        num_objects=len(rel_objects),
        layout_brief=format_object_lines(rel_objects),
        ada_guidance=ADA_GUIDANCE,
    )


# ── LLM dispatch ─────────────────────────────────────────────────────────────

def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _generate_with_asi(prompt: str) -> str:
    if not ASI_API_KEY:
        raise RuntimeError("ASI_API_KEY not set")
    resp = requests.post(
        ASI_API_URL,
        headers={
            "Authorization": f"Bearer {ASI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": ASI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
            "max_tokens": 1800,
        },
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _generate_with_gemini(prompt: str) -> str:
    client = _get_gemini_client()
    if not client:
        raise RuntimeError("GEMINI_API_KEY not set")
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return (getattr(response, "text", "") or "").strip()


def generate_recommendations_json(
    prompt: str, ctx: Context
) -> Tuple[Dict[str, Any], str]:
    """Try ASI1 first (fast), Gemini fallback. Parse the JSON response."""
    last_error: Exception | None = None

    if ASI_API_KEY:
        try:
            raw = _generate_with_asi(prompt)
            data = json.loads(_strip_code_fences(raw))
            ctx.logger.info("recommendations generated with ASI1-Mini")
            return data, "asi1-mini"
        except Exception as exc:
            last_error = exc
            ctx.logger.warning(
                f"ASI1 path failed ({exc}); trying Gemini fallback"
            )

    if GEMINI_API_KEY:
        raw = _generate_with_gemini(prompt)
        data = json.loads(_strip_code_fences(raw))
        ctx.logger.info("recommendations generated with Gemini fallback")
        return data, f"gemini:{GEMINI_MODEL}"

    raise RuntimeError(
        f"No LLM produced a recommendations JSON; last error: {last_error}"
    )


# ── PDF rendering ────────────────────────────────────────────────────────────

PRIORITY_COLORS = {
    "high": Color(0.85, 0.20, 0.18),     # red
    "medium": Color(0.95, 0.55, 0.10),   # amber
    "low": Color(0.20, 0.55, 0.85),      # blue
}


def _draw_priority_pill(c: canvas.Canvas, x: float, y: float, priority: str) -> float:
    """Renders a small coloured pill. Returns its width in points."""
    label = priority.upper()
    width = 38
    height = 14
    color = PRIORITY_COLORS.get(priority.lower(), Color(0.4, 0.4, 0.4))
    c.setFillColor(color)
    c.roundRect(x, y, width, height, 6, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 8)
    c.drawCentredString(x + width / 2, y + 4, label)
    return width


def _wrap_lines(text: str, font: str, size: float, max_width: float, c: canvas.Canvas) -> List[str]:
    words = text.split()
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        if c.stringWidth(candidate, font, size) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_pdf(
    room_id: str,
    data: Dict[str, Any],
    layout: Dict[str, Any],
    metadata: Dict[str, Any],
) -> str:
    path = os.path.join(OUTPUT_DIR, f"recommendations_{room_id}.pdf")
    c = canvas.Canvas(path, pagesize=A4)
    page_w, page_h = A4
    margin = 18 * mm

    space_label = resolve_space_label(layout, metadata)
    summary = data.get("summary") or ""
    score = int(data.get("compliance_score") or 0)
    recs: List[Dict[str, Any]] = list(data.get("recommendations") or [])

    # ── Header ──
    c.setFillColorRGB(0.10, 0.27, 0.55)
    c.rect(0, page_h - 28 * mm, page_w, 28 * mm, fill=1, stroke=0)

    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(margin, page_h - 15 * mm, "Accessibility Report")

    c.setFont("Helvetica", 11)
    c.drawString(margin, page_h - 22 * mm, f"{space_label}  •  Room {room_id[:8]}")

    # Score chip on the right
    chip_w = 38 * mm
    chip_x = page_w - margin - chip_w
    chip_y = page_h - 24 * mm
    c.setFillColorRGB(1, 1, 1)
    c.roundRect(chip_x, chip_y, chip_w, 14 * mm, 4 * mm, fill=1, stroke=0)
    c.setFillColorRGB(0.10, 0.27, 0.55)
    c.setFont("Helvetica-Bold", 22)
    c.drawCentredString(chip_x + chip_w / 2, chip_y + 7 * mm, f"{score}/100")
    c.setFont("Helvetica", 8)
    c.drawCentredString(chip_x + chip_w / 2, chip_y + 3 * mm, "ADA score")

    # ── Summary block ──
    y = page_h - 38 * mm
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, "Summary")
    y -= 5 * mm

    c.setFont("Helvetica", 10)
    summary_lines = _wrap_lines(summary, "Helvetica", 10, page_w - 2 * margin, c)
    for line in summary_lines:
        c.drawString(margin, y, line)
        y -= 4.5 * mm

    y -= 2 * mm
    c.setStrokeColorRGB(0.85, 0.85, 0.85)
    c.setLineWidth(0.5)
    c.line(margin, y, page_w - margin, y)
    y -= 6 * mm

    # ── Recommendations ──
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, f"Recommendations ({len(recs)})")
    y -= 6 * mm

    for idx, rec in enumerate(recs, start=1):
        priority = str(rec.get("priority") or "medium").lower()
        category = str(rec.get("category") or "")
        issue = str(rec.get("issue") or "")
        recommendation = str(rec.get("recommendation") or "")
        ada_ref = str(rec.get("ada_reference") or "")

        # Estimate the height we need for this entry; new page if not enough.
        body_lines = (
            _wrap_lines(f"Issue: {issue}", "Helvetica", 9.5, page_w - 2 * margin - 10, c)
            + _wrap_lines(
                f"Fix: {recommendation}",
                "Helvetica-Bold",
                9.5,
                page_w - 2 * margin - 10,
                c,
            )
        )
        block_h = 12 * mm + 4.5 * mm * len(body_lines) + (4 * mm if ada_ref else 0)

        if y - block_h < margin + 10 * mm:
            c.showPage()
            y = page_h - margin

        # Title row: number + category + priority pill (right-aligned)
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 11)
        title = f"{idx}. {category}"
        c.drawString(margin, y, title)
        pill_w = 38
        _draw_priority_pill(c, page_w - margin - pill_w, y - 2, priority)
        y -= 6 * mm

        # Issue + Fix
        c.setFillColorRGB(0.25, 0.25, 0.25)
        c.setFont("Helvetica", 9.5)
        for line in _wrap_lines(
            f"Issue: {issue}", "Helvetica", 9.5, page_w - 2 * margin - 10, c
        ):
            c.drawString(margin + 4, y, line)
            y -= 4.5 * mm

        y -= 1 * mm
        c.setFillColorRGB(0.05, 0.30, 0.18)
        c.setFont("Helvetica-Bold", 9.5)
        for line in _wrap_lines(
            f"Fix: {recommendation}",
            "Helvetica-Bold",
            9.5,
            page_w - 2 * margin - 10,
            c,
        ):
            c.drawString(margin + 4, y, line)
            y -= 4.5 * mm

        if ada_ref:
            c.setFillColorRGB(0.45, 0.45, 0.45)
            c.setFont("Helvetica-Oblique", 8.5)
            c.drawString(margin + 4, y, ada_ref)
            y -= 4 * mm

        y -= 4 * mm
        c.setStrokeColorRGB(0.92, 0.92, 0.92)
        c.line(margin, y, page_w - margin, y)
        y -= 4 * mm

    # ── Footer ──
    c.setFillColorRGB(0.55, 0.55, 0.55)
    c.setFont("Helvetica-Oblique", 8)
    c.drawCentredString(
        page_w / 2,
        12,
        "Generated by BrailleMap. Recommendations are advisory and reference the "
        "2010 ADA Standards for Accessible Design.",
    )

    c.save()
    return path


# ── Backend I/O ──────────────────────────────────────────────────────────────

def fetch_room_full(room_id: str) -> Dict[str, Any]:
    resp = requests.get(f"{BACKEND_URL}/rooms/{room_id}/full", timeout=30)
    resp.raise_for_status()
    return resp.json()


def patch_room(room_id: str, updates: Dict[str, Any]) -> None:
    resp = requests.patch(f"{BACKEND_URL}/rooms/{room_id}", json=updates, timeout=30)
    resp.raise_for_status()


def served_url(path: str) -> str:
    return f"{BACKEND_URL}/files/recommendations/{os.path.basename(path)}"


# ── Message handler ──────────────────────────────────────────────────────────

@recommendations_agent.on_message(model=RecommendationsRequest)
async def on_recommendations_request(
    ctx: Context, sender: str, msg: RecommendationsRequest
) -> None:
    room_id = msg.room_id
    ctx.logger.info(
        f"[msg] RecommendationsRequest from {sender} room={room_id}"
    )

    try:
        room = fetch_room_full(room_id)
    except Exception as exc:
        ctx.logger.error(f"failed to fetch room {room_id}: {exc}")
        return

    layout = room.get("layout_2d") or {}
    if not layout:
        ctx.logger.error(f"room {room_id} has no layout_2d — cannot recommend")
        patch_room(
            room_id,
            {"status": "error_recommendations_no_layout"},
        )
        return
    metadata = room.get("metadata") or {}

    prompt = build_analysis_prompt(layout, metadata)
    try:
        data, provider = generate_recommendations_json(prompt, ctx)
    except Exception as exc:
        ctx.logger.error(f"LLM recommendations failed: {exc}")
        patch_room(
            room_id,
            {
                "status": "error_recommendations_llm",
                "recommendations_error": str(exc),
            },
        )
        return

    try:
        path = render_pdf(room_id, data, layout, metadata)
    except Exception as exc:
        ctx.logger.error(f"recommendations PDF render failed: {exc}")
        patch_room(
            room_id,
            {
                "status": "error_recommendations_pdf",
                "recommendations_error": str(exc),
            },
        )
        return

    url = served_url(path)
    patch_room(
        room_id,
        {
            "recommendations_pdf_url": url,
            "recommendations_summary": data.get("summary"),
            "recommendations_score": data.get("compliance_score"),
            "recommendations_count": len(data.get("recommendations") or []),
            "recommendations_provider": provider,
            "status_recommendations_done": True,
        },
    )
    ctx.logger.info(f"✓ recommendations PDF saved: {url}")


if __name__ == "__main__":
    print("═" * 60)
    print(" BrailleMap Recommendations Agent (Agent 6)")
    print(f" Address       : {recommendations_agent.address}")
    print(f" Port          : {AGENT_PORT_6}")
    print(f" LLM           : ASI1-Mini → Gemini fallback")
    print(f" Output dir    : {OUTPUT_DIR}")
    print("═" * 60)
    recommendations_agent.run()
