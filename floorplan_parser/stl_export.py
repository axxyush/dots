"""Export the tactile floor plan as a 3D-printable STL.

The default output is a square 18 cm × 18 cm × 8 mm slab consisting of:

    * a 5 mm flat **base plate**, and
    * 3 mm of **raised features** sitting on top — outer building outline,
      individual room outlines, room category patterns (dot grid /
      cross-hatch / diagonal), plain-text labels, Braille dots,
      pictogram icons, compass rose, scale bar, and the legend panel.

Internally we render the existing 2-D tactile map into a white-on-black
"heightmap" PNG (using the same `render_tactile_map` pipeline), max-pool
downsample it to 0.5 mm per pixel, then extrude every "on" pixel of the
binary mask into a 3 mm-tall column above the base plate.  The result is
a closed manifold mesh written as binary STL — no external 3-D libraries
needed; only NumPy + Pillow + the standard library.

Print recipe
------------
* FDM:  0.4 mm nozzle, 0.2 mm layers, 100 % infill, no supports needed
        (everything is a positive extrusion above a flat base).
* Resin: standard photopolymer settings; orient flat-side-down.

Tactile fidelity
----------------
ADA Braille dot spec is 1.5 mm diameter × 0.5 mm tall raised dome.  The
defaults here use 0.5 mm/px raster and 3 mm column height which slightly
over-emphasises every feature — fine for prototyping and for sighted
companion use; to produce strict Grade-1 ADA dots, scale the print to
~80 % of full size or override `feature_height_mm=0.6` and re-print.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
from PIL import Image

from braille_map import Theme, render_tactile_map

# ── Heightmap theme — every visible feature collapses to pure white ──────────


THEME_HEIGHTMAP = Theme(
    bg="#000000",
    wall="#FFFFFF",
    pattern="#FFFFFF",
    text="#FFFFFF",
    braille="#FFFFFF",
    accent="#FFFFFF",
    legend_box="#FFFFFF",
    section_heading="#FFFFFF",
)


# ── Numpy helpers ────────────────────────────────────────────────────────────


def _max_pool_downsample(arr: np.ndarray, target_h: int, target_w: int) -> np.ndarray:
    """Block-max downsample so that any raised pixel in the source survives.
    Trims source to an integer multiple of (ry, rx) cells before pooling."""
    src_h, src_w = arr.shape
    ry = max(1, src_h // target_h)
    rx = max(1, src_w // target_w)
    new_h = target_h * ry
    new_w = target_w * rx
    if new_h != src_h or new_w != src_w:
        arr = arr[:new_h, :new_w]
    return arr.reshape(target_h, ry, target_w, rx).max(axis=(1, 3))


# ── Geometry ─────────────────────────────────────────────────────────────────


def _box_triangles(
    x0: float, y0: float, z0: float,
    x1: float, y1: float, z1: float,
) -> list[list[tuple[float, float, float]]]:
    """Closed axis-aligned box → 12 triangles.  Vertex order is CCW from
    each face's outward normal so STL viewers shade the box correctly."""
    p0 = (x0, y0, z0); p1 = (x1, y0, z0); p2 = (x1, y1, z0); p3 = (x0, y1, z0)
    p4 = (x0, y0, z1); p5 = (x1, y0, z1); p6 = (x1, y1, z1); p7 = (x0, y1, z1)
    return [
        # Bottom face (-Z normal)
        [p0, p2, p1], [p0, p3, p2],
        # Top face (+Z normal)
        [p4, p5, p6], [p4, p6, p7],
        # -Y face (front)
        [p0, p1, p5], [p0, p5, p4],
        # +X face (right)
        [p1, p2, p6], [p1, p6, p5],
        # +Y face (back)
        [p2, p3, p7], [p2, p7, p6],
        # -X face (left)
        [p3, p0, p4], [p3, p4, p7],
    ]


def _build_extrusion_mesh(
    mask: np.ndarray,
    *,
    mm_per_px: float,
    base_h: float,
    feature_h: float,
) -> np.ndarray:
    """Construct a closed manifold mesh = base plate + extruded raised
    cells.  Side faces shared between adjacent raised cells are skipped
    so the interior of each contiguous region is hollow (saves
    triangles, prints identically).  Returns ``(N, 3, 3)`` float32 array.
    """
    if mask.dtype != np.bool_:
        mask = mask > 0
    H, W = mask.shape
    width_mm = W * mm_per_px
    height_mm = H * mm_per_px
    z_top = base_h + feature_h

    triangles: list[list[tuple[float, float, float]]] = []

    # Base plate
    triangles.extend(_box_triangles(0, 0, 0, width_mm, height_mm, base_h))

    # Pre-compute "is neighbour raised" lookups vectorised
    raised = mask
    js, is_ = np.nonzero(raised)

    for j, i in zip(js.tolist(), is_.tolist()):
        x_a = i * mm_per_px
        x_b = (i + 1) * mm_per_px
        # Image y is inverted relative to STL world y (image row 0 sits
        # at the *top* of the picture — i.e. the largest world-y value).
        y_a = (H - 1 - j) * mm_per_px
        y_b = (H - j) * mm_per_px

        # Top face — always emitted because it is the visible upper surface.
        triangles.append([(x_a, y_a, z_top), (x_b, y_a, z_top), (x_b, y_b, z_top)])
        triangles.append([(x_a, y_a, z_top), (x_b, y_b, z_top), (x_a, y_b, z_top)])

        # -X side
        if i == 0 or not raised[j, i - 1]:
            triangles.append([(x_a, y_a, base_h), (x_a, y_b, z_top), (x_a, y_b, base_h)])
            triangles.append([(x_a, y_a, base_h), (x_a, y_a, z_top), (x_a, y_b, z_top)])
        # +X side
        if i == W - 1 or not raised[j, i + 1]:
            triangles.append([(x_b, y_a, base_h), (x_b, y_b, base_h), (x_b, y_b, z_top)])
            triangles.append([(x_b, y_a, base_h), (x_b, y_b, z_top), (x_b, y_a, z_top)])
        # -Y side  (image row j+1 = below in image = lower world y)
        if j == H - 1 or not raised[j + 1, i]:
            triangles.append([(x_a, y_a, base_h), (x_b, y_a, base_h), (x_b, y_a, z_top)])
            triangles.append([(x_a, y_a, base_h), (x_b, y_a, z_top), (x_a, y_a, z_top)])
        # +Y side  (image row j-1 = above in image = higher world y)
        if j == 0 or not raised[j - 1, i]:
            triangles.append([(x_a, y_b, base_h), (x_b, y_b, z_top), (x_b, y_b, base_h)])
            triangles.append([(x_a, y_b, base_h), (x_a, y_b, z_top), (x_b, y_b, z_top)])

    return np.asarray(triangles, dtype=np.float32)


# ── Binary STL writer (vectorised) ───────────────────────────────────────────


_STL_RECORD_DTYPE = np.dtype([
    ("normal", "<f4", 3),
    ("vertices", "<f4", (3, 3)),
    ("attr", "<u2"),
])


def _binary_stl_bytes(triangles: np.ndarray) -> bytes:
    """Encode an ``(N, 3, 3)`` float32 triangle array as binary STL."""
    n = int(triangles.shape[0])
    v0 = triangles[:, 0, :]
    v1 = triangles[:, 1, :]
    v2 = triangles[:, 2, :]
    normals = np.cross(v1 - v0, v2 - v0)
    norm = np.linalg.norm(normals, axis=1, keepdims=True)
    safe = np.where(norm > 0, norm, 1.0)
    normals = normals / safe

    record = np.zeros(n, dtype=_STL_RECORD_DTYPE)
    record["normal"] = normals.astype(np.float32)
    record["vertices"] = triangles.astype(np.float32)

    header = b" " * 80
    count = struct.pack("<I", n)
    return header + count + record.tobytes()


# ── Public entry point ──────────────────────────────────────────────────────


def floor_plan_to_stl(
    floor_plan: dict,
    output_stl: Path,
    *,
    base_size_mm: float = 180.0,
    base_thickness_mm: float = 5.0,
    feature_height_mm: float = 3.0,
    mm_per_px: float = 0.5,
    title: str | None = None,
    dpi: int = 300,
    keep_heightmap: bool = False,
) -> dict:
    """Render a tactile floor plan and convert it to a 3-D-printable STL.

    Parameters
    ----------
    floor_plan : dict
        The parsed floor-plan structure (same shape as the renderer
        accepts — ``rooms``, ``doors``, ``verticals``, ``emergency``,
        ``labels``).
    output_stl : Path
        Destination ``.stl`` file path (created if missing).
    base_size_mm, base_thickness_mm, feature_height_mm : float
        Final print dimensions in millimetres.  Defaults match a
        standard ADA-style tactile sign at 18 cm × 18 cm × 8 mm
        (5 mm base + 3 mm features).
    mm_per_px : float
        Heightmap resolution in millimetres per pixel.  0.5 mm/px keeps
        the STL file under ~30 MB while still showing 1.5 mm Braille
        dots and 1 mm walls clearly.
    title : str, optional
        Override for the title bar; defaults to a cleaned-up source
        filename or ``"Tactile Floor Plan"``.
    dpi : int
        Render DPI for the intermediate heightmap PNG.  Higher = crisper
        intermediate but identical STL after max-pool downsampling.
    keep_heightmap : bool
        If ``True``, retain the intermediate heightmap PNG next to the
        STL (handy for debugging the threshold).

    Returns
    -------
    dict with ``stl_path``, ``triangle_count``, ``size_mm``,
    ``raised_cells``, etc.
    """
    output_stl = Path(output_stl)
    output_stl.parent.mkdir(parents=True, exist_ok=True)

    canvas_in = base_size_mm / 25.4
    tmp_png = output_stl.parent / (output_stl.stem + "_heightmap.png")
    tmp_txt = output_stl.parent / (output_stl.stem + "_heightmap.txt")

    # 1) Render a square white-on-black heightmap of the full tactile map.
    render_tactile_map(
        floor_plan,
        output_png=tmp_png,
        output_txt=tmp_txt,
        title=title,
        theme=THEME_HEIGHTMAP,
        dpi=dpi,
        canvas_w_in=canvas_in,
        canvas_h_in=canvas_in,
    )

    # 2) Downsample to mm_per_px with max-pool so no fine feature
    #    (Braille dot, hairline wall) gets washed out.
    img = Image.open(tmp_png).convert("L")
    arr = np.asarray(img)
    target = int(round(base_size_mm / mm_per_px))
    pooled = _max_pool_downsample(arr, target, target)
    mask = pooled > 64

    if not keep_heightmap:
        for p in (tmp_png, tmp_txt):
            try:
                p.unlink()
            except OSError:
                pass

    # 3) Extrude the mask to a closed manifold mesh.
    triangles = _build_extrusion_mesh(
        mask,
        mm_per_px=mm_per_px,
        base_h=base_thickness_mm,
        feature_h=feature_height_mm,
    )
    output_stl.write_bytes(_binary_stl_bytes(triangles))

    return {
        "stl_path": str(output_stl),
        "triangle_count": int(triangles.shape[0]),
        "size_mm": (base_size_mm, base_size_mm,
                    base_thickness_mm + feature_height_mm),
        "base_thickness_mm": base_thickness_mm,
        "feature_height_mm": feature_height_mm,
        "mm_per_px": mm_per_px,
        "mask_resolution_px": list(mask.shape),
        "raised_cells": int(mask.sum()),
        "file_size_bytes": output_stl.stat().st_size,
    }
