"""Render a parsed floor-plan JSON into a tactile map a blind person can
actually read by touch.

Two artefacts are produced side-by-side:

1. ``<stem>_tactile.png`` — high-contrast, print-ready image designed for
   swell paper or a tactile embosser.  It contains:
     * a thick outer building border
     * each room drawn as a rectangle filled with a **distinct hatching
       pattern** per functional category (8 patterns, chosen so they stay
       distinguishable under touch: lines, dots, cross-hatch, etc.)
     * **real Braille dots** (not a font!) for the room labels, sized at
       the ADA-standard dot geometry (1.5 mm dots, 2.5 mm intra-cell,
       6.2 mm inter-cell)
     * a Braille-labelled legend panel on the right-hand side
     * orientation marker (N arrow) + scale bar + short title bar

2. ``<stem>_tactile.txt`` — plain-text screen-reader companion: building
   summary, room list grouped by category with directional positions
   (top-left, centre, bottom-right, …), entrance list, and key features.

All drawing uses Pillow; no extra dependency beyond what the project
already ships with.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

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

# ── Tunables ─────────────────────────────────────────────────────────────────

#: Print resolution we design for. 300 DPI swell paper is standard.
DPI = 300

#: Output canvas — 11 × 8.5 in landscape letter, matches most embossers.
CANVAS_W_IN = 11.0
CANVAS_H_IN = 8.5

#: Reserve the right ~32 % of the canvas for the legend panel.
LEGEND_FRACTION = 0.30

#: Title bar height at top.
TITLE_H_IN = 0.9

#: Minimum room size (fraction of map area) to be drawn. Anything smaller
#: is merged into the "small detail" category (tiny dot pattern) so the
#: tactile reader isn't overwhelmed.
MIN_ROOM_AREA_FRACTION = 0.0015


# ── Category grouping ────────────────────────────────────────────────────────
# We fold the ~40 raw schema types into 8 touch-distinguishable categories.

@dataclass(frozen=True)
class Category:
    key: str
    label: str                  # legend text, English
    pattern: str                # one of: hatch_d, hatch_h, hatch_v, cross,
                                # dots, bricks, solid, outline, stair, elev
    outline_weight: int = 3     # border thickness in px (scaled later)


CATEGORIES: list[Category] = [
    Category("room",        "Room",            "hatch_d"),
    Category("social",      "Food / Rest",     "dots"),
    Category("restroom",    "Restroom",        "cross"),
    Category("corridor",    "Corridor",        "hatch_h"),
    Category("entrance",    "Entrance / Door", "solid", outline_weight=4),
    Category("stairs",      "Stairs",          "stair"),
    Category("elevator",    "Elevator",        "elev"),
    Category("emergency",   "Emergency Exit",  "bricks", outline_weight=4),
    Category("label",       "Landmark",        "outline"),
    Category("unknown",     "Unlabelled area", "outline"),
]

_TYPE_TO_CATEGORY: dict[str, str] = {
    # Ordinary occupiable rooms
    "store": "room", "office": "room", "classroom": "room", "laboratory": "room",
    "library": "room", "auditorium": "room", "gym": "room", "music_room": "room",
    "art_room": "room", "staff_room": "room", "reading_room": "room",
    "computer_lab": "room", "multimedia_room": "room", "general_office": "room",
    "utility": "room", "lobby": "room", "reception": "room",
    "bedroom": "room", "living_room": "room",
    "service_counter": "room",
    # Social / food
    "restaurant": "social", "cafe": "social", "dining_room": "social",
    "kitchen": "social", "rest_area": "social", "courtyard": "social",
    # Restrooms
    "restroom": "restroom", "bathroom": "restroom",
    # Corridors
    "corridor": "corridor", "hallway": "corridor",
    # Circulation
    "stairs": "stairs",
    "elevator": "elevator",
    # Doors / entrances
    "entrance": "entrance", "door": "entrance",
    # Emergency
    "fire_exit": "emergency",
    "fire_extinguisher": "emergency",
    "fire_alarm": "emergency",
    # Labels / unknown
    "label": "label",
    "unknown": "unknown",
}


def category_for(type_: str) -> Category:
    key = _TYPE_TO_CATEGORY.get(type_, "unknown")
    for cat in CATEGORIES:
        if cat.key == key:
            return cat
    return CATEGORIES[-1]


# ── Geometry helpers ─────────────────────────────────────────────────────────


def _mm_to_px(mm: float, dpi: int = DPI) -> int:
    return max(1, int(round(mm / 25.4 * dpi)))


@dataclass
class _Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2


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
    """Emboss a row of braille cells starting at (x, y).

    Returns the (width, height) of the rendered block in pixels so the
    caller can stack lines.
    """
    dot_r = _mm_to_px(DOT_DIAMETER_MM * scale / 2, dpi)
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

    width = len(cells) * inter
    height = 2 * intra + 2 * dot_r
    return width, height


def _braille_block_size(text: str, dpi: int = DPI, scale: float = 1.0) -> tuple[int, int]:
    cells = text_to_cells(text)
    inter = _mm_to_px(INTER_CELL_SPACING_MM * scale, dpi)
    intra = _mm_to_px(INTRA_CELL_SPACING_MM * scale, dpi)
    dot_r = _mm_to_px(DOT_DIAMETER_MM * scale / 2, dpi)
    return (len(cells) * inter, 2 * intra + 2 * dot_r)


def _draw_braille_multiline(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    max_cells_per_line: int,
    dpi: int = DPI,
    fill: str = "black",
    scale: float = 1.0,
) -> tuple[int, int]:
    """Word-wrap *text* to fit ``max_cells_per_line`` braille cells and
    emboss it starting at (x, y). Returns (width, total_height)."""
    cells_full = text_to_cells(text)
    # Naive wrap on original text's spaces is tricky because multi-cell
    # encodings (caps / number) interrupt word boundaries; instead we wrap
    # the cell stream on blank-cell separators.
    lines: list[list[BrailleCell]] = []
    current: list[BrailleCell] = []
    for c in cells_full:
        prospective = len(current) + 1
        if prospective > max_cells_per_line and c.mask == 0:
            lines.append(current)
            current = []
            continue
        if prospective > max_cells_per_line:
            # hard-break mid-word
            lines.append(current)
            current = [c]
            continue
        current.append(c)
    if current:
        lines.append(current)

    line_step_px = _mm_to_px(INTER_LINE_SPACING_MM * scale, dpi)
    max_w = 0
    cy = y
    for ln in lines:
        w, _ = _draw_braille_cells(draw, ln, x, cy, dpi, fill=fill, scale=scale)
        max_w = max(max_w, w)
        cy += line_step_px
    total_h = cy - y
    return max_w, total_h


# ── Hatching / pattern fills ─────────────────────────────────────────────────


def _render_pattern_tile(
    width: int,
    height: int,
    pattern: str,
    line_w: int,
    color: str = "black",
) -> Image.Image:
    """Render *pattern* onto a fresh ``(width, height)`` image.

    By drawing into a sub-image we get automatic clipping — hatch strokes
    can never leak outside the room's rectangle the way they would if we
    painted directly on the main canvas.
    """
    tile = Image.new("RGB", (max(1, width), max(1, height)), "white")
    d = ImageDraw.Draw(tile)
    step = max(10, line_w * 4)

    if pattern == "solid":
        d.rectangle((0, 0, width, height), fill=color)
        return tile
    if pattern == "outline":
        return tile

    if pattern == "hatch_d":
        # diagonal ╱ — generate enough offsets to cover both corners
        for offset in range(-height, width + step, step):
            d.line((offset, height, offset + height, 0), fill=color, width=line_w)
        return tile

    if pattern == "hatch_h":
        for yy in range(step // 2, height, step):
            d.line((0, yy, width, yy), fill=color, width=line_w)
        return tile

    if pattern == "hatch_v":
        for xx in range(step // 2, width, step):
            d.line((xx, 0, xx, height), fill=color, width=line_w)
        return tile

    if pattern == "cross":
        for yy in range(step // 2, height, step):
            d.line((0, yy, width, yy), fill=color, width=line_w)
        for xx in range(step // 2, width, step):
            d.line((xx, 0, xx, height), fill=color, width=line_w)
        return tile

    if pattern == "dots":
        r_dot = max(2, line_w)
        for row_i, yy in enumerate(range(step // 2, height, step)):
            x_off = (step // 2) if row_i % 2 else 0
            for xx in range(step // 2 + x_off, width, step):
                d.ellipse((xx - r_dot, yy - r_dot, xx + r_dot, yy + r_dot), fill=color)
        return tile

    if pattern == "bricks":
        brick_h = step
        for row_i, yy in enumerate(range(0, height, brick_h)):
            xoff = (step // 2) if row_i % 2 else 0
            for xx in range(xoff, width, step):
                d.rectangle(
                    (xx, yy, min(xx + step - 2, width - 1), min(yy + brick_h - 2, height - 1)),
                    outline=color, width=line_w,
                )
        return tile

    if pattern == "stair":
        rungs = max(3, min(9, height // max(12, step)))
        for i in range(rungs):
            yy = int((i + 1) * height / (rungs + 1))
            d.line((4, yy, width - 4, yy), fill=color, width=line_w + 1)
        return tile

    if pattern == "elev":
        inset = max(6, step // 2)
        x0, y0 = inset, inset
        x1, y1 = width - inset, height - inset
        d.rectangle((x0, y0, x1, y1), outline=color, width=line_w + 1)
        d.line((x0, y0, x1, y1), fill=color, width=line_w)
        d.line((x0, y1, x1, y0), fill=color, width=line_w)
        return tile

    return tile


def _fill_pattern(
    image: Image.Image,
    r: _Rect,
    pattern: str,
    line_w: int,
    color: str = "black",
) -> None:
    """Paste a pattern tile clipped to rect *r* on the main *image*."""
    if r.w <= 0 or r.h <= 0:
        return
    tile = _render_pattern_tile(r.w, r.h, pattern, line_w=line_w, color=color)
    image.paste(tile, (r.x, r.y))


# ── Text-description companion ───────────────────────────────────────────────


def _compass_for(x_pct: float, y_pct: float) -> str:
    """Describe a normalized (x,y) percent position in the 9-zone compass."""
    vertical = "top" if y_pct < 34 else "bottom" if y_pct > 66 else "centre"
    horizontal = "left" if x_pct < 34 else "right" if x_pct > 66 else "centre"
    if vertical == horizontal == "centre":
        return "centre"
    if vertical == "centre":
        return horizontal
    if horizontal == "centre":
        return f"{vertical} centre"
    return f"{vertical}-{horizontal}"


def _describe_room(room: dict) -> tuple[str, str]:
    """Return (short_display, long_line) for one room dict."""
    t = (room.get("type") or "unknown").replace("_", " ")
    label = (room.get("label") or "").strip()
    display = label or t.title()
    pos = room.get("position", {}) or {}
    where = _compass_for(float(pos.get("x", 50)) + float(pos.get("w", 0)) / 2,
                         float(pos.get("y", 50)) + float(pos.get("h", 0)) / 2)
    kind = t if t != "unknown" else "space"
    return display, f"- {display} — {kind}, located {where}."


def build_text_description(
    floor_plan: dict,
    title: str,
) -> str:
    """Produce a screen-reader-friendly summary of the parsed floor plan."""
    rooms: list[dict] = list(floor_plan.get("rooms", []) or [])
    doors: list[dict] = list(floor_plan.get("doors", []) or [])
    verticals: list[dict] = list(floor_plan.get("verticals", []) or [])
    emergency: list[dict] = list(floor_plan.get("emergency", []) or [])

    all_items: list[dict] = rooms + doors + verticals + emergency

    # Group by category.
    grouped: dict[str, list[dict]] = {}
    for obj in all_items:
        cat = category_for(obj.get("type", "unknown")).label
        grouped.setdefault(cat, []).append(obj)

    lines: list[str] = []
    lines.append(f"Tactile map: {title}")
    lines.append("=" * (len(title) + 13))
    lines.append("")
    lines.append(
        f"This building has {len(rooms)} rooms, "
        f"{len(doors)} door/entrance markers, "
        f"{sum(1 for v in verticals if (v.get('type') or '') == 'stairs')} stair, and "
        f"{sum(1 for v in verticals if (v.get('type') or '') == 'elevator')} elevator features "
        f"recognised from the source plan."
    )
    lines.append("")
    lines.append(
        "Orientation: the tactile map is printed with the top of the page "
        "as the NORTH side of the building. Rooms are described by the "
        "9-zone compass (top-left, centre, bottom-right, etc.)."
    )
    lines.append("")
    lines.append(
        "Legend patterns (as felt on the map): smooth diagonal lines = room; "
        "raised dots = food or rest area; cross-hatching = restroom; "
        "horizontal lines = corridor; solid filled = entrance or door; "
        "horizontal rungs = stairs; nested squares = elevator; "
        "brick texture with strong border = emergency exit."
    )
    lines.append("")

    for cat_name in (
        "Entrance / Door", "Emergency Exit", "Stairs", "Elevator",
        "Restroom", "Room", "Food / Rest", "Corridor",
        "Landmark", "Unlabelled area",
    ):
        items = grouped.get(cat_name, [])
        if not items:
            continue
        lines.append(f"{cat_name} ({len(items)}):")
        for obj in items[:40]:                # cap per-section to keep file short
            _, line = _describe_room(obj)
            lines.append("  " + line)
        if len(items) > 40:
            lines.append(f"  …plus {len(items) - 40} more.")
        lines.append("")

    lines.append(
        "How to use this map: print the accompanying PNG on swell paper "
        "(A4 or US Letter), heat it to raise the black ink into tactile "
        "ridges, then read with your fingertips. The title and all room "
        "labels are printed in Grade-1 English Braille. The rectangular "
        "legend panel on the right identifies each texture."
    )
    return "\n".join(lines)


# ── Main renderer ────────────────────────────────────────────────────────────


def _pct_to_map_px(pos: dict, map_rect: _Rect) -> _Rect:
    x = map_rect.x + int(float(pos.get("x", 0)) / 100 * map_rect.w)
    y = map_rect.y + int(float(pos.get("y", 0)) / 100 * map_rect.h)
    w = max(1, int(float(pos.get("w", 1)) / 100 * map_rect.w))
    h = max(1, int(float(pos.get("h", 1)) / 100 * map_rect.h))
    # Clamp so rooms never spill outside the map area.
    if x + w > map_rect.x2:
        w = map_rect.x2 - x
    if y + h > map_rect.y2:
        h = map_rect.y2 - y
    return _Rect(x=x, y=y, w=max(1, w), h=max(1, h))


def _short_label_for(room: dict) -> str:
    """Pick the shortest useful label for on-room braille. Prefers the
    user-visible label (e.g. "101", "Lobby") falling back to the type."""
    label = (room.get("label") or "").strip()
    if label:
        # Keep labels compact for on-room rendering; braille cells are big.
        if len(label) > 10:
            return label[:10]
        return label
    t = (room.get("type") or "unknown").replace("_", " ")
    abbr = {
        "restroom": "WC",
        "bathroom": "WC",
        "corridor": "hall",
        "hallway": "hall",
        "elevator": "lift",
        "stairs": "stair",
        "entrance": "in",
        "fire_exit": "exit",
        "classroom": "class",
        "laboratory": "lab",
        "library": "lib",
        "auditorium": "aud",
        "office": "off",
        "lobby": "lobby",
        "restaurant": "food",
        "cafe": "cafe",
    }.get(t, t)
    return abbr[:10]


def _load_label_font(size_px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in (
        "DejaVuSans-Bold.ttf",
        "Arial Bold.ttf",
        "Arial.ttf",
        "Helvetica.ttc",
    ):
        try:
            return ImageFont.truetype(name, size_px)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_title_bar(
    draw: ImageDraw.ImageDraw,
    title: str,
    x: int,
    y: int,
    w: int,
    h: int,
    dpi: int,
) -> None:
    draw.rectangle((x, y, x + w, y + h), outline="black", width=_mm_to_px(0.8, dpi))

    # Print title (plain text — sighted helpers use it) small at top.
    font_px = max(24, int(h * 0.30))
    font = _load_label_font(font_px)
    txt = f"Tactile Map — {title}"
    try:
        bbox = draw.textbbox((0, 0), txt, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        tw, th = len(txt) * font_px // 2, font_px
    draw.text((x + 20, y + 16), txt, fill="black", font=font)

    # Braille subtitle below — big, tactile, cross-building title.
    _draw_braille_cells(
        draw,
        text_to_cells("TACTILE MAP"),
        x + 20,
        y + 16 + th + 18,
        dpi=dpi,
        scale=1.0,
    )


def _draw_compass(
    draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, dpi: int,
) -> None:
    # Simple tactile "N" arrow: outline circle with arrowhead pointing up
    # and a braille "n" cell inside.
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline="black", width=_mm_to_px(1.0, dpi))
    draw.polygon(
        [(cx, cy - r + 4), (cx - r // 3, cy - r // 4), (cx + r // 3, cy - r // 4)],
        fill="black",
    )
    # Braille capital-N under the arrow
    _draw_braille_cells(
        draw,
        text_to_cells("N"),
        cx - _mm_to_px(INTER_CELL_SPACING_MM, dpi),
        cy + r // 6,
        dpi=dpi,
        scale=0.9,
    )


def _draw_scale_bar(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    map_w_px: int,
    dpi: int,
) -> None:
    bar_px = map_w_px // 5
    draw.line((x, y, x + bar_px, y), fill="black", width=_mm_to_px(1.2, dpi))
    # tick marks at ends
    tick = _mm_to_px(2.0, dpi)
    draw.line((x, y - tick, x, y + tick), fill="black", width=_mm_to_px(0.8, dpi))
    draw.line((x + bar_px, y - tick, x + bar_px, y + tick), fill="black", width=_mm_to_px(0.8, dpi))
    _draw_braille_cells(
        draw,
        text_to_cells("scale"),
        x,
        y + tick + _mm_to_px(2.0, dpi),
        dpi=dpi,
        scale=0.9,
    )


def _draw_legend_panel(
    image: Image.Image,
    x: int,
    y: int,
    w: int,
    h: int,
    categories_used: set[str],
    dpi: int,
) -> None:
    draw = ImageDraw.Draw(image)
    border_w = _mm_to_px(0.8, dpi)
    draw.rectangle((x, y, x + w, y + h), outline="black", width=border_w)

    title_txt = "LEGEND"
    font = _load_label_font(max(22, int(_mm_to_px(5.0, dpi))))
    draw.text((x + 20, y + 12), title_txt, fill="black", font=font)
    try:
        tb = draw.textbbox((0, 0), title_txt, font=font)
        title_h = tb[3] - tb[1]
    except Exception:
        title_h = font.size if hasattr(font, "size") else 24
    _draw_braille_cells(
        draw,
        text_to_cells("LEGEND"),
        x + 20,
        y + 12 + title_h + 10,
        dpi=dpi,
        scale=1.0,
    )

    inner_top = y + 12 + title_h + 10 + _mm_to_px(INTER_LINE_SPACING_MM * 1.2, dpi)
    swatch_w = _mm_to_px(16.0, dpi)
    swatch_h = _mm_to_px(10.0, dpi)
    row_gap = _mm_to_px(5.0, dpi)
    text_gap = _mm_to_px(3.0, dpi)
    line_w = _mm_to_px(0.4, dpi)

    # Braille cells of the labels need to fit inside `w`. Compute an
    # appropriate scale so the widest legend label still fits.
    text_left_of_braille = 20 + swatch_w + text_gap
    braille_budget_px = w - text_left_of_braille - 20       # right margin
    longest_label_cells = max(len(text_to_cells(c.label)) for c in CATEGORIES)
    cell_w_at_scale_1 = _mm_to_px(INTER_CELL_SPACING_MM, dpi)
    braille_scale = min(0.9, braille_budget_px / max(1, longest_label_cells * cell_w_at_scale_1))
    braille_scale = max(0.55, braille_scale)                # floor so it's still legible

    cy = inner_top
    for cat in CATEGORIES:
        if cat.key not in categories_used:
            continue
        sx = x + 18
        sy = cy
        _fill_pattern(
            image,
            _Rect(x=sx, y=sy, w=swatch_w, h=swatch_h),
            cat.pattern,
            line_w=line_w,
        )
        draw.rectangle(
            (sx, sy, sx + swatch_w, sy + swatch_h),
            outline="black", width=max(border_w, cat.outline_weight),
        )
        tx = sx + swatch_w + text_gap
        ty = sy + 2
        fnt = _load_label_font(max(16, int(_mm_to_px(3.4, dpi))))
        draw.text((tx, ty), cat.label, fill="black", font=fnt)
        try:
            tb = draw.textbbox((0, 0), cat.label, font=fnt)
            th = tb[3] - tb[1]
        except Exception:
            th = fnt.size if hasattr(fnt, "size") else 16
        _draw_braille_cells(
            draw,
            text_to_cells(cat.label),
            tx,
            ty + th + 4,
            dpi=dpi,
            scale=braille_scale,
        )
        cy += swatch_h + row_gap


def render_tactile_map(
    floor_plan: dict,
    output_png: Path,
    output_txt: Path,
    title: str | None = None,
    dpi: int = DPI,
) -> tuple[Path, Path, dict]:
    """Render the tactile map PNG + text companion.

    Returns ``(png_path, txt_path, info_dict)`` where ``info_dict``
    contains counts useful for upstream logging.
    """
    title = title or floor_plan.get("source_image") or "Floor Plan"

    W = int(CANVAS_W_IN * dpi)
    H = int(CANVAS_H_IN * dpi)
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)

    margin = _mm_to_px(8.0, dpi)
    title_h = int(TITLE_H_IN * dpi)

    # Top title bar
    _draw_title_bar(
        draw,
        title=title,
        x=margin,
        y=margin,
        w=W - 2 * margin,
        h=title_h,
        dpi=dpi,
    )

    body_y = margin + title_h + margin
    body_h = H - body_y - margin

    legend_w = int((W - 2 * margin) * LEGEND_FRACTION)
    map_w = (W - 2 * margin) - legend_w - margin
    map_rect = _Rect(x=margin, y=body_y, w=map_w, h=body_h)
    legend_rect = _Rect(x=margin + map_w + margin, y=body_y, w=legend_w, h=body_h)

    # Outer building border (thick) — high-contrast, will emboss as a wall.
    wall_w = _mm_to_px(2.4, dpi)
    draw.rectangle(
        (map_rect.x, map_rect.y, map_rect.x2, map_rect.y2),
        outline="black", width=wall_w,
    )

    # Collect every drawable object with its category.
    def collect() -> list[tuple[dict, Category]]:
        items: list[tuple[dict, Category]] = []
        for key in ("rooms", "doors", "verticals", "emergency", "labels"):
            for obj in floor_plan.get(key, []) or []:
                cat = category_for(obj.get("type", "unknown"))
                items.append((obj, cat))
        return items

    drawables = collect()

    # Filter pathologically-small rooms so the map stays readable by touch.
    total_area = map_rect.w * map_rect.h
    drawables = [
        (obj, cat) for (obj, cat) in drawables
        if _pct_to_map_px(obj.get("position", {}), map_rect).w
        * _pct_to_map_px(obj.get("position", {}), map_rect).h
        >= total_area * MIN_ROOM_AREA_FRACTION
    ]

    categories_used: set[str] = set()

    # Draw biggest first so smaller ones layer on top cleanly.
    drawables.sort(
        key=lambda pair: -(float(pair[0].get("position", {}).get("w", 0))
                           * float(pair[0].get("position", {}).get("h", 0)))
    )

    hatch_line_w = _mm_to_px(0.4, dpi)
    label_line_w = _mm_to_px(0.5, dpi)

    for obj, cat in drawables:
        rect = _pct_to_map_px(obj.get("position", {}), map_rect)
        # Clear interior first (white) so hatches from parent rooms don't
        # leak into child rooms.
        draw.rectangle((rect.x, rect.y, rect.x2, rect.y2), fill="white")
        _fill_pattern(img, rect, cat.pattern, line_w=hatch_line_w)
        draw.rectangle(
            (rect.x, rect.y, rect.x2, rect.y2),
            outline="black", width=max(label_line_w, cat.outline_weight),
        )
        categories_used.add(cat.key)

        # Only emboss a braille label when the room is big enough to
        # comfortably contain 3+ cells — otherwise the dots collide.
        min_cells_w = _mm_to_px(INTER_CELL_SPACING_MM * 3, dpi)
        min_cells_h = _mm_to_px(INTER_LINE_SPACING_MM, dpi)
        if rect.w >= min_cells_w and rect.h >= min_cells_h:
            label_text = _short_label_for(obj)
            if label_text:
                # Shrink scale if the label is too wide for the room.
                scale = 0.9
                for _ in range(3):
                    bw, bh = _braille_block_size(label_text, dpi=dpi, scale=scale)
                    if bw <= rect.w - _mm_to_px(2, dpi) and bh <= rect.h - _mm_to_px(2, dpi):
                        break
                    scale *= 0.8
                else:
                    bw, bh = _braille_block_size(label_text, dpi=dpi, scale=scale)

                lx = rect.cx - bw // 2
                ly = rect.cy - bh // 2
                # Carve a white "label island" so dots aren't occluded
                # by the hatching pattern.
                pad = _mm_to_px(1.5, dpi)
                draw.rectangle(
                    (lx - pad, ly - pad, lx + bw + pad, ly + bh + pad),
                    fill="white",
                )
                _draw_braille_cells(
                    draw,
                    text_to_cells(label_text),
                    lx, ly,
                    dpi=dpi,
                    scale=scale,
                )

    # Compass rose — top-left of map area, inside the wall.
    compass_r = _mm_to_px(9.0, dpi)
    _draw_compass(
        draw,
        cx=map_rect.x + compass_r + _mm_to_px(3, dpi),
        cy=map_rect.y + compass_r + _mm_to_px(3, dpi),
        r=compass_r,
        dpi=dpi,
    )

    # Scale bar — bottom-left, inside.
    _draw_scale_bar(
        draw,
        x=map_rect.x + _mm_to_px(4, dpi),
        y=map_rect.y2 - _mm_to_px(12, dpi),
        map_w_px=map_rect.w,
        dpi=dpi,
    )

    # Legend panel (on right) — always show the full legend so a tactile
    # reader can learn the patterns even for a simple plan.
    _draw_legend_panel(
        image=img,
        x=legend_rect.x,
        y=legend_rect.y,
        w=legend_rect.w,
        h=legend_rect.h,
        categories_used={c.key for c in CATEGORIES},
        dpi=dpi,
    )

    output_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_png, format="PNG", dpi=(dpi, dpi))

    # Text companion
    text = build_text_description(floor_plan, title=title)
    output_txt.parent.mkdir(parents=True, exist_ok=True)
    output_txt.write_text(text, encoding="utf-8")

    info = {
        "png_path": str(output_png),
        "txt_path": str(output_txt),
        "width_px": W,
        "height_px": H,
        "dpi": dpi,
        "drawables_rendered": len(drawables),
        "categories_used": sorted(categories_used),
    }
    return output_png, output_txt, info
