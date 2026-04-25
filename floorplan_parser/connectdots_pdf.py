from __future__ import annotations

"""
ConnectDots-style tactile PDF renderer.

This module is adapted from `ConnectDots/braillemap-agents/agent_map.py` but:
  - contains no uAgents/backends/cloudinary
  - exposes a single deterministic `generate_tactile_pdf(...)` function
"""

import math
from typing import Any, Dict, List, Tuple

from pybraille import convertText as to_braille  # type: ignore
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

# ── Register Braille font (macOS) ─────────────────────────────────────────────
_BRAILLE_FONT_AVAILABLE = False
_BRAILLE_FONT_NAME = "AppleBraille"
_BRAILLE_FONT_PATH = "/System/Library/Fonts/Apple Braille.ttf"
try:
    pdfmetrics.registerFont(TTFont(_BRAILLE_FONT_NAME, _BRAILLE_FONT_PATH))
    _BRAILLE_FONT_AVAILABLE = True
except Exception:
    _BRAILLE_FONT_AVAILABLE = False


DOT_SPACING_PTS = 5.0
WALL_DOT_RADIUS = 2.2
OBJECT_DOT_RADIUS = 1.5
WINDOW_DOT_RADIUS = 1.2
DOOR_GAP_RADIUS = 6.0


def _draw_dot_line(
    c: canvas.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    spacing: float,
    radius: float,
    *,
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
    cx: float,
    cy: float,
    w: float,
    h: float,
    spacing: float,
    radius: float,
) -> None:
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


def _draw_circle_outline(
    c: canvas.Canvas,
    cx: float,
    cy: float,
    r: float,
    *,
    num_dots: int = 16,
) -> None:
    for i in range(num_dots):
        angle = i * 2 * math.pi / num_dots
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        c.circle(x, y, 1.0, fill=1, stroke=0)


def _wall_endpoints(wall: Dict[str, Any]) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    cx, cy = float(wall["x"]), float(wall["y"])
    half = float(wall["width"]) / 2.0
    yaw = float(wall.get("rotation_y", 0.0))
    dx = half * math.cos(yaw)
    dy = -half * math.sin(yaw)
    return (cx - dx, cy - dy), (cx + dx, cy + dy)


def _sanitize_for_pybraille(text: str) -> str:
    """
    `pybraille` can raise/produce None for unsupported characters.
    Keep output stable by restricting to a conservative ASCII subset.
    """
    out = []
    for ch in (text or ""):
        o = ord(ch)
        if 32 <= o <= 126:
            out.append(ch)
        else:
            out.append(" ")
    return "".join(out).replace("—", "-")


def _draw_braille_text(c: canvas.Canvas, x: float, y: float, text: str, *, size: float = 20) -> float:
    safe = _sanitize_for_pybraille(text)
    try:
        braille = to_braille(safe)
    except Exception:
        braille = safe
    if _BRAILLE_FONT_AVAILABLE:
        c.setFont(_BRAILLE_FONT_NAME, size)
        c.drawString(x, y, braille)
        return c.stringWidth(braille, _BRAILLE_FONT_NAME, size)
    c.setFont("Helvetica", size * 0.6)
    c.drawString(x, y, braille)
    return c.stringWidth(braille, "Helvetica", size * 0.6)


def generate_tactile_pdf(
    *,
    output_pdf_path: str,
    layout: Dict[str, Any],
    metadata: Dict[str, Any],
    room_id: str = "room",
) -> str:
    """
    layout format (ConnectDots `layout_2d`):
      - room_width (m), room_depth (m)
      - walls: [{x,y,width,rotation_y}, ...]
      - doors: [{x,y,width,rotation_y,is_entrance?}, ...]
      - windows: [{x,y,width,rotation_y}, ...]
      - objects: [{index,category,x,y,width,depth,...}, ...]
      - entrance: optional
    """
    c = canvas.Canvas(output_pdf_path, pagesize=A4)
    page_w, page_h = A4
    margin = 40

    room_w = float(layout.get("room_width") or 0.0)
    room_d = float(layout.get("room_depth") or 0.0)
    walls: List[Dict[str, Any]] = list(layout.get("walls") or [])
    doors: List[Dict[str, Any]] = list(layout.get("doors") or [])
    windows: List[Dict[str, Any]] = list(layout.get("windows") or [])
    objects: List[Dict[str, Any]] = list(layout.get("objects") or [])
    entrance = layout.get("entrance") or {}

    room_label = metadata.get("room_name", metadata.get("space_name", "Unnamed Space"))
    building = metadata.get("building_name", "")

    # PAGE 1: map
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

    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(margin, page_h - margin, f"BrailleMap — {room_label}")
    _draw_braille_text(c, margin, page_h - margin - 24, room_label, size=22)
    c.setFont("Helvetica", 9)
    sub = f"{building}   •   {room_w:.1f}m × {room_d:.1f}m   •   ID: {room_id[:12]}"
    c.drawString(margin, page_h - margin - 46, sub)

    c.setStrokeColorRGB(0.8, 0.8, 0.8)
    c.setLineWidth(0.3)
    c.rect(origin_x - 4, origin_y - 4, map_w + 8, map_h + 8, stroke=1, fill=0)

    # walls
    c.setFillColorRGB(0, 0, 0)
    for wall in walls:
        (sx, sy), (ex, ey) = _wall_endpoints(wall)
        psx, psy = to_page(sx, sy)
        pex, pey = to_page(ex, ey)
        _draw_dot_line(c, psx, psy, pex, pey, DOT_SPACING_PTS, WALL_DOT_RADIUS)

    # windows
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
        pcx, pcy = to_page(cx, cy)
        _draw_circle_outline(c, pcx, pcy, 5, num_dots=10)

    # doors
    for door in doors:
        dx_page, dy_page = to_page(float(door["x"]), float(door["y"]))
        c.setFillColorRGB(1, 1, 1)
        c.circle(dx_page, dy_page, DOOR_GAP_RADIUS + 2, fill=1, stroke=0)
        c.setFillColorRGB(0, 0, 0)
        if door.get("is_entrance"):
            _draw_star(c, dx_page, dy_page, 10)
        else:
            _draw_triangle(c, dx_page, dy_page, 6)

    # objects
    c.setFillColorRGB(0, 0, 0)
    for obj in objects:
        ox, oy = to_page(float(obj["x"]), float(obj["y"]))
        ow = float(obj["width"]) * scale
        od = float(obj["depth"]) * scale
        _draw_dot_rect(c, ox, oy, ow, od, DOT_SPACING_PTS, OBJECT_DOT_RADIUS)
        num = int(obj.get("index", 0)) + 1
        c.setFont("Helvetica-Bold", 11)
        label_x = ox + max(ow / 2, 8) + 3
        label_y = oy - 4
        c.drawString(label_x, label_y, str(num))
        if _BRAILLE_FONT_AVAILABLE:
            c.setFont(_BRAILLE_FONT_NAME, 16)
            c.drawString(label_x + 12, label_y - 2, to_braille(str(num)))

    # scale bar (1m)
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

    # compact key
    key_y = bar_y + 20
    c.setFont("Helvetica", 8)
    c.setFillColorRGB(0, 0, 0)
    _draw_star(c, margin + 5, key_y + 2, 5)
    c.drawString(margin + 14, key_y - 1, "Entrance")
    _draw_triangle(c, margin + 70, key_y + 2, 4)
    c.drawString(margin + 78, key_y - 1, "Door")
    _draw_dot_line(c, margin + 110, key_y + 2, margin + 130, key_y + 2, 4, 1.0, dashed=True)
    c.drawString(margin + 134, key_y - 1, "Window")
    _draw_dot_rect(c, margin + 185, key_y + 2, 10, 6, 4, 1.0)
    c.drawString(margin + 194, key_y - 1, "Object (numbered)")

    # PAGE 2: legend
    c.showPage()
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(margin, page_h - margin, "Legend")
    _draw_braille_text(c, margin + 80, page_h - margin, "Legend", size=22)

    c.setFont("Helvetica", 10)
    sub2 = f"{room_label} — {len(objects)} object(s), {len(doors)} door(s), {len(windows)} window(s)"
    c.drawString(margin, page_h - margin - 20, sub2)

    y = page_h - margin - 55
    c.setFont("Helvetica-Bold", 13)
    c.drawString(margin, y, "Objects")
    _draw_braille_text(c, margin + 65, y, "objects", size=18)
    y -= 8

    for obj in objects:
        if y < margin + 80:
            c.showPage()
            c.setFillColorRGB(0, 0, 0)
            y = page_h - margin

        num = int(obj.get("index", 0)) + 1
        label = str(obj.get("category") or "unknown")
        pos = f"({float(obj['x']):.1f}m, {float(obj['y']):.1f}m)"
        size = f"{float(obj['width']):.1f} x {float(obj['depth']):.1f}m"

        y -= 16
        c.setFont("Helvetica-Bold", 11)
        c.drawString(margin, y, f"{num}.")
        c.setFont("Helvetica", 11)
        c.drawString(margin + 16, y, f"  {label}")

        y -= 13
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.3, 0.3, 0.3)
        c.drawString(margin + 16, y, f"Position: {pos}  •  Size: {size}")
        c.setFillColorRGB(0, 0, 0)

        y -= 22
        _draw_braille_text(c, margin + 16, y, f"{num} {label}", size=18)
        y -= 6

    if doors or windows:
        if y < margin + 100:
            c.showPage()
            c.setFillColorRGB(0, 0, 0)
            y = page_h - margin
        y -= 20
        c.setFont("Helvetica-Bold", 13)
        c.drawString(margin, y, "Openings")
        _draw_braille_text(c, margin + 75, y, "openings", size=18)

        for door in doors:
            y -= 20
            if y < margin + 40:
                c.showPage()
                c.setFillColorRGB(0, 0, 0)
                y = page_h - margin
            tag = "Entrance" if door.get("is_entrance") else "Door"
            pos = f"({float(door['x']):.1f}m, {float(door['y']):.1f}m)"
            width = f"{float(door['width']):.1f}m wide"
            if door.get("is_entrance"):
                _draw_star(c, margin + 6, y + 3, 5)
            else:
                _draw_triangle(c, margin + 6, y + 3, 4)
            c.setFont("Helvetica", 11)
            c.drawString(margin + 16, y, f"  {tag} at {pos}, {width}")
            y -= 22
            _draw_braille_text(c, margin + 16, y, f"{tag} {pos}", size=18)

        for win in windows:
            y -= 20
            if y < margin + 40:
                c.showPage()
                c.setFillColorRGB(0, 0, 0)
                y = page_h - margin
            pos = f"({float(win['x']):.1f}m, {float(win['y']):.1f}m)"
            width = f"{float(win['width']):.1f}m wide"
            _draw_circle_outline(c, margin + 6, y + 3, 4, num_dots=8)
            c.setFont("Helvetica", 11)
            c.drawString(margin + 16, y, f"  Window at {pos}, {width}")
            y -= 22
            _draw_braille_text(c, margin + 16, y, f"window {pos}", size=18)

    if entrance and entrance.get("kind") == "wall_midpoint":
        y -= 20
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(margin, y, "(No door detected — entrance approximated as midpoint of longest wall.)")

    c.save()
    return output_pdf_path

