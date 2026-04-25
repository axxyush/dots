"""Agent 3 — Map Generator.

Fetches the enriched 2D layout and renders it as a Braille-style dot-grid
PDF (page 1 = tactile map, page 2 = Braille legend), saves the PDF locally,
and patches the room document with the `pdf_url`.

The legend page uses both visual text AND Braille Unicode characters
rendered with Apple Braille font, so the PDF can be printed on swell paper
(microcapsule / thermoform) for actual tactile reading.

NOTE: Cloudinary is disabled for local testing. Re-enable by setting
Set_cloudinary=true in .env and providing CLOUDINARY_* credentials.
"""

from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Tuple

import requests
from dotenv import load_dotenv
from pybraille import convertText as to_braille
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from uagents import Agent, Context

from schemas import MapGenerationRequest

load_dotenv()

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
AGENT_SEED_3 = os.getenv("AGENT_SEED_3")
AGENT_PORT_3 = int(os.getenv("AGENT_PORT_3", "8003"))
USE_CLOUDINARY = os.getenv("Set_cloudinary", "false").lower() == "true"

# Local output directory — created next to this script
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs", "pdfs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

if not AGENT_SEED_3:
    raise SystemExit("Set AGENT_SEED_3 in .env")

# ── Register Braille font ────────────────────────────────────────────────────
BRAILLE_FONT_PATH = "/System/Library/Fonts/Apple Braille.ttf"
_braille_font_available = False
try:
    pdfmetrics.registerFont(TTFont("AppleBraille", BRAILLE_FONT_PATH))
    _braille_font_available = True
except Exception as e:
    print(f"[map] WARNING: Could not load Braille font: {e}")
    print("[map] Braille characters will appear as boxes. Install Apple Braille font.")

if USE_CLOUDINARY:
    import cloudinary
    import cloudinary.uploader
    CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
    CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
    CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")
    if not (CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET):
        raise SystemExit("Set CLOUDINARY_* vars in .env or set Set_cloudinary=false")
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True,
    )

map_agent = Agent(
    name="braillemap_map_generator",
    seed=AGENT_SEED_3,
    port=AGENT_PORT_3,
    endpoint=[f"http://localhost:{AGENT_PORT_3}/submit"],
)


# ── Drawing primitives ───────────────────────────────────────────────────────

DOT_SPACING_PTS = 5.0     # distance between Braille dots (tighter for better resolution)
WALL_DOT_RADIUS = 2.2     # thick walls for clear tactile feel
OBJECT_DOT_RADIUS = 1.5
WINDOW_DOT_RADIUS = 1.2
DOOR_GAP_RADIUS = 6.0


def _draw_dot_line(
    c: canvas.Canvas,
    x1: float, y1: float, x2: float, y2: float,
    spacing: float, radius: float,
    dashed: bool = False,
) -> None:
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length <= 0:
        c.circle(x1, y1, radius, fill=1, stroke=0)
        return
    n = max(2, int(length / spacing))
    for i in range(n + 1):
        if dashed and (i // 2) % 2 == 1:
            continue
        t = i / n
        c.circle(x1 + t * dx, y1 + t * dy, radius, fill=1, stroke=0)


def _draw_dot_rect(
    c: canvas.Canvas,
    cx: float, cy: float, w: float, h: float,
    spacing: float, radius: float,
) -> None:
    """Filled dot rectangle for objects — clear tactile cluster."""
    w = max(w, spacing * 2)
    h = max(h, spacing * 2)
    nx = max(2, int(w / spacing))
    ny = max(2, int(h / spacing))
    for ix in range(nx):
        for iy in range(ny):
            x = cx - w / 2 + (ix + 0.5) * (w / nx)
            y = cy - h / 2 + (iy + 0.5) * (h / ny)
            c.circle(x, y, radius, fill=1, stroke=0)


def _draw_star(c: canvas.Canvas, cx: float, cy: float, r: float) -> None:
    """Five-pointed star for entrance marker."""
    points = []
    for i in range(10):
        angle = math.pi / 2 + i * math.pi / 5
        radius = r if i % 2 == 0 else r * 0.45
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    p = c.beginPath()
    p.moveTo(*points[0])
    for pt in points[1:]:
        p.lineTo(*pt)
    p.close()
    c.drawPath(p, fill=1, stroke=0)


def _draw_triangle(c: canvas.Canvas, cx: float, cy: float, r: float) -> None:
    """Equilateral triangle for door marker."""
    points = []
    for i in range(3):
        angle = math.pi / 2 + i * 2 * math.pi / 3
        points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    p = c.beginPath()
    p.moveTo(*points[0])
    for pt in points[1:]:
        p.lineTo(*pt)
    p.close()
    c.drawPath(p, fill=1, stroke=0)


def _draw_circle_outline(c: canvas.Canvas, cx: float, cy: float, r: float, num_dots: int = 16) -> None:
    """Circle of dots for window marker."""
    for i in range(num_dots):
        angle = i * 2 * math.pi / num_dots
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        c.circle(x, y, 1.0, fill=1, stroke=0)


# ── Layout → wall endpoints ──────────────────────────────────────────────────

def _wall_endpoints(wall: Dict[str, Any]) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """Wall endpoints in room coordinates (meters)."""
    cx, cy = float(wall["x"]), float(wall["y"])
    half = float(wall["width"]) / 2.0
    yaw = float(wall.get("rotation_y", 0.0))
    dx = half * math.cos(yaw)
    dy = -half * math.sin(yaw)
    return (cx - dx, cy - dy), (cx + dx, cy + dy)


# ── Braille text helper ──────────────────────────────────────────────────────

def _draw_braille_text(c: canvas.Canvas, x: float, y: float, text: str, size: float = 20) -> float:
    """Draw Braille unicode text. Returns the width used."""
    braille = to_braille(text)
    if _braille_font_available:
        c.setFont("AppleBraille", size)
        c.drawString(x, y, braille)
        return c.stringWidth(braille, "AppleBraille", size)
    else:
        # Fallback to Helvetica (won't render properly but won't crash)
        c.setFont("Helvetica", size * 0.6)
        c.drawString(x, y, braille)
        return c.stringWidth(braille, "Helvetica", size * 0.6)


def _draw_dual_text(c: canvas.Canvas, x: float, y: float, text: str,
                    visual_size: float = 11, braille_size: float = 20) -> float:
    """Draw visual text on one line and Braille translation below it. Returns total height used."""
    c.setFont("Helvetica", visual_size)
    c.drawString(x, y, text)
    _draw_braille_text(c, x, y - braille_size - 2, text, braille_size)
    return visual_size + braille_size + 6


# ── PDF generation ───────────────────────────────────────────────────────────

def generate_pdf(room_id: str, layout: Dict[str, Any], metadata: Dict[str, Any]) -> str:
    path = os.path.join(OUTPUT_DIR, f"room_{room_id}.pdf")
    c = canvas.Canvas(path, pagesize=A4)
    page_w, page_h = A4
    margin = 40

    room_w = float(layout.get("room_width") or 0.0)
    room_d = float(layout.get("room_depth") or 0.0)
    walls: List[Dict[str, Any]] = layout.get("walls") or []
    doors: List[Dict[str, Any]] = layout.get("doors") or []
    windows: List[Dict[str, Any]] = layout.get("windows") or []
    objects: List[Dict[str, Any]] = layout.get("objects") or []
    entrance = layout.get("entrance") or {}

    room_label = metadata.get("room_name", "Unnamed Room")
    building = metadata.get("building_name", "")

    # ══════════════════════════════════════════════════════════════════════════
    # PAGE 1: TACTILE MAP
    # ══════════════════════════════════════════════════════════════════════════

    header_height = 80
    footer_height = 60
    usable_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin - header_height - footer_height
    scale = min(usable_w / max(room_w, 0.1), usable_h / max(room_d, 0.1))
    map_w = room_w * scale
    map_h = room_d * scale
    origin_x = margin + (usable_w - map_w) / 2
    origin_y = margin + footer_height

    def to_page(mx: float, my: float) -> Tuple[float, float]:
        return origin_x + mx * scale, origin_y + my * scale

    # ── Header ──
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 16)
    title = f"BrailleMap — {room_label}"
    c.drawString(margin, page_h - margin, title)
    # Braille title below
    _draw_braille_text(c, margin, page_h - margin - 24, room_label, 22)

    c.setFont("Helvetica", 9)
    sub = f"{building}   •   {room_w:.1f}m × {room_d:.1f}m   •   ID: {room_id[:12]}"
    c.drawString(margin, page_h - margin - 46, sub)

    # ── Map bounding box ──
    c.setStrokeColorRGB(0.8, 0.8, 0.8)
    c.setLineWidth(0.3)
    c.rect(origin_x - 4, origin_y - 4, map_w + 8, map_h + 8, stroke=1, fill=0)

    # ── Walls — thick dot lines ──
    c.setFillColorRGB(0, 0, 0)
    for wall in walls:
        (sx, sy), (ex, ey) = _wall_endpoints(wall)
        psx, psy = to_page(sx, sy)
        pex, pey = to_page(ex, ey)
        _draw_dot_line(c, psx, psy, pex, pey, DOT_SPACING_PTS, WALL_DOT_RADIUS)

    # ── Windows — dashed dot line ──
    c.setFillColorRGB(0.15, 0.15, 0.15)
    for win in windows:
        yaw = float(win.get("rotation_y", 0.0))
        half = float(win["width"]) / 2.0
        cx, cy = float(win["x"]), float(win["y"])
        dx = half * math.cos(yaw)
        dy = -half * math.sin(yaw)
        psx, psy = to_page(cx - dx, cy - dy)
        pex, pey = to_page(cx + dx, cy + dy)
        _draw_dot_line(c, psx, psy, pex, pey, DOT_SPACING_PTS, WINDOW_DOT_RADIUS, dashed=True)
        # Window circle marker at center
        pcx, pcy = to_page(cx, cy)
        _draw_circle_outline(c, pcx, pcy, 5, 10)

    # ── Doors — gap marker with triangle or star ──
    for door in doors:
        dx_page, dy_page = to_page(float(door["x"]), float(door["y"]))
        # Clear wall dots behind door
        c.setFillColorRGB(1, 1, 1)
        c.circle(dx_page, dy_page, DOOR_GAP_RADIUS + 2, fill=1, stroke=0)
        # Draw door marker
        if door.get("is_entrance"):
            c.setFillColorRGB(0, 0, 0)
            _draw_star(c, dx_page, dy_page, 10)
        else:
            c.setFillColorRGB(0, 0, 0)
            _draw_triangle(c, dx_page, dy_page, 6)

    # ── Objects — dot rectangles with numbered labels ──
    c.setFillColorRGB(0, 0, 0)
    for obj in objects:
        ox, oy = to_page(float(obj["x"]), float(obj["y"]))
        ow = float(obj["width"]) * scale
        od = float(obj["depth"]) * scale
        _draw_dot_rect(c, ox, oy, ow, od, DOT_SPACING_PTS, OBJECT_DOT_RADIUS)
        # Draw number label (large and bold for tactile)
        num = int(obj["index"]) + 1
        c.setFont("Helvetica-Bold", 11)
        label_x = ox + max(ow / 2, 8) + 3
        label_y = oy - 4
        c.drawString(label_x, label_y, str(num))
        # Braille number next to it
        braille_num = to_braille(str(num))
        if _braille_font_available:
            c.setFont("AppleBraille", 16)
            c.drawString(label_x + 12, label_y - 2, braille_num)

    # ── Scale bar ──
    c.setFillColorRGB(0, 0, 0)
    bar_x = margin
    bar_y = margin + 10
    c.setLineWidth(2.0)
    c.setStrokeColorRGB(0, 0, 0)
    c.line(bar_x, bar_y, bar_x + scale, bar_y)
    c.line(bar_x, bar_y - 4, bar_x, bar_y + 4)
    c.line(bar_x + scale, bar_y - 4, bar_x + scale, bar_y + 4)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(bar_x + scale + 6, bar_y - 3, "1 meter")

    # ── Inline legend key (compact, at bottom) ──
    key_y = bar_y + 20
    c.setFont("Helvetica", 8)
    c.setFillColorRGB(0, 0, 0)
    # Star
    _draw_star(c, margin + 5, key_y + 2, 5)
    c.drawString(margin + 14, key_y - 1, "Entrance")
    # Triangle
    _draw_triangle(c, margin + 70, key_y + 2, 4)
    c.drawString(margin + 78, key_y - 1, "Door")
    # Dashed dots
    _draw_dot_line(c, margin + 110, key_y + 2, margin + 130, key_y + 2, 4, 1.0, dashed=True)
    c.drawString(margin + 134, key_y - 1, "Window")
    # Dot cluster
    _draw_dot_rect(c, margin + 185, key_y + 2, 10, 6, 4, 1.0)
    c.drawString(margin + 194, key_y - 1, "Object (numbered)")

    # ══════════════════════════════════════════════════════════════════════════
    # PAGE 2: LEGEND (Visual + Braille)
    # ══════════════════════════════════════════════════════════════════════════
    c.showPage()
    c.setFillColorRGB(0, 0, 0)

    # Title
    c.setFont("Helvetica-Bold", 18)
    c.drawString(margin, page_h - margin, "Legend")
    _draw_braille_text(c, margin + 80, page_h - margin, "Legend", 22)

    # Subtitle
    c.setFont("Helvetica", 10)
    sub = f"{room_label} — {len(objects)} object(s), {len(doors)} door(s), {len(windows)} window(s)"
    c.drawString(margin, page_h - margin - 20, sub)

    y = page_h - margin - 55

    # ── Objects section ──
    c.setFont("Helvetica-Bold", 13)
    c.drawString(margin, y, "Objects")
    _draw_braille_text(c, margin + 65, y, "objects", 18)
    y -= 8

    for obj in objects:
        if y < margin + 80:
            c.showPage()
            c.setFillColorRGB(0, 0, 0)
            y = page_h - margin

        num = int(obj["index"]) + 1
        label = obj["category"]
        original = obj.get("original_category")
        pos = f"({float(obj['x']):.1f}m, {float(obj['y']):.1f}m)"
        size = f"{float(obj['width']):.1f} x {float(obj['depth']):.1f}m"

        # Visual line
        y -= 16
        c.setFont("Helvetica-Bold", 11)
        c.drawString(margin, y, f"{num}.")
        c.setFont("Helvetica", 11)
        text = f"  {label}"
        if original and original != label:
            text += f"  [was: {original}]"
        c.drawString(margin + 16, y, text)

        # Position/size on next line
        y -= 13
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.3, 0.3, 0.3)
        c.drawString(margin + 16, y, f"Position: {pos}  •  Size: {size}")
        c.setFillColorRGB(0, 0, 0)

        # Braille translation of the label
        y -= 22
        braille_line = f"{num} {label}"
        _draw_braille_text(c, margin + 16, y, braille_line, 18)
        y -= 6

    # ── Openings section ──
    if doors or windows:
        if y < margin + 100:
            c.showPage()
            c.setFillColorRGB(0, 0, 0)
            y = page_h - margin

        y -= 20
        c.setFont("Helvetica-Bold", 13)
        c.drawString(margin, y, "Openings")
        _draw_braille_text(c, margin + 75, y, "openings", 18)

        for door in doors:
            y -= 20
            if y < margin + 40:
                c.showPage()
                c.setFillColorRGB(0, 0, 0)
                y = page_h - margin

            tag = "Entrance" if door.get("is_entrance") else "Door"
            pos = f"({float(door['x']):.1f}m, {float(door['y']):.1f}m)"
            width = f"{float(door['width']):.1f}m wide"

            c.setFont("Helvetica", 11)
            visual = f"  {tag} at {pos}, {width}"
            if door.get("is_entrance"):
                _draw_star(c, margin + 6, y + 3, 5)
            else:
                _draw_triangle(c, margin + 6, y + 3, 4)
            c.drawString(margin + 16, y, visual)

            # Braille
            y -= 22
            _draw_braille_text(c, margin + 16, y, f"{tag} {pos}", 18)

        for win in windows:
            y -= 20
            if y < margin + 40:
                c.showPage()
                c.setFillColorRGB(0, 0, 0)
                y = page_h - margin

            pos = f"({float(win['x']):.1f}m, {float(win['y']):.1f}m)"
            width = f"{float(win['width']):.1f}m wide"

            _draw_circle_outline(c, margin + 6, y + 3, 4, 8)
            c.setFont("Helvetica", 11)
            c.drawString(margin + 16, y, f"  Window at {pos}, {width}")

            y -= 22
            _draw_braille_text(c, margin + 16, y, f"window {pos}", 18)

    if entrance and entrance.get("kind") == "wall_midpoint":
        y -= 20
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(margin, y, "(No door detected — entrance approximated as midpoint of longest wall.)")

    c.save()
    return path


def save_pdf_locally(path: str, room_id: str) -> str:
    """Returns a backend-served URL for the PDF."""
    filename = os.path.basename(path)
    return f"{BACKEND_URL}/files/pdfs/{filename}"


def upload_pdf(path: str, room_id: str) -> str:
    """Upload to Cloudinary if enabled, otherwise serve locally."""
    if USE_CLOUDINARY:
        result = cloudinary.uploader.upload(
            path,
            resource_type="raw",
            folder="braillemap/pdfs",
            public_id=f"room_{room_id}",
            overwrite=True,
            use_filename=False,
            unique_filename=False,
        )
        return result["secure_url"]
    return save_pdf_locally(path, room_id)


# ── Backend I/O ──────────────────────────────────────────────────────────────

def fetch_room_full(room_id: str) -> Dict[str, Any]:
    resp = requests.get(f"{BACKEND_URL}/rooms/{room_id}/full", timeout=30)
    resp.raise_for_status()
    return resp.json()


def patch_room(room_id: str, updates: Dict[str, Any]) -> None:
    resp = requests.patch(f"{BACKEND_URL}/rooms/{room_id}", json=updates, timeout=30)
    resp.raise_for_status()


# ── Message handler ──────────────────────────────────────────────────────────

@map_agent.on_message(model=MapGenerationRequest)
async def on_map_request(ctx: Context, sender: str, msg: MapGenerationRequest) -> None:
    room_id = msg.room_id
    ctx.logger.info(f"[msg] MapGenerationRequest from {sender} room={room_id}")

    try:
        room = fetch_room_full(room_id)
    except Exception as exc:
        ctx.logger.error(f"failed to fetch room {room_id}: {exc}")
        return

    layout = room.get("layout_2d") or {}
    if not layout:
        ctx.logger.error(f"room {room_id} has no layout_2d — run Agent 1 first")
        return
    metadata = room.get("metadata") or {}

    ctx.logger.info(f"generating PDF for room {room_id}")
    try:
        pdf_path = generate_pdf(room_id, layout, metadata)
    except Exception as exc:
        ctx.logger.error(f"PDF generation failed: {exc}")
        patch_room(room_id, {"status": "error_pdf_generation", "pdf_error": str(exc)})
        return

    storage = "Cloudinary" if USE_CLOUDINARY else "local"
    ctx.logger.info(f"saving PDF ({storage}) for room {room_id}")
    try:
        pdf_url = upload_pdf(pdf_path, room_id)
    except Exception as exc:
        ctx.logger.error(f"PDF storage failed: {exc}")
        patch_room(room_id, {"status": "error_pdf_storage", "pdf_error": str(exc)})
        return

    patch_room(room_id, {"pdf_url": pdf_url, "status_map_done": True})
    ctx.logger.info(f"✓ PDF saved: {pdf_url}")


if __name__ == "__main__":
    print("═" * 60)
    print(" BrailleMap Map Generator (Agent 3)")
    print(f" Address       : {map_agent.address}")
    print(f" Port          : {AGENT_PORT_3}")
    print(f" Storage       : {'Cloudinary' if USE_CLOUDINARY else 'local → ' + OUTPUT_DIR}")
    print(f" Backend       : {BACKEND_URL}")
    print(f" Braille font  : {'AppleBraille ✓' if _braille_font_available else '✗ not found'}")
    print("═" * 60)
    map_agent.run()
