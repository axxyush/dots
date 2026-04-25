#!/usr/bin/env python3
"""
Render a parsed floor-plan JSON → PNG.
No AI — pure Python / Pillow geometry.

Usage:
    python render_map.py --json result.json --output rendered.png
    python render_map.py --json result.json --image original.png --output comparison.png
"""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

# ── Colour palette ────────────────────────────────────────────────────────────

_FILL: dict[str, str | None] = {
    "store":              "#AED6F1",
    "restaurant":         "#FAD7A0",
    "restroom":           "#A9DFBF",
    "elevator":           "#F9E79F",
    "stairs":             "#D7BDE2",
    "door":               "#85929E",
    "corridor":           "#D0D3D4",
    "fire_exit":          "#F1948A",
    "fire_extinguisher":  "#EC7063",
    "fire_alarm":         "#EC7063",
    "rest_area":          "#FADBD8",
    "office":             "#D5D8DC",
    "cafe":               "#D5B8A5",
    "service_counter":    "#A2D9CE",
    "label":              None,
    "entrance":           "#82E0AA",
    # residential types
    "bedroom":            "#F9C6D0",
    "bathroom":           "#AED6F1",
    "living_room":        "#D5F5E3",
    "kitchen":            "#FDEBD0",
    "dining_room":        "#FEF9E7",
    "hallway":            "#F2F3F4",
    # institutional / school / public-building types
    "classroom":          "#FADBD8",
    "laboratory":         "#D6EAF8",
    "library":            "#FCF3CF",
    "auditorium":         "#E8DAEF",
    "gym":                "#F5CBA7",
    "music_room":         "#D2B4DE",
    "art_room":           "#F5B7B1",
    "staff_room":         "#D5F5E3",
    "reading_room":       "#FDEBD0",
    "computer_lab":       "#A9CCE3",
    "courtyard":          "#A9DFBF",
    "multimedia_room":    "#AED6F1",
    "general_office":     "#CACFD2",
    "utility":            "#D7DBDD",
    "lobby":              "#F2F4F4",
    "reception":          "#E5E8E8",
    "unknown":            "#E5E8E8",
}
_BORDER = "#2C3E50"
_CORRIDOR_LINE = "#A5AFBB"
_CORRIDOR_FILL = "#DCE0E3"
_LOW_CONF_COLOR = "#E74C3C"
_NAV_EDGE_COLOR = "#27AE60"
_NAV_NODE_COLOR = "#1E8449"
_GRID_COLOR = "#EAECEE"
_TEXT_DARK = "#1A252F"
_TEXT_MID = "#566573"
_BG = "#FFFFFF"
_CANVAS_BG = "#F7F9F9"

_LEGEND_NAMES = {
    "store": "Store",
    "restaurant": "Restaurant",
    "restroom": "Restroom",
    "office": "Office",
    "cafe": "Cafe",
    "rest_area": "Rest Area",
    "service_counter": "Service Counter",
    "entrance": "Entrance",
    "elevator": "Elevator",
    "stairs": "Stairs",
    "door": "Door",
    "fire_exit": "Fire Exit",
    "fire_extinguisher": "Fire Extinguisher",
    "fire_alarm": "Fire Alarm",
    "corridor": "Corridor",
    "bedroom": "Bedroom",
    "bathroom": "Bathroom",
    "living_room": "Living Room",
    "kitchen": "Kitchen",
    "dining_room": "Dining Room",
    "hallway": "Hallway",
    "classroom": "Classroom",
    "laboratory": "Laboratory",
    "library": "Library",
    "auditorium": "Auditorium",
    "gym": "Gymnasium",
    "music_room": "Music Room",
    "art_room": "Art Room",
    "staff_room": "Staff Room",
    "reading_room": "Reading Room",
    "computer_lab": "Computer Lab",
    "courtyard": "Courtyard",
    "multimedia_room": "Multimedia Room",
    "general_office": "General Office",
    "utility": "Utility",
    "lobby": "Lobby",
    "reception": "Reception",
    "unknown": "Unknown",
}


# ── Font helpers ──────────────────────────────────────────────────────────────

def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


# ── Coordinate conversion ─────────────────────────────────────────────────────

class Canvas:
    """Converts 0-100 normalised coords → pixel coords, respecting aspect ratio."""

    def __init__(self, fp: dict, target_long_side: int = 900, pad: int = 50) -> None:
        dims = fp.get("dimensions_px", {})
        src_w = dims.get("width", 1000) or 1000
        src_h = dims.get("height", 1000) or 1000
        aspect = src_h / src_w

        if aspect >= 1:            # portrait / square
            self.cw = int(target_long_side / aspect)
            self.ch = target_long_side
        else:                      # landscape
            self.cw = target_long_side
            self.ch = int(target_long_side * aspect)

        self.pad = pad

    def x(self, v: float) -> int:
        return self.pad + int(v / 100.0 * self.cw)

    def y(self, v: float) -> int:
        return self.pad + int(v / 100.0 * self.ch)

    def bbox(self, pos: dict) -> tuple[int, int, int, int]:
        return (
            self.x(pos["x"]),
            self.y(pos["y"]),
            self.x(pos["x"] + pos["w"]),
            self.y(pos["y"] + pos["h"]),
        )

    @property
    def total_w(self) -> int:
        return self.cw + 2 * self.pad

    @property
    def total_h(self) -> int:
        return self.ch + 2 * self.pad


# ── Drawing primitives ────────────────────────────────────────────────────────

def _text_bbox(draw: ImageDraw.ImageDraw, text: str, font: Any) -> tuple[int, int]:
    """Return (width, height) of text."""
    try:
        bb = draw.textbbox((0, 0), text, font=font)
        return bb[2] - bb[0], bb[3] - bb[1]
    except Exception:
        return len(text) * 6, 10


def _center_text(draw: ImageDraw.ImageDraw, bbox: tuple, text: str, font: Any,
                 color: str = _TEXT_DARK, min_box: int = 10) -> None:
    x0, y0, x1, y1 = bbox
    bw, bh = x1 - x0, y1 - y0
    if bw < min_box or bh < min_box or not text:
        return
    tw, th = _text_bbox(draw, text, font)
    while tw > bw - 4 and len(text) > 2:
        text = text[:-1]
        tw, _ = _text_bbox(draw, text + "…", font)
    if len(text) > 2:
        text = text if tw <= bw - 4 else text + "…"
    tx = x0 + (bw - tw) // 2
    ty = y0 + (bh - th) // 2
    draw.text((tx, ty), text, fill=color, font=font)


def _hatch(draw: ImageDraw.ImageDraw, bbox: tuple, color: str, spacing: int = 7) -> None:
    x0, y0, x1, y1 = bbox
    for offset in range(-(y1 - y0), (x1 - x0) + (y1 - y0), spacing):
        lx0 = x0 + offset
        ly0 = y0
        lx1 = x0 + offset + (y1 - y0)
        ly1 = y1
        # clip to bbox
        draw.line(
            [(max(x0, lx0), max(y0, ly0)), (min(x1, lx1), min(y1, ly1))],
            fill=color, width=1,
        )


def _elevator_x(draw: ImageDraw.ImageDraw, bbox: tuple, color: str = "#7D6608") -> None:
    x0, y0, x1, y1 = bbox
    m = 4
    draw.line([(x0 + m, y0 + m), (x1 - m, y1 - m)], fill=color, width=2)
    draw.line([(x1 - m, y0 + m), (x0 + m, y1 - m)], fill=color, width=2)


def _fire_symbol(draw: ImageDraw.ImageDraw, bbox: tuple, otype: str) -> None:
    x0, y0, x1, y1 = bbox
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    r = max(3, min(8, (min(x1 - x0, y1 - y0)) // 4))
    c, oc = "#E74C3C", "#922B21"
    if otype == "fire_extinguisher":
        draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=c, outline=oc, width=1)
    elif otype == "fire_alarm":
        draw.polygon([(cx, cy - r), (cx + r, cy + r), (cx - r, cy + r)], fill=c, outline=oc)
    elif otype == "fire_exit":
        # filled rectangle with white "EXIT" diagonal arrow suggestion
        draw.rectangle([(x0 + 1, y0 + 1), (x1 - 1, y1 - 1)], fill=c, outline=oc)
        draw.line([(cx - r, cy), (cx + r, cy)], fill="white", width=2)
        draw.line([(cx, cy - r), (cx, cy + r)], fill="white", width=2)


def _door_arc(draw: ImageDraw.ImageDraw, cv: Canvas, pos: dict, swing: str | None) -> None:
    x0, y0, x1, y1 = cv.bbox(pos)
    # door leaf along the longer axis
    bw, bh = x1 - x0, y1 - y0
    r = max(bw, bh)
    if bw >= bh:  # horizontal door
        if swing == "inward":
            draw.arc([(x0, y0 - r), (x0 + r, y0)], 0, 90, fill=_BORDER, width=1)
        else:
            draw.arc([(x0, y1), (x0 + r, y1 + r)], 270, 360, fill=_BORDER, width=1)
        draw.line([(x0, (y0 + y1) // 2), (x1, (y0 + y1) // 2)], fill=_BORDER, width=2)
    else:          # vertical door
        if swing == "inward":
            draw.arc([(x1, y0), (x1 + r, y0 + r)], 180, 270, fill=_BORDER, width=1)
        else:
            draw.arc([(x0 - r, y0), (x0, y0 + r)], 270, 360, fill=_BORDER, width=1)
        draw.line([((x0 + x1) // 2, y0), ((x0 + x1) // 2, y1)], fill=_BORDER, width=2)


def _low_conf_border(draw: ImageDraw.ImageDraw, bbox: tuple) -> None:
    x0, y0, x1, y1 = bbox
    dash, gap = 5, 3
    for i in range(x0, x1, dash + gap):
        draw.line([(i, y0), (min(i + dash, x1), y0)], fill=_LOW_CONF_COLOR, width=2)
        draw.line([(i, y1), (min(i + dash, x1), y1)], fill=_LOW_CONF_COLOR, width=2)
    for i in range(y0, y1, dash + gap):
        draw.line([(x0, i), (x0, min(i + dash, y1))], fill=_LOW_CONF_COLOR, width=2)
        draw.line([(x1, i), (x1, min(i + dash, y1))], fill=_LOW_CONF_COLOR, width=2)


def _draw_corridor(draw: ImageDraw.ImageDraw, cv: Canvas, corr: dict) -> None:
    pts = corr.get("centerline", [])
    if len(pts) < 2:
        return
    width_m = corr.get("width_m") or 3.5
    half_pct = max(1.5, min(7.0, width_m * 1.3))

    for i in range(len(pts) - 1):
        ax, ay = cv.x(pts[i]["x"]), cv.y(pts[i]["y"])
        bx, by = cv.x(pts[i + 1]["x"]), cv.y(pts[i + 1]["y"])
        length = math.hypot(bx - ax, by - ay)
        if length < 1:
            continue
        # width in pixels proportional to canvas
        half_w = max(4, int(half_pct / 100 * cv.cw))
        # draw as thick line (PIL line with width simulates corridor)
        draw.line([(ax, ay), (bx, by)], fill=_CORRIDOR_FILL, width=half_w * 2)
        draw.line([(ax, ay), (bx, by)], fill=_CORRIDOR_LINE, width=max(1, half_w * 2 - 2))


# ── Legend ─────────────────────────────────────────────────────────────────────

def _draw_legend(draw: ImageDraw.ImageDraw, fp: dict, lx: int, ly: int,
                 font_hd: Any, font_sm: Any,
                 show_nav: bool, show_low_conf: bool) -> None:
    draw.text((lx, ly), "Legend", fill=_TEXT_DARK, font=font_hd)
    ly += 22

    # count objects per type
    counts: dict[str, int] = {}
    for bucket in ("rooms", "doors", "verticals", "emergency", "labels"):
        for obj in fp.get(bucket, []):
            t = obj.get("type", "unknown")
            counts[t] = counts.get(t, 0) + 1
    for corr in fp.get("corridors", []):
        counts["corridor"] = counts.get("corridor", 0) + 1

    for ltype, lname in _LEGEND_NAMES.items():
        n = counts.get(ltype, 0)
        if n == 0:
            continue
        fill = _FILL.get(ltype)
        if fill:
            draw.rectangle([(lx, ly + 1), (lx + 14, ly + 13)], fill=fill, outline=_BORDER, width=1)
        else:
            draw.rectangle([(lx, ly + 1), (lx + 14, ly + 13)], outline=_BORDER, width=1)
        draw.text((lx + 18, ly + 1), f"{lname}  ×{n}", fill=_TEXT_DARK, font=font_sm)
        ly += 18

    ly += 8
    draw.line([(lx, ly), (lx + 220, ly)], fill=_GRID_COLOR, width=1)
    ly += 10

    # pipeline stats
    meta = fp.get("parse_metadata", {})
    stats = [
        f"Source:  {fp.get('source_image', '—')}",
        f"Grid:    {meta.get('tile_grid', '—')}  overlap {int(meta.get('overlap_pct', 0) * 100)}%",
        f"Raw objects:   {meta.get('total_objects_before_dedup', '—')}",
        f"After dedup:   {meta.get('total_objects_after_dedup', '—')}",
        f"Low-conf flags: {len(fp.get('low_confidence_flags', []))}",
        f"Nav nodes: {len(fp.get('navigation_graph', {}).get('nodes', []))}",
        f"Nav edges: {len(fp.get('navigation_graph', {}).get('edges', []))}",
    ]
    for stat in stats:
        draw.text((lx, ly), stat, fill=_TEXT_MID, font=font_sm)
        ly += 15

    ly += 6
    if show_low_conf:
        _low_conf_border(draw, (lx, ly, lx + 14, ly + 10))
        draw.text((lx + 18, ly), "Low confidence", fill=_LOW_CONF_COLOR, font=font_sm)
        ly += 18
    if show_nav:
        draw.line([(lx, ly + 5), (lx + 14, ly + 5)], fill=_NAV_EDGE_COLOR, width=2)
        draw.ellipse([(lx + 4, ly + 2), (lx + 10, ly + 8)], fill=_NAV_NODE_COLOR)
        draw.text((lx + 18, ly), "Nav graph", fill=_NAV_EDGE_COLOR, font=font_sm)


# ── Main render ────────────────────────────────────────────────────────────────

LEGEND_W = 250

def render_floor_plan(
    fp: dict,
    show_nav_graph: bool = True,
    show_grid: bool = True,
    show_low_conf: bool = True,
    target_long_side: int = 900,
    pad: int = 50,
) -> Image.Image:
    cv = Canvas(fp, target_long_side=target_long_side, pad=pad)
    total_w = cv.total_w + LEGEND_W
    total_h = cv.total_h + 40   # extra space for title

    img = Image.new("RGB", (total_w, total_h + pad), _BG)
    draw = ImageDraw.Draw(img)

    font_sm = _font(9)
    font_md = _font(11)
    font_hd = _font(13)
    font_title = _font(16)

    # ── Title ────────────────────────────────────────────────────────────────
    draw.text((pad, 10), f"Floor Plan · {fp.get('source_image', '')}", fill=_TEXT_DARK, font=font_title)

    # Shift all floor-plan drawing down by title height
    TITLE_H = 36

    def shifted_bbox(pos: dict) -> tuple[int, int, int, int]:
        x0, y0, x1, y1 = cv.bbox(pos)
        return x0, y0 + TITLE_H, x1, y1 + TITLE_H

    def sy(py: int) -> int:   # shift a raw pixel-y
        return py + TITLE_H

    # ── Canvas background + border ────────────────────────────────────────────
    draw.rectangle(
        [pad, TITLE_H + pad, pad + cv.cw, TITLE_H + pad + cv.ch],
        fill=_CANVAS_BG, outline=_BORDER, width=2,
    )

    # ── Grid ─────────────────────────────────────────────────────────────────
    if show_grid:
        for v in range(0, 101, 10):
            px, py = cv.x(v), sy(cv.y(v))
            draw.line([(px, sy(pad)), (px, sy(pad + cv.ch))], fill=_GRID_COLOR, width=1)
            draw.line([(pad, py), (pad + cv.cw, py)], fill=_GRID_COLOR, width=1)
            if 0 < v < 100:
                draw.text((px - 8, sy(pad) - 14), str(v), fill="#95A5A6", font=font_sm)
                draw.text((pad - 26, py - 5), str(v), fill="#95A5A6", font=font_sm)

    # ── Layer 1: corridors ────────────────────────────────────────────────────
    # We draw corridors directly with shifted coords
    for corr in fp.get("corridors", []):
        pts = corr.get("centerline", [])
        if len(pts) < 2:
            continue
        width_m = corr.get("width_m") or 3.5
        half_pct = max(1.5, min(7.0, width_m * 1.3))
        for i in range(len(pts) - 1):
            ax = cv.x(pts[i]["x"])
            ay = sy(cv.y(pts[i]["y"]))
            bx = cv.x(pts[i + 1]["x"])
            by = sy(cv.y(pts[i + 1]["y"]))
            half_w = max(4, int(half_pct / 100 * cv.cw))
            draw.line([(ax, ay), (bx, by)], fill=_CORRIDOR_FILL, width=half_w * 2)
            draw.line([(ax, ay), (bx, by)], fill=_CORRIDOR_LINE, width=max(1, half_w * 2 - 2))

    # ── Layer 2: rooms / verticals / emergency ────────────────────────────────
    low_conf_ids = {o.get("id") for o in fp.get("low_confidence_flags", [])}

    def draw_object(obj: dict) -> None:
        pos = obj.get("position", {})
        if not pos:
            return
        x0, y0, x1, y1 = shifted_bbox(pos)
        # clamp to canvas
        cx0 = max(pad, min(pad + cv.cw, x0))
        cy0 = max(sy(pad), min(sy(pad + cv.ch), y0))
        cx1 = max(pad, min(pad + cv.cw, x1))
        cy1 = max(sy(pad), min(sy(pad + cv.ch), y1))
        if cx1 <= cx0 or cy1 <= cy0:
            return

        bbox = (cx0, cy0, cx1, cy1)
        otype = obj.get("type", "label")
        fill = _FILL.get(otype, "#D5D8DC")

        if fill is None:
            return  # labels handled separately

        draw.rectangle(bbox, fill=fill, outline=_BORDER, width=1)

        if otype == "stairs":
            _hatch(draw, bbox, "#8E44AD")
            _center_text(draw, bbox, "STAIRS", font_sm)
        elif otype == "elevator":
            _elevator_x(draw, bbox)
            _center_text(draw, bbox, "ELEV", font_sm)
        elif otype in ("fire_extinguisher", "fire_alarm", "fire_exit"):
            _fire_symbol(draw, bbox, otype)
            if otype == "fire_exit":
                _center_text(draw, bbox, "EXIT", font_sm, color="white")
        else:
            label = obj.get("label") or otype.replace("_", " ").title()
            _center_text(draw, bbox, label, font_sm)

        # low-confidence dashed red border
        if show_low_conf and obj.get("id") in low_conf_ids:
            _low_conf_border(draw, bbox)

    # Draw larger rooms first so any nested smaller rooms (e.g. en-suite
    # bathroom inside a bedroom) paint ON TOP of their parent instead of
    # being hidden underneath it.
    def _area(obj: dict) -> float:
        p = obj.get("position") or {}
        return float(p.get("w", 0)) * float(p.get("h", 0))

    for bucket in ("rooms", "verticals", "emergency"):
        items = list(fp.get(bucket, []))
        if bucket == "rooms":
            items.sort(key=_area, reverse=True)
        for obj in items:
            draw_object(obj)

    # ── Layer 3: doors ────────────────────────────────────────────────────────
    for door in fp.get("doors", []):
        pos = door.get("position", {})
        if not pos:
            continue
        x0, y0, x1, y1 = shifted_bbox(pos)
        bw, bh = x1 - x0, y1 - y0
        r = max(bw, bh)
        swing = door.get("door_swing")
        if bw >= bh:
            mid_y = (y0 + y1) // 2
            draw.line([(x0, mid_y), (x1, mid_y)], fill=_BORDER, width=2)
            arc_box = (x0, mid_y - r, x0 + r, mid_y) if swing == "inward" else (x0, mid_y, x0 + r, mid_y + r)
            draw.arc(arc_box, 270 if swing == "inward" else 180, 360, fill=_BORDER, width=1)
        else:
            mid_x = (x0 + x1) // 2
            draw.line([(mid_x, y0), (mid_x, y1)], fill=_BORDER, width=2)
            arc_box = (mid_x, y0, mid_x + r, y0 + r) if swing == "inward" else (mid_x - r, y0, mid_x, y0 + r)
            draw.arc(arc_box, 180 if swing == "inward" else 270, 360, fill=_BORDER, width=1)

    # ── Layer 4: labels ───────────────────────────────────────────────────────
    for obj in fp.get("labels", []):
        pos = obj.get("position", {})
        if not pos:
            continue
        x0, y0, x1, y1 = shifted_bbox(pos)
        text = obj.get("label") or ""
        if text:
            draw.text((x0 + 1, y0 + 1), text, fill=_TEXT_DARK, font=font_sm)

    # ── Layer 5: navigation graph (optional) ──────────────────────────────────
    if show_nav_graph:
        nav = fp.get("navigation_graph", {})
        node_map = {n["id"]: n for n in nav.get("nodes", [])}
        for edge in nav.get("edges", []):
            n0 = node_map.get(edge.get("from_node"))
            n1 = node_map.get(edge.get("to_node"))
            if n0 and n1:
                ax = cv.x(n0["position"]["x"])
                ay = sy(cv.y(n0["position"]["y"]))
                bx = cv.x(n1["position"]["x"])
                by = sy(cv.y(n1["position"]["y"]))
                draw.line([(ax, ay), (bx, by)], fill=_NAV_EDGE_COLOR, width=1)
        for node in nav.get("nodes", []):
            nx = cv.x(node["position"]["x"])
            ny = sy(cv.y(node["position"]["y"]))
            draw.ellipse([(nx - 3, ny - 3), (nx + 3, ny + 3)], fill=_NAV_NODE_COLOR)

    # ── Legend ────────────────────────────────────────────────────────────────
    lx = pad + cv.cw + 20
    ly = TITLE_H + pad
    _draw_legend(draw, fp, lx, ly, font_hd, font_sm, show_nav_graph, show_low_conf)

    return img


# ── Side-by-side comparison ───────────────────────────────────────────────────

def render_comparison(
    original_path: Path,
    fp: dict,
    **render_kwargs: Any,
) -> Image.Image:
    """
    Original floor plan image  |  divider  |  rendered map  |  legend
    Axis tick marks on both panels aligned to 0-100 coordinate space.
    """
    orig = Image.open(original_path).convert("RGB")
    rendered = render_floor_plan(fp, **render_kwargs)

    # scale original to match rendered height
    r_h = rendered.height
    scale = r_h / orig.height
    orig_resized = orig.resize((int(orig.width * scale), r_h), Image.LANCZOS)

    DIVIDER = 8
    total_w = orig_resized.width + DIVIDER + rendered.width
    out = Image.new("RGB", (total_w, r_h), _BG)

    out.paste(orig_resized, (0, 0))

    # divider bar
    draw = ImageDraw.Draw(out)
    draw.rectangle([(orig_resized.width, 0), (orig_resized.width + DIVIDER, r_h)], fill="#BDC3C7")

    font = _font(13)
    draw.text((4, 4), "Original", fill=_TEXT_DARK, font=font)
    draw.text((orig_resized.width + DIVIDER + 4, 4), "Rendered from JSON", fill=_TEXT_DARK, font=font)

    out.paste(rendered, (orig_resized.width + DIVIDER, 0))
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Render floor plan JSON → PNG")
    ap.add_argument("--json",    required=True,  help="Parsed JSON file (result.json)")
    ap.add_argument("--image",   default=None,   help="Original floor plan image (for comparison mode)")
    ap.add_argument("--output",  default="rendered.png", help="Output PNG path")
    ap.add_argument("--no-nav",  action="store_true",    help="Hide navigation graph overlay")
    ap.add_argument("--no-grid", action="store_true",    help="Hide 10%% grid lines")
    ap.add_argument("--no-flags",action="store_true",    help="Hide low-confidence dashed borders")
    ap.add_argument("--size",    type=int, default=900,  help="Long-side target px (default 900)")
    args = ap.parse_args()

    data = json.loads(Path(args.json).read_text())
    fp = data.get("floor_plan") or data   # handle both wrapped and bare

    kwargs = dict(
        show_nav_graph=not args.no_nav,
        show_grid=not args.no_grid,
        show_low_conf=not args.no_flags,
        target_long_side=args.size,
    )

    if args.image:
        img = render_comparison(Path(args.image), fp, **kwargs)
    else:
        img = render_floor_plan(fp, **kwargs)

    out = Path(args.output)
    img.save(out, format="PNG")
    print(f"Saved → {out}  ({img.width}×{img.height} px)")

    # print object summary
    total = sum(
        len(fp.get(b, []))
        for b in ("rooms", "doors", "verticals", "emergency", "labels", "corridors")
    )
    print(f"\nObjects rendered:")
    for b in ("rooms", "doors", "verticals", "emergency", "labels", "corridors"):
        n = len(fp.get(b, []))
        if n:
            print(f"  {b:<16} {n}")
    print(f"  {'TOTAL':<16} {total}")
    print(f"  low-conf flags  {len(fp.get('low_confidence_flags', []))}")


if __name__ == "__main__":
    main()
