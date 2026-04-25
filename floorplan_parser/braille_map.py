"""Render a parsed floor-plan JSON as a clean, signage-style tactile map.

Output style follows real-world CNC-routed / laser-cut tactile floor-plan
signs (ADA-compliant building wayfinding):

    * **Dark navy** background with **warm cream** line-art (high-contrast,
      embosses cleanly when produced via CNC routing of two-tone plastic
      such as Rowmark ADA Alternative or laser-cut wood veneer);
    * **Sparse subtle patterns** per room category (small staggered dots,
      fine cross-hatch, light diagonal) — touch-distinguishable without
      visual clutter;
    * **Pictogram icons** inside rooms (bed, toilet, sink, stairs, lift,
      table, door arc) for instant orientation;
    * **Both plain text and Grade-1 English Braille** on every room
      label, matching the convention used by accessibility standards
      organisations (ADA / RNIB / Braille Authority of North America);
    * **Two-section legend** on the right — pattern swatches on top,
      icon glyphs on bottom — with each entry labelled in plain text +
      Braille just like the rooms;
    * **8-point compass rose** with N/E/S/W in Braille;
    * **Scale bar** along the bottom edge.

Two artefacts are produced per call:

    <stem>_tactile.png   high-DPI PNG of the map
    <stem>_tactile.txt   screen-reader text companion (compass-zone walkthrough)
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageDraw, ImageFont

from braille import (
    DOT_DIAMETER_MM,
    INTER_CELL_SPACING_MM,
    INTER_LINE_SPACING_MM,
    INTRA_CELL_SPACING_MM,
    BrailleCell,
    active_dots,
    cell_dot_offsets_mm,
    text_to_cells,
)

# ── Theme ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Theme:
    """Colour palette used by the renderer.

    The defaults match a CNC-routed two-tone tactile sign: dark navy
    substrate with warm cream relief.  Override for a print-on-swell-paper
    workflow with `Theme.PRINT` (black ink on white).
    """
    bg: str = "#1A2233"
    wall: str = "#D4B896"
    pattern: str = "#5A6B83"        # subtle texture (between bg and wall)
    text: str = "#F5E6D3"
    braille: str = "#F5E6D3"
    accent: str = "#E8D5B7"
    legend_box: str = "#D4B896"
    section_heading: str = "#F5E6D3"


THEME_DARK = Theme()
THEME_PRINT = Theme(
    bg="#FFFFFF",
    wall="#000000",
    pattern="#404040",
    text="#000000",
    braille="#000000",
    accent="#000000",
    legend_box="#000000",
    section_heading="#000000",
)


# ── Tunables ─────────────────────────────────────────────────────────────────

DPI = 300
CANVAS_W_IN = 11.0
CANVAS_H_IN = 8.5
LEGEND_FRACTION = 0.30
TITLE_H_IN = 0.75

#: Drop a room if its bbox overlaps an already-kept room by more than this
#: IoU fraction (CV outputs are noisy; the LLM is cleaner).  Fixes the
#: "dozens of stacked rectangles" look.
ROOM_IOU_DEDUP = 0.30

#: Also drop a room if more than this fraction of its area is already
#: covered by a previously-kept room (catches small rooms drawn entirely
#: inside a larger one — common CV artefact).
ROOM_CONTAIN_DEDUP = 0.70

#: Hard cap on the number of room rectangles drawn.  Tactile readers
#: can't trace through more than ~30 elements meaningfully.
MAX_ROOMS_TO_RENDER = 30

#: Filter pathologically tiny rooms (smaller than this fraction of the
#: map area).
MIN_ROOM_AREA_FRACTION = 0.0035

#: Filter pathologically *huge* rooms (larger than this fraction).  These
#: are almost always CV artefacts — a single bbox that swallows most of
#: the map and would dedup-occlude every real room behind it.
MAX_ROOM_AREA_FRACTION = 0.35


# ── Category grouping ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    pattern: str          # one of: dots, cross, diag, plain, solid, stair, elev
    icon: str | None = None   # icon name to draw inside (or None)
    outline_weight: int = 4


CATEGORIES: list[Category] = [
    Category("bedroom",   "Bedroom",         "dots",  icon="bed"),
    Category("dining",    "Dining / Food",   "cross", icon="table"),
    Category("restroom",  "Restroom",        "cross", icon="toilet"),
    Category("circulation", "Circulation",   "diag"),
    Category("room",      "Room",            "diag"),
    Category("stairs",    "Stairs",          "plain", icon="stairs"),
    Category("elevator",  "Elevator",        "plain", icon="elev"),
    Category("entrance",  "Entrance / Door", "plain", icon="door"),
    Category("emergency", "Emergency Exit",  "plain", icon="exit"),
    Category("landmark",  "Landmark",        "plain"),
]

_TYPE_TO_CATEGORY: dict[str, str] = {
    # Bedrooms / sleeping
    "bedroom": "bedroom",
    # Dining / food / social
    "restaurant": "dining", "cafe": "dining", "dining_room": "dining",
    "kitchen": "dining", "rest_area": "dining",
    # Restrooms
    "restroom": "restroom", "bathroom": "restroom",
    # Circulation
    "corridor": "circulation", "hallway": "circulation", "courtyard": "circulation",
    # Generic rooms
    "store": "room", "office": "room", "classroom": "room", "laboratory": "room",
    "library": "room", "auditorium": "room", "gym": "room", "music_room": "room",
    "art_room": "room", "staff_room": "room", "reading_room": "room",
    "computer_lab": "room", "multimedia_room": "room", "general_office": "room",
    "utility": "room", "lobby": "room", "reception": "room",
    "living_room": "room", "service_counter": "room", "label": "landmark",
    # Vertical circulation
    "stairs": "stairs",
    "elevator": "elevator",
    # Doors / entrances
    "entrance": "entrance", "door": "entrance",
    # Emergency
    "fire_exit": "emergency",
    "fire_extinguisher": "emergency",
    "fire_alarm": "emergency",
    # Unknown
    "unknown": "room",
}


def category_for(type_: str) -> Category:
    key = _TYPE_TO_CATEGORY.get(type_, "room")
    for cat in CATEGORIES:
        if cat.key == key:
            return cat
    return CATEGORIES[4]                    # default = "room"


# ── Geometry primitives ──────────────────────────────────────────────────────


def _mm_to_px(mm: float, dpi: int = DPI) -> int:
    return max(1, int(round(mm / 25.4 * dpi)))


@dataclass
class _Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def x2(self) -> int: return self.x + self.w
    @property
    def y2(self) -> int: return self.y + self.h
    @property
    def cx(self) -> int: return self.x + self.w // 2
    @property
    def cy(self) -> int: return self.y + self.h // 2
    @property
    def area(self) -> int: return self.w * self.h


def _intersection_area(a: _Rect, b: _Rect) -> int:
    ix0 = max(a.x, b.x); iy0 = max(a.y, b.y)
    ix1 = min(a.x2, b.x2); iy1 = min(a.y2, b.y2)
    if ix0 >= ix1 or iy0 >= iy1:
        return 0
    return (ix1 - ix0) * (iy1 - iy0)


def _iou(a: _Rect, b: _Rect) -> float:
    inter = _intersection_area(a, b)
    if not inter:
        return 0.0
    union = a.area + b.area - inter
    return inter / union if union else 0.0


def _containment_in(small: _Rect, big: _Rect) -> float:
    """Fraction of *small*'s area that lies inside *big*."""
    inter = _intersection_area(small, big)
    return inter / small.area if small.area else 0.0


def _pct_to_map_px(pos: dict, map_rect: _Rect) -> _Rect:
    x = map_rect.x + int(float(pos.get("x", 0)) / 100 * map_rect.w)
    y = map_rect.y + int(float(pos.get("y", 0)) / 100 * map_rect.h)
    w = max(1, int(float(pos.get("w", 1)) / 100 * map_rect.w))
    h = max(1, int(float(pos.get("h", 1)) / 100 * map_rect.h))
    if x + w > map_rect.x2: w = map_rect.x2 - x
    if y + h > map_rect.y2: h = map_rect.y2 - y
    return _Rect(x=x, y=y, w=max(1, w), h=max(1, h))


# ── Braille dot rendering ────────────────────────────────────────────────────


def _draw_braille_cells(
    draw: ImageDraw.ImageDraw,
    cells: list[BrailleCell],
    x: int,
    y: int,
    dpi: int = DPI,
    fill: str = "black",
    scale: float = 1.0,
) -> tuple[int, int]:
    dot_r = max(1, _mm_to_px(DOT_DIAMETER_MM * scale / 2, dpi))
    intra = _mm_to_px(INTRA_CELL_SPACING_MM * scale, dpi)
    inter = _mm_to_px(INTER_CELL_SPACING_MM * scale, dpi)
    offsets = cell_dot_offsets_mm()

    for idx, cell in enumerate(cells):
        ox = x + idx * inter
        for dot in active_dots(cell.mask):
            for n, (dx_mm, dy_mm) in offsets:
                if n != dot:
                    continue
                cx = ox + _mm_to_px(dx_mm * scale, dpi)
                cy = y + _mm_to_px(dy_mm * scale, dpi)
                draw.ellipse(
                    (cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r),
                    fill=fill,
                )
    return (len(cells) * inter, 2 * intra + 2 * dot_r)


def _braille_block_size(text: str, dpi: int = DPI, scale: float = 1.0) -> tuple[int, int]:
    cells = text_to_cells(text)
    inter = _mm_to_px(INTER_CELL_SPACING_MM * scale, dpi)
    intra = _mm_to_px(INTRA_CELL_SPACING_MM * scale, dpi)
    dot_r = max(1, _mm_to_px(DOT_DIAMETER_MM * scale / 2, dpi))
    return (len(cells) * inter, 2 * intra + 2 * dot_r)


# ── Pattern fills ────────────────────────────────────────────────────────────


def _render_pattern_tile(
    width: int,
    height: int,
    pattern: str,
    *,
    bg: str,
    fg: str,
    line_w: int,
    step: int,
) -> Image.Image:
    """Draw *pattern* into a fresh image (auto-clips to bounds)."""
    tile = Image.new("RGB", (max(1, width), max(1, height)), bg)
    d = ImageDraw.Draw(tile)

    if pattern in ("plain", "outline"):
        return tile

    if pattern == "solid":
        d.rectangle((0, 0, width, height), fill=fg)
        return tile

    if pattern == "dots":
        r_dot = max(2, line_w + 1)
        s = step
        for row, yy in enumerate(range(s // 2, height, s)):
            x_off = (s // 2) if row % 2 else 0
            for xx in range(s // 2 + x_off, width, s):
                d.ellipse((xx - r_dot, yy - r_dot, xx + r_dot, yy + r_dot), fill=fg)
        return tile

    if pattern == "cross":
        s = step
        for yy in range(s // 2, height, s):
            d.line((0, yy, width, yy), fill=fg, width=line_w)
        for xx in range(s // 2, width, s):
            d.line((xx, 0, xx, height), fill=fg, width=line_w)
        return tile

    if pattern == "diag":
        s = step
        for offset in range(-height, width + s, s):
            d.line((offset, height, offset + height, 0), fill=fg, width=line_w)
        return tile

    return tile


def _fill_pattern(
    image: Image.Image,
    r: _Rect,
    pattern: str,
    *,
    bg: str,
    fg: str,
    line_w: int,
    step: int,
) -> None:
    if r.w <= 0 or r.h <= 0:
        return
    tile = _render_pattern_tile(
        r.w, r.h, pattern, bg=bg, fg=fg, line_w=line_w, step=step,
    )
    image.paste(tile, (r.x, r.y))


# ── Icon library ─────────────────────────────────────────────────────────────


IconFn = Callable[[ImageDraw.ImageDraw, int, int, int, str], None]


def _icon_bed(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, color: str) -> None:
    w = size; h = int(size * 0.65)
    x, y = cx - w // 2, cy - h // 2
    line = max(2, size // 22)
    draw.rectangle((x, y, x + w, y + h), outline=color, width=line)
    # pillow at left
    pw, ph = int(w * 0.30), int(h * 0.55)
    draw.rectangle((x + line + 2, y + (h - ph) // 2, x + line + 2 + pw, y + (h + ph) // 2),
                   outline=color, width=max(1, line - 1))
    # blanket fold (diagonal)
    draw.line((x + line + 2 + pw + 4, y + h - line - 2, x + w - line, y + line + 2),
              fill=color, width=max(1, line - 1))


def _icon_toilet(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, color: str) -> None:
    line = max(2, size // 22)
    bowl_w = int(size * 0.55); bowl_h = int(size * 0.45)
    bx, by = cx - bowl_w // 2, cy - bowl_h // 2 + int(size * 0.06)
    draw.ellipse((bx, by, bx + bowl_w, by + bowl_h), outline=color, width=line)
    # tank
    tank_w = int(size * 0.55); tank_h = int(size * 0.30)
    tx = cx - tank_w // 2
    ty = by - tank_h + line
    draw.rectangle((tx, ty, tx + tank_w, ty + tank_h), outline=color, width=line)


def _icon_sink(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, color: str) -> None:
    line = max(2, size // 22)
    w = int(size * 0.75); h = int(size * 0.50)
    x, y = cx - w // 2, cy - h // 2 + int(size * 0.05)
    draw.rounded_rectangle((x, y, x + w, y + h), radius=max(4, size // 12),
                           outline=color, width=line)
    # faucet
    fw = max(3, size // 14)
    draw.line((cx, y - int(size * 0.24), cx, y + line), fill=color, width=fw)
    draw.line((cx - int(size * 0.16), y - int(size * 0.24),
               cx + int(size * 0.16), y - int(size * 0.24)), fill=color, width=fw)


def _icon_stairs(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, color: str) -> None:
    line = max(2, size // 22)
    steps = 5
    w = int(size * 0.85); h = int(size * 0.85)
    x, y = cx - w // 2, cy - h // 2
    sw = w / steps
    for i in range(steps):
        sx = x + int(i * sw)
        sy = y + int(h - (i + 1) * h / steps)
        sh = int(h / steps)
        draw.rectangle((sx, sy, sx + int(sw), sy + sh), outline=color, width=line)


def _icon_elev(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, color: str) -> None:
    line = max(2, size // 22)
    s = int(size * 0.80)
    x, y = cx - s // 2, cy - s // 2
    draw.rectangle((x, y, x + s, y + s), outline=color, width=line)
    # up + down arrows
    arrow_w = int(s * 0.20)
    # up arrow on left
    ax = x + int(s * 0.30); top = y + int(s * 0.18)
    draw.polygon(
        [(ax, top), (ax - arrow_w // 2, top + arrow_w),
         (ax + arrow_w // 2, top + arrow_w)],
        outline=color, width=line, fill=color,
    )
    draw.line((ax, top + arrow_w, ax, y + int(s * 0.82)), fill=color, width=line)
    # down arrow on right
    bx = x + int(s * 0.70); bot = y + int(s * 0.82)
    draw.polygon(
        [(bx, bot), (bx - arrow_w // 2, bot - arrow_w),
         (bx + arrow_w // 2, bot - arrow_w)],
        outline=color, width=line, fill=color,
    )
    draw.line((bx, bot - arrow_w, bx, y + int(s * 0.18)), fill=color, width=line)


def _icon_door(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, color: str) -> None:
    line = max(2, size // 18)
    s = int(size * 0.85)
    # door arc — corner at lower-left, opens to upper-right
    x, y = cx - s // 2, cy + s // 2
    # leaf
    draw.line((x, y, x + s, y - int(s * 0.05)), fill=color, width=line)
    # swing arc
    bbox = (x - s, y - s, x + s, y + s)
    draw.arc(bbox, start=270, end=360, fill=color, width=max(1, line - 1))


def _icon_table(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, color: str) -> None:
    line = max(2, size // 22)
    w = int(size * 0.70); h = int(size * 0.45)
    x, y = cx - w // 2, cy - h // 2 + int(size * 0.05)
    draw.rectangle((x, y, x + w, y + h), outline=color, width=line)
    # 2 chairs (top + bottom)
    cw = int(w * 0.25); ch = int(size * 0.13)
    draw.rectangle((cx - cw // 2, y - ch - 4, cx + cw // 2, y - 4),
                   outline=color, width=line)
    draw.rectangle((cx - cw // 2, y + h + 4, cx + cw // 2, y + h + ch + 4),
                   outline=color, width=line)


def _icon_exit(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, color: str) -> None:
    line = max(2, size // 22)
    s = int(size * 0.85)
    # door box
    x, y = cx - s // 2, cy - s // 2
    draw.rectangle((x, y, x + int(s * 0.55), y + s), outline=color, width=line)
    # arrow leaving the door
    ay = cy
    ax0 = x + int(s * 0.55) + 6
    ax1 = x + s
    draw.line((ax0, ay, ax1, ay), fill=color, width=line)
    head = max(8, int(s * 0.18))
    draw.polygon(
        [(ax1, ay), (ax1 - head, ay - head // 2), (ax1 - head, ay + head // 2)],
        fill=color, outline=color,
    )


_ICON_FNS: dict[str, IconFn] = {
    "bed": _icon_bed,
    "toilet": _icon_toilet,
    "sink": _icon_sink,
    "stairs": _icon_stairs,
    "elev": _icon_elev,
    "door": _icon_door,
    "table": _icon_table,
    "exit": _icon_exit,
}


# ── Room dedup / declutter ───────────────────────────────────────────────────


def _filter_drawables(
    floor_plan: dict, map_rect: _Rect,
) -> list[tuple[dict, Category, _Rect]]:
    """Collect rooms/doors/verticals/emergency, drop overlapping & tiny ones,
    cap to ``MAX_ROOMS_TO_RENDER`` of the largest unique rectangles."""
    map_area = map_rect.w * map_rect.h
    raw: list[tuple[dict, Category, _Rect]] = []
    for key in ("rooms", "doors", "verticals", "emergency", "labels"):
        for obj in floor_plan.get(key, []) or []:
            cat = category_for(obj.get("type", "unknown"))
            rect = _pct_to_map_px(obj.get("position", {}), map_rect)
            area = rect.w * rect.h
            if area < MIN_ROOM_AREA_FRACTION * map_area:
                continue
            # CV pipelines often emit a single mega-rectangle covering the
            # whole frame (mis-classified room) — skip it; the real rooms
            # live inside it.
            if area > MAX_ROOM_AREA_FRACTION * map_area:
                continue
            # Drop narrow unlabeled "generic" rectangles — they are almost
            # always CV column/stripe noise and only add visual clutter
            # without conveying meaningful wayfinding info.
            raw_type = (obj.get("type") or "").strip()
            raw_label = (obj.get("label") or "").strip()
            is_generic = raw_type in ("", "unknown", "utility") and not raw_label
            aspect = min(rect.w, rect.h) / max(rect.w, rect.h) if max(rect.w, rect.h) else 0
            if is_generic and aspect < 0.35:
                continue
            raw.append((obj, cat, rect))

    # Largest-first so overlaps prefer to keep the larger room.
    raw.sort(key=lambda t: -t[2].area)

    kept: list[tuple[dict, Category, _Rect]] = []
    for item in raw:
        _, _, rect = item
        skip = False
        for k in kept:
            kr = k[2]
            if _iou(rect, kr) > ROOM_IOU_DEDUP:
                skip = True; break
            # If this candidate is mostly inside an already-kept room, drop it.
            if _containment_in(rect, kr) > ROOM_CONTAIN_DEDUP:
                skip = True; break
        if skip:
            continue
        kept.append(item)
        if len(kept) >= MAX_ROOMS_TO_RENDER:
            break
    return kept


# ── Compass / scale / title ──────────────────────────────────────────────────


def _load_label_font(size_px: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
        ("Helvetica.ttc"),
        ("Arial Bold.ttf" if bold else "Arial.ttf"),
    )
    for name in candidates:
        try:
            return ImageFont.truetype(name, size_px)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, txt: str, font) -> tuple[int, int]:
    try:
        bb = draw.textbbox((0, 0), txt, font=font)
        return (bb[2] - bb[0], bb[3] - bb[1])
    except Exception:
        return (len(txt) * (font.size if hasattr(font, "size") else 16) // 2,
                font.size if hasattr(font, "size") else 16)


def _draw_compass(
    draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, theme: Theme, dpi: int,
) -> None:
    line = _mm_to_px(0.6, dpi)
    # outer ring
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=theme.wall, width=line)
    # 8-point star — N,S,E,W are major spikes; NE/NW/SE/SW are minor
    inner = int(r * 0.25)
    minor = int(r * 0.5)
    pts = []
    for i in range(8):
        ang = math.pi / 2 - i * math.pi / 4
        rr = r - line * 2 if i % 2 == 0 else minor
        pts.append((cx + int(rr * math.cos(ang)), cy - int(rr * math.sin(ang))))
        ang2 = ang - math.pi / 8
        pts.append((cx + int(inner * math.cos(ang2)), cy - int(inner * math.sin(ang2))))
    draw.polygon(pts, outline=theme.wall, width=line, fill=theme.wall)

    # filled north spike (so the up direction is unambiguous)
    north = [
        (cx, cy - r + line * 2),
        (cx - inner, cy - inner),
        (cx + inner, cy - inner),
    ]
    draw.polygon(north, fill=theme.bg, outline=theme.wall, width=line)

    # NE / SW directional letters
    font = _load_label_font(max(14, _mm_to_px(3.2, dpi)), bold=True)
    pad = _mm_to_px(2.0, dpi)
    labels = [("N", (cx, cy - r - pad - font.size // 2)),
              ("S", (cx, cy + r + pad + font.size // 2)),
              ("E", (cx + r + pad + font.size // 2, cy)),
              ("W", (cx - r - pad - font.size // 2, cy))]
    for txt, (lx, ly) in labels:
        tw, th = _text_size(draw, txt, font)
        draw.text((lx - tw // 2, ly - th // 2), txt, fill=theme.text, font=font)


def _draw_scale_bar(
    draw: ImageDraw.ImageDraw, x: int, y: int, map_w_px: int, theme: Theme, dpi: int,
) -> None:
    bar_px = map_w_px // 6
    line = _mm_to_px(0.8, dpi)
    tick = _mm_to_px(2.0, dpi)
    draw.line((x, y, x + bar_px, y), fill=theme.wall, width=line)
    draw.line((x, y - tick, x, y + tick), fill=theme.wall, width=line)
    draw.line((x + bar_px, y - tick, x + bar_px, y + tick), fill=theme.wall, width=line)
    font = _load_label_font(max(12, _mm_to_px(2.6, dpi)))
    draw.text((x, y + tick + 4), "scale", fill=theme.text, font=font)


def _draw_title_bar(
    draw: ImageDraw.ImageDraw,
    title: str,
    x: int, y: int, w: int, h: int,
    theme: Theme,
    dpi: int,
) -> None:
    font = _load_label_font(max(28, _mm_to_px(7.0, dpi)), bold=True)
    draw.text((x, y), title, fill=theme.text, font=font)
    _, th = _text_size(draw, title, font)
    _draw_braille_cells(
        draw, text_to_cells(title.upper()),
        x, y + th + _mm_to_px(2.0, dpi),
        dpi=dpi, fill=theme.braille, scale=0.85,
    )


# ── Legend panel ─────────────────────────────────────────────────────────────


def _draw_legend_panel(
    image: Image.Image,
    x: int, y: int, w: int, h: int,
    theme: Theme,
    dpi: int,
) -> None:
    draw = ImageDraw.Draw(image)
    border = _mm_to_px(0.7, dpi)
    pad = _mm_to_px(5.0, dpi)

    # Outer panel
    draw.rectangle((x, y, x + w, y + h), outline=theme.legend_box, width=border)

    # Heading
    head_font = _load_label_font(max(20, _mm_to_px(5.0, dpi)), bold=True)
    head = "VISUAL LEGEND"
    draw.text((x + pad, y + pad), head, fill=theme.section_heading, font=head_font)
    _, hh = _text_size(draw, head, head_font)
    _draw_braille_cells(
        draw, text_to_cells(head),
        x + pad, y + pad + hh + 6,
        dpi=dpi, fill=theme.braille, scale=0.75,
    )

    # Inner content area
    inner_y = y + pad + hh + 6 + _mm_to_px(INTER_LINE_SPACING_MM * 0.9, dpi)
    inner_h = (y + h) - inner_y - pad
    inner_x = x + pad
    inner_w = w - 2 * pad

    swatch_w = _mm_to_px(16.0, dpi)
    swatch_h = _mm_to_px(11.0, dpi)
    icon_box = _mm_to_px(14.0, dpi)
    row_gap = _mm_to_px(4.0, dpi)
    text_gap = _mm_to_px(3.0, dpi)
    label_font = _load_label_font(max(15, _mm_to_px(3.4, dpi)))

    # Section (a) — patterns
    sec_font = _load_label_font(max(14, _mm_to_px(3.0, dpi)), bold=True)
    pattern_cats = [c for c in CATEGORIES if c.icon is None
                    or c.key in ("bedroom", "dining", "restroom", "circulation", "room")]
    icon_cats = [c for c in CATEGORIES if c.icon is not None
                 and c.key not in ("bedroom", "dining", "restroom")]

    cy = inner_y
    draw.text((inner_x, cy), "(a)", fill=theme.text, font=sec_font)
    cy_pat_start = cy
    pat_text_x = inner_x + _mm_to_px(8.0, dpi)
    for cat in pattern_cats:
        sx = pat_text_x
        sy = cy
        # Swatch — same look as on-map rooms
        _fill_pattern(
            image, _Rect(sx, sy, swatch_w, swatch_h), cat.pattern,
            bg=theme.bg, fg=theme.pattern,
            line_w=_mm_to_px(0.35, dpi),
            step=_mm_to_px(2.6, dpi),
        )
        draw.rectangle((sx, sy, sx + swatch_w, sy + swatch_h),
                       outline=theme.wall, width=max(border, 2))
        tx = sx + swatch_w + text_gap
        draw.text((tx, sy + 2), cat.label, fill=theme.text, font=label_font)
        _, lh = _text_size(draw, cat.label, label_font)
        _draw_braille_cells(
            draw, text_to_cells(cat.label),
            tx, sy + 2 + lh + 4,
            dpi=dpi, fill=theme.braille, scale=0.7,
        )
        cy += swatch_h + row_gap

    # Section (b) — icons
    cy += _mm_to_px(2.0, dpi)
    draw.text((inner_x, cy), "(b)", fill=theme.text, font=sec_font)
    icn_text_x = inner_x + _mm_to_px(8.0, dpi)
    for cat in icon_cats:
        if cat.icon is None or cat.icon not in _ICON_FNS:
            continue
        sx = icn_text_x
        sy = cy
        # Icon glyph (no border)
        _ICON_FNS[cat.icon](
            draw, sx + icon_box // 2, sy + icon_box // 2, icon_box, theme.wall,
        )
        tx = sx + icon_box + text_gap + _mm_to_px(2.0, dpi)
        # Use specific labels for the icon section
        ilabel = {
            "stairs": "Stairs Icon", "elev": "Elevator Icon",
            "door": "Door Icon", "exit": "Emergency Exit",
        }.get(cat.icon, f"{cat.icon.title()} Icon")
        draw.text((tx, sy + 2), ilabel, fill=theme.text, font=label_font)
        _, lh = _text_size(draw, ilabel, label_font)
        _draw_braille_cells(
            draw, text_to_cells(ilabel),
            tx, sy + 2 + lh + 4,
            dpi=dpi, fill=theme.braille, scale=0.7,
        )
        cy += icon_box + row_gap


# ── Text companion ───────────────────────────────────────────────────────────


def _compass_for(x_pct: float, y_pct: float) -> str:
    vertical = "top" if y_pct < 34 else "bottom" if y_pct > 66 else "centre"
    horizontal = "left" if x_pct < 34 else "right" if x_pct > 66 else "centre"
    if vertical == horizontal == "centre": return "centre"
    if vertical == "centre": return horizontal
    if horizontal == "centre": return f"{vertical} centre"
    return f"{vertical}-{horizontal}"


def _describe_room(room: dict) -> str:
    t = (room.get("type") or "unknown").replace("_", " ")
    label = (room.get("label") or "").strip()
    display = label or t.title()
    pos = room.get("position", {}) or {}
    where = _compass_for(float(pos.get("x", 50)) + float(pos.get("w", 0)) / 2,
                         float(pos.get("y", 50)) + float(pos.get("h", 0)) / 2)
    kind = t if t != "unknown" else "space"
    return f"  - {display} — {kind}, located {where}."


def build_text_description(floor_plan: dict, title: str) -> str:
    rooms = list(floor_plan.get("rooms", []) or [])
    doors = list(floor_plan.get("doors", []) or [])
    verticals = list(floor_plan.get("verticals", []) or [])
    emergency = list(floor_plan.get("emergency", []) or [])
    all_items = rooms + doors + verticals + emergency

    grouped: dict[str, list[dict]] = {}
    for obj in all_items:
        grouped.setdefault(category_for(obj.get("type", "unknown")).label, []).append(obj)

    lines: list[str] = []
    lines.append(f"Tactile map: {title}")
    lines.append("=" * (len(title) + 13))
    lines.append("")
    lines.append(
        f"This building has {len(rooms)} rooms, {len(doors)} entrance markers, "
        f"{sum(1 for v in verticals if (v.get('type') or '') == 'stairs')} stair, and "
        f"{sum(1 for v in verticals if (v.get('type') or '') == 'elevator')} elevator features."
    )
    lines.append("")
    lines.append(
        "Orientation: the top of the page is NORTH. Rooms are described "
        "by the 9-zone compass (top-left, centre, bottom-right, etc.)."
    )
    lines.append("")
    lines.append(
        "Touch key — bedroom = staggered dots; dining/food = cross-hatch; "
        "restroom = cross-hatch + toilet icon; corridor / circulation = "
        "fine diagonal lines; rooms = diagonal lines; stairs / elevator / "
        "door / emergency exit are marked with raised pictogram icons."
    )
    lines.append("")

    order = (
        "Entrance / Door", "Emergency Exit", "Stairs", "Elevator",
        "Restroom", "Bedroom", "Dining / Food", "Room",
        "Circulation", "Landmark",
    )
    for name in order:
        items = grouped.get(name, [])
        if not items:
            continue
        lines.append(f"{name} ({len(items)}):")
        for obj in items[:40]:
            lines.append(_describe_room(obj))
        if len(items) > 40:
            lines.append(f"  …plus {len(items) - 40} more.")
        lines.append("")

    lines.append(
        "How to read: print the PNG and produce a tactile copy via swell "
        "paper, embosser, or two-tone CNC routing. The legend panel on "
        "the right identifies every pattern and pictogram. Plain text and "
        "Grade-1 English Braille appear together on the title and on every "
        "room and legend label."
    )
    return "\n".join(lines)


# ── Helpers for room labels ──────────────────────────────────────────────────


def _display_label(room: dict) -> str:
    label = (room.get("label") or "").strip()
    if label:
        return label
    t = (room.get("type") or "unknown").replace("_", " ")
    return t.title()


_LABEL_ABBREV = {
    "Bedroom": "Bdr", "Bathroom": "Bath", "Restroom": "WC",
    "Corridor": "Corr.", "Hallway": "Hall", "Kitchen": "Kitch.",
    "Dining Room": "Dining", "Restaurant": "Rest.", "Cafe": "Cafe",
    "Classroom": "Class", "Laboratory": "Lab", "Library": "Library",
    "Auditorium": "Aud.", "Office": "Office",
    "General Office": "Office", "Lobby": "Lobby", "Reception": "Recep.",
    "Reading Room": "Reading", "Computer Lab": "Comp.",
    "Music Room": "Music", "Art Room": "Art",
    "Multimedia Room": "Media", "Service Counter": "Counter",
    "Living Room": "Living", "Utility": "Util.",
    "Stairs": "Stairs", "Elevator": "Lift",
    "Entrance": "Entry", "Door": "Door",
    "Fire Exit": "Exit", "Fire Extinguisher": "Ext.",
    "Fire Alarm": "Alarm", "Rest Area": "Rest",
    "Courtyard": "Court", "Staff Room": "Staff",
    "Store": "Store", "Gym": "Gym",
}


def _abbrev_label(full: str) -> str:
    """Map verbose room labels to a short version that fits inside small
    rectangles ("Bedroom" → "Bdr")."""
    return _LABEL_ABBREV.get(full, full)


_HASHLIKE_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*[0-9])[A-Za-z0-9]{6,}$")
_TITLE_NOISE = {
    "tmp", "temp", "upload", "uploads", "img", "image", "file", "files",
    "scan", "scans", "page", "pages",
}


def _is_hashlike(token: str) -> bool:
    """Heuristic for tempfile/UUID/hash fragments: alphanumeric, ≥6 chars,
    contains both letters and digits *and* at least two case styles."""
    if not _HASHLIKE_RE.match(token):
        return False
    has_upper = any(c.isupper() for c in token)
    has_lower = any(c.islower() for c in token)
    return has_upper and has_lower


def _clean_title(raw: str) -> str:
    """Turn an arbitrary source string (URL, tempfile stem, UUID-suffixed
    upload, etc.) into a clean display title. Falls back to a generic
    title when nothing useful survives."""
    if not raw:
        return "Tactile Floor Plan"
    base = raw.split("?", 1)[0]            # drop URL query
    base = Path(base).stem                 # drop dir + extension

    parts = [p for p in re.split(r"[_\-\s.]+", base) if p]
    # Any hash-like fragment means this is almost certainly a tempfile —
    # discard the whole thing and use the generic title.
    if any(_is_hashlike(p) for p in parts):
        return "Tactile Floor Plan"
    parts = [p for p in parts if p.lower() not in _TITLE_NOISE]
    parts = [p for p in parts if any(c.isalpha() for c in p)]
    if not parts:
        return "Tactile Floor Plan"
    title = " ".join(parts).title()
    return title if len(title) >= 3 else "Tactile Floor Plan"


# ── Main renderer ────────────────────────────────────────────────────────────


def render_tactile_map(
    floor_plan: dict,
    output_png: Path,
    output_txt: Path,
    title: str | None = None,
    dpi: int = DPI,
    theme: Theme = THEME_DARK,
    canvas_w_in: float = CANVAS_W_IN,
    canvas_h_in: float = CANVAS_H_IN,
) -> tuple[Path, Path, dict]:
    raw_title = title or floor_plan.get("source_image") or "Floor Plan"
    nice_title = _clean_title(raw_title)

    W = int(canvas_w_in * dpi)
    H = int(canvas_h_in * dpi)
    img = Image.new("RGB", (W, H), theme.bg)
    draw = ImageDraw.Draw(img)

    margin = _mm_to_px(8.0, dpi)
    title_h = int(TITLE_H_IN * dpi)

    # ── Title bar (top-left, no enclosing box like the reference)
    _draw_title_bar(
        draw, nice_title,
        x=margin + _mm_to_px(28.0, dpi),     # leave room for compass
        y=margin,
        w=W - 2 * margin,
        h=title_h,
        theme=theme, dpi=dpi,
    )

    # ── Compass rose (top-left corner)
    compass_r = _mm_to_px(11.0, dpi)
    _draw_compass(
        draw,
        cx=margin + compass_r + _mm_to_px(2, dpi),
        cy=margin + compass_r + _mm_to_px(2, dpi),
        r=compass_r,
        theme=theme, dpi=dpi,
    )

    body_y = margin + title_h + margin
    body_h = H - body_y - margin
    legend_w = int((W - 2 * margin) * LEGEND_FRACTION)
    map_w = (W - 2 * margin) - legend_w - margin
    map_rect = _Rect(margin, body_y, map_w, body_h)
    legend_rect = _Rect(margin + map_w + margin, body_y, legend_w, body_h)

    # ── Outer building frame
    wall_w = _mm_to_px(2.0, dpi)
    draw.rectangle(
        (map_rect.x, map_rect.y, map_rect.x2, map_rect.y2),
        outline=theme.wall, width=wall_w,
    )

    # ── Room / object rendering
    drawables = _filter_drawables(floor_plan, map_rect)
    categories_used: set[str] = set()
    pat_step = _mm_to_px(3.2, dpi)
    pat_line = _mm_to_px(0.4, dpi)
    room_outline = _mm_to_px(0.6, dpi)

    inner_pad = _mm_to_px(2.8, dpi)   # visual breathing room per room

    # Text-size / braille-scale pairs, largest to smallest.  We always
    # try to show Braille alongside the plain text; Braille is the whole
    # point of the artefact.  The smallest scales are *visual-only* — at
    # ≤0.45 the dots are too small to read by touch and are decorative
    # for the on-screen render.  Production tactile output should use a
    # larger format so the larger pairs are picked.
    _SIZE_PAIRS: tuple[tuple[float, float], ...] = (
        (3.6, 0.55),
        (3.2, 0.48),
        (2.8, 0.42),
        (2.5, 0.36),
        (2.2, 0.32),
        (2.0, 0.28),
    )

    def _fit_label_pair(
        label: str, max_w: int, max_h: int,
    ) -> tuple[str, ImageFont.FreeTypeFont, int, int, float, int, int] | None:
        """Find the largest (text, braille) pair that fits the given box.
        Prefers the full label, falls back to its abbreviation.  Returns
        ``(text, font, tw, th, braille_scale, bw, bh)`` or None.  If
        Braille cannot be fit at any size, returns a text-only result
        with ``braille_scale == 0``."""
        label_spacer = _mm_to_px(1.2, dpi)
        candidates = [label]
        ab = _abbrev_label(label)
        if ab != label:
            candidates.append(ab)

        # First pass — require BOTH text and braille to fit.
        for txt in candidates:
            for tsize, bscale in _SIZE_PAIRS:
                f = _load_label_font(_mm_to_px(tsize, dpi), bold=True)
                tw, th = _text_size(draw, txt, f)
                bw, bh = _braille_block_size(txt, dpi=dpi, scale=bscale)
                if (tw <= max_w and bw <= max_w
                        and (th + label_spacer + bh) <= max_h):
                    return txt, f, tw, th, bscale, bw, bh

        # Second pass — fallback: text-only (smallest room where Braille
        # simply will not fit).
        for txt in candidates:
            for tsize, _ in _SIZE_PAIRS:
                f = _load_label_font(_mm_to_px(tsize, dpi), bold=True)
                tw, th = _text_size(draw, txt, f)
                if tw <= max_w and th <= max_h:
                    return txt, f, tw, th, 0.0, 0, 0
        return None

    for obj, cat, rect in drawables:
        # Carve the room interior to bg first so patterns from larger
        # parents can't leak.
        draw.rectangle((rect.x, rect.y, rect.x2, rect.y2), fill=theme.bg)
        _fill_pattern(
            img, rect, cat.pattern,
            bg=theme.bg, fg=theme.pattern,
            line_w=pat_line, step=pat_step,
        )
        draw.rectangle(
            (rect.x, rect.y, rect.x2, rect.y2),
            outline=theme.wall, width=max(room_outline, cat.outline_weight // 2),
        )
        categories_used.add(cat.key)

        usable_w = rect.w - 2 * inner_pad
        usable_h = rect.h - 2 * inner_pad
        spacer = _mm_to_px(1.2, dpi)

        # ── Icon (only when the room has comfortable room for it).
        #     Sized against the *raw* room dim so narrow shafts can still
        #     carry a small pictogram; capped at 18 mm so it never
        #     dominates the label.
        icon_size = 0
        if cat.icon and cat.icon in _ICON_FNS:
            min_dim = min(rect.w, rect.h)
            if min_dim >= _mm_to_px(11, dpi):
                icon_size = min(_mm_to_px(18, dpi),
                                max(_mm_to_px(8, dpi), int(min_dim * 0.45)))

        # ── Try to fit label + braille first, allowing for icon.
        full_label = _display_label(obj)
        fit = None
        if usable_w > 0 and usable_h > 0:
            # First attempt: with icon
            if icon_size:
                budget_h = usable_h - icon_size - spacer
                if budget_h > 0:
                    fit = _fit_label_pair(full_label, usable_w, budget_h)
            # If nothing fit and we had an icon, drop the icon for more
            # label real estate.
            if fit is None:
                icon_size = 0
                fit = _fit_label_pair(full_label, usable_w, usable_h)

        if fit is None and icon_size == 0:
            # Nothing to draw inside the room.
            continue

        # Compute block height.
        if fit:
            txt, font, tw, th, bscale, bw, bh = fit
            label_block_h = th + (spacer + bh if bscale > 0 else 0)
        else:
            txt = font = None  # type: ignore
            tw = th = bw = bh = 0
            bscale = 0.0
            label_block_h = 0

        block_h = label_block_h + (icon_size + spacer if icon_size and fit else icon_size)

        cur_y = rect.cy - block_h // 2
        cur_y = max(rect.y + inner_pad, cur_y)

        if icon_size:
            _ICON_FNS[cat.icon](draw, rect.cx,
                                cur_y + icon_size // 2,
                                icon_size, theme.wall)
            cur_y += icon_size + (spacer if fit else 0)

        if fit:
            tx = rect.cx - tw // 2
            draw.text((tx, cur_y), txt, fill=theme.text, font=font)
            cur_y += th
            if bscale > 0:
                cur_y += spacer
                bx = rect.cx - bw // 2
                _draw_braille_cells(
                    draw, text_to_cells(txt),
                    bx, cur_y,
                    dpi=dpi, fill=theme.braille, scale=bscale,
                )

    # ── Scale bar (lower-left of map area)
    _draw_scale_bar(
        draw,
        x=map_rect.x + _mm_to_px(6, dpi),
        y=map_rect.y2 - _mm_to_px(10, dpi),
        map_w_px=map_rect.w,
        theme=theme, dpi=dpi,
    )

    # ── Legend panel
    _draw_legend_panel(
        image=img,
        x=legend_rect.x, y=legend_rect.y,
        w=legend_rect.w, h=legend_rect.h,
        theme=theme, dpi=dpi,
    )

    # ── Save artefacts
    output_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_png, format="PNG", dpi=(dpi, dpi))
    text = build_text_description(floor_plan, title=nice_title)
    output_txt.parent.mkdir(parents=True, exist_ok=True)
    output_txt.write_text(text, encoding="utf-8")

    info = {
        "png_path": str(output_png),
        "txt_path": str(output_txt),
        "width_px": W, "height_px": H, "dpi": dpi,
        "drawables_rendered": len(drawables),
        "categories_used": sorted(categories_used),
        "title": nice_title,
    }
    return output_png, output_txt, info
