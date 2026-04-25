from __future__ import annotations
import io
from dataclasses import dataclass
from PIL import Image


@dataclass(frozen=True)
class TileSpec:
    row: int
    col: int
    # pixel bounds (inclusive start, exclusive end)
    px_x0: int
    px_y0: int
    px_x1: int
    px_y1: int
    img_w: int
    img_h: int

    @property
    def tile_id(self) -> str:
        return f"tile_{self.row}_{self.col}"

    # global normalized coordinate range this tile covers
    def global_range(self) -> tuple[float, float, float, float]:
        x0 = self.px_x0 / self.img_w * 100
        y0 = self.px_y0 / self.img_h * 100
        x1 = self.px_x1 / self.img_w * 100
        y1 = self.px_y1 / self.img_h * 100
        return x0, y0, x1, y1


@dataclass(frozen=True)
class SeamSpec:
    seam_id: str
    orientation: str  # "vertical" | "horizontal"
    px_x0: int
    px_y0: int
    px_x1: int
    px_y1: int
    img_w: int
    img_h: int

    def global_range(self) -> tuple[float, float, float, float]:
        x0 = self.px_x0 / self.img_w * 100
        y0 = self.px_y0 / self.img_h * 100
        x1 = self.px_x1 / self.img_w * 100
        y1 = self.px_y1 / self.img_h * 100
        return x0, y0, x1, y1


def auto_grid(img_w: int, img_h: int) -> tuple[int, int]:
    """
    Pick a sensible (cols, rows) based on image resolution.

    Over-tiling small images causes duplicate detections across overlapping
    tiles (the LLM reports the same room from two neighboring tiles).
    Under-tiling huge images costs recall on small objects.

    Heuristic megapixel bands:
       < 0.5 MP →  1×1   (single-pass is plenty for small images)
       < 1.5 MP →  2×2
       < 4.0 MP →  3×2 or 2×3 depending on aspect
       < 8.0 MP →  3×3
       < 15  MP →  4×3 or 3×4
       else     →  5×4 or 4×5
    """
    mp = (img_w * img_h) / 1_000_000
    wide = img_w >= img_h
    if mp < 0.5:
        return (1, 1)
    if mp < 1.5:
        return (2, 2)
    if mp < 4.0:
        return (3, 2) if wide else (2, 3)
    if mp < 8.0:
        return (3, 3)
    if mp < 15.0:
        return (4, 3) if wide else (3, 4)
    return (5, 4) if wide else (4, 5)


def compute_tiles(img_w: int, img_h: int, cols: int, rows: int, overlap: float) -> list[TileSpec]:
    """Return tile specs for a cols×rows grid with fractional overlap."""
    base_w = img_w / cols
    base_h = img_h / rows
    pad_x = int(base_w * overlap / 2)
    pad_y = int(base_h * overlap / 2)

    specs: list[TileSpec] = []
    for r in range(rows):
        for c in range(cols):
            x0 = max(0, int(c * base_w) - pad_x)
            y0 = max(0, int(r * base_h) - pad_y)
            x1 = min(img_w, int((c + 1) * base_w) + pad_x)
            y1 = min(img_h, int((r + 1) * base_h) + pad_y)
            specs.append(TileSpec(row=r, col=c, px_x0=x0, px_y0=y0, px_x1=x1, px_y1=y1, img_w=img_w, img_h=img_h))
    return specs


def compute_seams(img_w: int, img_h: int, cols: int, rows: int, seam_pct: float = 0.05) -> list[SeamSpec]:
    """Return thin strip specs centered on each internal tile boundary."""
    seams: list[SeamSpec] = []
    base_w = img_w / cols
    base_h = img_h / rows
    half_x = int(img_w * seam_pct / 2)
    half_y = int(img_h * seam_pct / 2)

    # vertical seams (between columns)
    for c in range(1, cols):
        cx = int(c * base_w)
        seams.append(SeamSpec(
            seam_id=f"vseam_{c}",
            orientation="vertical",
            px_x0=max(0, cx - half_x),
            px_y0=0,
            px_x1=min(img_w, cx + half_x),
            px_y1=img_h,
            img_w=img_w,
            img_h=img_h,
        ))

    # horizontal seams (between rows)
    for r in range(1, rows):
        cy = int(r * base_h)
        seams.append(SeamSpec(
            seam_id=f"hseam_{r}",
            orientation="horizontal",
            px_x0=0,
            px_y0=max(0, cy - half_y),
            px_x1=img_w,
            px_y1=min(img_h, cy + half_y),
            img_w=img_w,
            img_h=img_h,
        ))

    return seams


def tile_to_global_bbox(pos: dict, spec: "TileSpec | SeamSpec") -> dict:
    """
    Convert a tile-local bbox (0–100 within the tile) to global (0–100) coordinates.
    The LLM reports positions relative to the tile; Python does the global math.
    """
    gx0, gy0, gx1, gy1 = spec.global_range()
    rng_x = gx1 - gx0
    rng_y = gy1 - gy0
    return {
        "x": gx0 + (pos["x"] / 100.0) * rng_x,
        "y": gy0 + (pos["y"] / 100.0) * rng_y,
        "w": (pos["w"] / 100.0) * rng_x,
        "h": (pos["h"] / 100.0) * rng_y,
    }


def tile_to_global_point(x: float, y: float, spec: "TileSpec | SeamSpec") -> tuple[float, float]:
    gx0, gy0, gx1, gy1 = spec.global_range()
    return gx0 + (x / 100.0) * (gx1 - gx0), gy0 + (y / 100.0) * (gy1 - gy0)


def crop_to_bytes(img: Image.Image, x0: int, y0: int, x1: int, y1: int) -> bytes:
    cropped = img.crop((x0, y0, x1, y1))
    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    return buf.getvalue()


def tile_neighbor_info(spec: TileSpec, all_specs: list[TileSpec]) -> dict[str, str | None]:
    idx = {(s.row, s.col): s.tile_id for s in all_specs}
    return {
        "north": idx.get((spec.row - 1, spec.col)),
        "south": idx.get((spec.row + 1, spec.col)),
        "west": idx.get((spec.row, spec.col - 1)),
        "east": idx.get((spec.row, spec.col + 1)),
    }
