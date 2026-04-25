"""
OpenCV room extractor for floor plans.

Two algorithms are provided:

  extract_rooms_cv_v2 (default): Otsu threshold → remove text/icon noise →
  dilate walls to seal door gaps → connected components on free space →
  drop exterior component(s) → bboxes.  This is the "bread-and-butter"
  strategy for architectural plans.

  extract_rooms_cv (original): flood-fill from borders.  Kept for
  back-compatibility / ablation.

Both return list[RoomRegion] plus a dict of intermediate debug images so the
caller can save a visual overlay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np


@dataclass(frozen=True)
class RoomRegion:
    """A CV-extracted enclosed region in pixel and percent coordinates.

    `label_hint` is a canonical type (e.g. "laboratory") used to choose a
    render color. `label_text` is the free-form text printed inside the room
    on the source floor plan (e.g. "Multi-Media Room"). When available, the
    renderer displays `label_text` verbatim; `label_hint` only drives colour.
    """

    id: str
    bbox_px: dict[str, int]
    bbox_pct: dict[str, float]
    area_px: int
    source: str = "cv"
    label_hint: str | None = None
    label_text: str | None = None


# ── Geometry helpers ─────────────────────────────────────────────────────────


def _rect_iou(a: dict[str, int], b: dict[str, int]) -> float:
    ax0, ay0, aw, ah = a["x"], a["y"], a["w"], a["h"]
    bx0, by0, bw, bh = b["x"], b["y"], b["w"], b["h"]
    ax1, ay1 = ax0 + aw, ay0 + ah
    bx1, by1 = bx0 + bw, by0 + bh
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _rect_contains(outer: dict[str, int], inner: dict[str, int], eps: int = 2) -> bool:
    return (
        inner["x"] >= outer["x"] - eps
        and inner["y"] >= outer["y"] - eps
        and inner["x"] + inner["w"] <= outer["x"] + outer["w"] + eps
        and inner["y"] + inner["h"] <= outer["y"] + outer["h"] + eps
    )


def _rect_intersection_area(a: dict[str, int], b: dict[str, int]) -> int:
    ix0, iy0 = max(a["x"], b["x"]), max(a["y"], b["y"])
    ix1 = min(a["x"] + a["w"], b["x"] + b["w"])
    iy1 = min(a["y"] + a["h"], b["y"] + b["h"])
    return max(0, ix1 - ix0) * max(0, iy1 - iy0)


def _dedup_regions(
    regions: list[RoomRegion],
    iou_thresh: float = 0.30,
    halo_ratio_max: float = 3.0,
    overlap_frac_thresh: float = 0.60,
) -> list[RoomRegion]:
    """
    Greedy NMS-style dedup. Process smaller-first so genuinely-more-specific
    rooms (e.g. a bathroom) survive over loose "halo" detections from larger
    door-seal dilations.

    A candidate is dropped as a duplicate of a kept box when ANY of:
      * IoU ≥ iou_thresh — substantial overlap.
      * One fully contains the other AND the outer is ≤ halo_ratio_max × the
        inner. This kills halo duplicates but preserves legitimate nested
        rooms (e.g. a bathroom inside a bedroom — where the outer is typically
        > 3× the inner).
      * The intersection covers ≥ overlap_frac_thresh of the smaller box AND
        the outer is ≤ halo_ratio_max × the smaller. Catches partial-overlap
        halos where edges shifted by a few pixels so neither box strictly
        contains the other.
    """
    kept: list[RoomRegion] = []
    for r in sorted(regions, key=lambda x: x.area_px):
        duplicate = False
        for k in kept:
            if _rect_iou(r.bbox_px, k.bbox_px) >= iou_thresh:
                duplicate = True
                break

            smaller = min(r.area_px, k.area_px)
            larger = max(r.area_px, k.area_px)
            size_ratio = larger / max(1, smaller)

            if _rect_contains(r.bbox_px, k.bbox_px) and size_ratio <= halo_ratio_max:
                duplicate = True
                break
            if _rect_contains(k.bbox_px, r.bbox_px) and size_ratio <= halo_ratio_max:
                duplicate = True
                break

            inter = _rect_intersection_area(r.bbox_px, k.bbox_px)
            if smaller > 0 and inter / smaller >= overlap_frac_thresh and size_ratio <= halo_ratio_max:
                duplicate = True
                break

        if not duplicate:
            kept.append(r)
    return kept


def _bbox_pct(bbox_px: dict[str, int], img_w: int, img_h: int) -> dict[str, float]:
    return {
        "x": round(bbox_px["x"] / img_w * 100.0, 2),
        "y": round(bbox_px["y"] / img_h * 100.0, 2),
        "w": round(bbox_px["w"] / img_w * 100.0, 2),
        "h": round(bbox_px["h"] / img_h * 100.0, 2),
    }


# ── Lightweight type classifier (geometry-only) ─────────────────────────────


def classify_room_heuristic(bbox_pct: dict[str, float]) -> str:
    """
    Conservative geometry-only fallback. Used only when both OCR and the LLM
    fail to provide a type.

    We ONLY return confident structural guesses (very long narrow shapes are
    almost certainly hallways). For everything else we return "unknown" — it
    is better to show "Unknown" (renders as neutral gray) than to confidently
    mislabel a laboratory as a "restaurant".
    """
    w = float(bbox_pct["w"])
    h = float(bbox_pct["h"])
    aspect = max(w, h) / max(1e-6, min(w, h))
    area = w * h

    if aspect >= 4.0 and area <= 25.0:
        return "hallway"
    return "unknown"


# ── Original (simple) algorithm ──────────────────────────────────────────────


def extract_rooms_cv(
    image_bgr: np.ndarray,
    *,
    threshold: int = 180,
    close_kernel: int = 3,
    close_iters: int = 3,
    min_area_frac: float = 0.002,
    sample_step: int = 8,
) -> tuple[list[RoomRegion], dict[str, np.ndarray]]:
    """Border flood-fill approach (kept for back-compat)."""
    import cv2

    h, w = image_bgr.shape[:2]
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)

    _, walls = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY_INV)

    kernel = np.ones((close_kernel, close_kernel), np.uint8)
    walls_closed = cv2.morphologyEx(walls, cv2.MORPH_CLOSE, kernel, iterations=close_iters)

    space = cv2.bitwise_not(walls_closed)
    outside = space.copy()
    flood_mask = np.zeros((h + 2, w + 2), np.uint8)

    seeds: list[tuple[int, int]] = []
    for x in range(0, w, max(1, sample_step)):
        seeds.append((x, 0))
        seeds.append((x, h - 1))
    for y in range(0, h, max(1, sample_step)):
        seeds.append((0, y))
        seeds.append((w - 1, y))

    for sx, sy in seeds:
        if outside[sy, sx] == 255:
            cv2.floodFill(outside, flood_mask, (sx, sy), 0)

    enclosed = (outside == 255).astype(np.uint8) * 255
    num, labels = cv2.connectedComponents(enclosed)

    regions: list[RoomRegion] = []
    min_area_px = int(w * h * float(min_area_frac))

    for lab in range(1, num):
        ys, xs = np.where(labels == lab)
        if xs.size == 0:
            continue
        x0, x1 = int(xs.min()), int(xs.max())
        y0, y1 = int(ys.min()), int(ys.max())
        bw = max(1, x1 - x0 + 1)
        bh = max(1, y1 - y0 + 1)
        area_px = int(xs.size)
        if area_px < min_area_px:
            continue
        bbox_px = {"x": x0, "y": y0, "w": bw, "h": bh}
        regions.append(
            RoomRegion(
                id=f"cv_room_{len(regions)}",
                bbox_px=bbox_px,
                bbox_pct=_bbox_pct(bbox_px, w, h),
                area_px=area_px,
            )
        )

    regions = _dedup_regions(regions)
    debug = {
        "gray": gray,
        "walls": walls,
        "walls_closed": walls_closed,
        "space": space,
        "enclosed": enclosed,
    }
    return regions, debug


# ── Improved algorithm ──────────────────────────────────────────────────────


def extract_rooms_cv_v2(
    image_bgr: np.ndarray,
    *,
    threshold: int | None = None,      # None = Otsu
    door_seal_px: int = 9,             # dilate walls to seal door gaps (px)
    min_wall_component_px: int = 20,   # drop tiny dark blobs (text, icons)
    min_area_frac: float = 0.0015,
    max_area_frac: float = 0.45,
    border_exterior_span: float = 0.6, # component is "exterior" if bbox spans ≥ this frac of each axis
) -> tuple[list[RoomRegion], dict[str, np.ndarray]]:
    """
    Robust CV extractor for architectural floor plans.

    Steps:
      1. Otsu threshold (unless `threshold` override) → wall mask.
      2. Drop tiny connected components from walls → removes text/icon noise.
      3. Dilate walls by `door_seal_px` to seal door openings and thin gaps.
      4. Free space = inverse of sealed walls.
      5. Connected components on free space (4-connectivity).
      6. Drop exterior components (bbox spans full image on both axes).
      7. Drop too-small / too-big components.
      8. Expand each room bbox by `door_seal_px//2` to undo dilation halo,
         clamp to image bounds, then deduplicate.

    Returns (regions, debug_images).
    """
    import cv2

    h, w = image_bgr.shape[:2]
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)

    # 1) wall mask
    if threshold is None:
        _, walls = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    else:
        _, walls = cv2.threshold(gray, int(threshold), 255, cv2.THRESH_BINARY_INV)

    # 2) strip tiny wall blobs (text, icons, arrows) — they survive thresholding
    #    but aren't structural.
    num_w, lbl_w, stats_w, _ = cv2.connectedComponentsWithStats(walls, connectivity=8)
    clean = np.zeros_like(walls)
    for i in range(1, num_w):
        if stats_w[i, cv2.CC_STAT_AREA] >= int(min_wall_component_px):
            clean[lbl_w == i] = 255
    walls_clean = clean

    # 3) seal door gaps — dilation closes ~door_seal_px openings so an adjacent
    #    room no longer leaks into the outside / corridor.
    seal_k = max(1, int(door_seal_px))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (seal_k, seal_k))
    walls_sealed = cv2.dilate(walls_clean, kernel, iterations=1)

    # 4) free space
    space = cv2.bitwise_not(walls_sealed)

    # 5) connected components (4-connectivity is stricter — safer for rooms).
    num, lbl, stats, cents = cv2.connectedComponentsWithStats(space, connectivity=4)

    # 6) detect exterior component(s) — those whose bbox nearly spans the image
    exterior_ids: set[int] = set()
    span_req_x = int(border_exterior_span * w)
    span_req_y = int(border_exterior_span * h)
    for i in range(1, num):
        cw_i = int(stats[i, cv2.CC_STAT_WIDTH])
        ch_i = int(stats[i, cv2.CC_STAT_HEIGHT])
        if cw_i >= span_req_x and ch_i >= span_req_y:
            # spans a large portion of both axes → treat as exterior background
            exterior_ids.add(i)

    min_area_px = int(w * h * float(min_area_frac))
    max_area_px = int(w * h * float(max_area_frac))

    shrink = seal_k // 2   # half the dilation halo to restore true room bbox

    regions: list[RoomRegion] = []
    for i in range(1, num):
        if i in exterior_ids:
            continue
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < min_area_px or area > max_area_px:
            continue

        x = int(stats[i, cv2.CC_STAT_LEFT])
        y = int(stats[i, cv2.CC_STAT_TOP])
        cw_i = int(stats[i, cv2.CC_STAT_WIDTH])
        ch_i = int(stats[i, cv2.CC_STAT_HEIGHT])

        # expand bbox back to approximate the true room size
        x0 = max(0, x - shrink)
        y0 = max(0, y - shrink)
        x1 = min(w, x + cw_i + shrink)
        y1 = min(h, y + ch_i + shrink)
        bw_i, bh_i = x1 - x0, y1 - y0
        if bw_i <= 2 or bh_i <= 2:
            continue

        bbox_px = {"x": x0, "y": y0, "w": bw_i, "h": bh_i}
        regions.append(
            RoomRegion(
                id=f"cv_room_{len(regions)}",
                bbox_px=bbox_px,
                bbox_pct=_bbox_pct(bbox_px, w, h),
                area_px=area,
                source="cv",
            )
        )

    regions = _dedup_regions(regions)

    debug = {
        "gray": gray,
        "walls": walls,
        "walls_clean": walls_clean,
        "walls_sealed": walls_sealed,
        "space": space,
    }
    return regions, debug


# ── Schema-compat conversion ────────────────────────────────────────────────


_SCHEMA_TYPES = frozenset({
    "store", "restaurant", "restroom", "elevator", "stairs", "door",
    "corridor", "fire_exit", "fire_extinguisher", "fire_alarm", "rest_area",
    "office", "cafe", "service_counter", "label", "entrance",
    "bedroom", "bathroom", "living_room", "kitchen", "dining_room", "hallway",
    "classroom", "laboratory", "library", "auditorium", "gym",
    "music_room", "art_room", "staff_room", "reading_room",
    "computer_lab", "courtyard", "multimedia_room", "general_office",
    "utility", "lobby", "reception", "unknown",
})


def regions_to_floor_objects(regions: Iterable[RoomRegion]) -> list[dict]:
    """
    Convert regions to schema-compatible FloorObject dicts.

    Precedence for the displayed label (what the reconstructed PNG shows):
      1. r.label_text  — free-form text that Gemini read off the floor plan
      2. r.label_hint  — canonical type from the LLM
      3. heuristic type (only "hallway" or "unknown")

    Precedence for the canonical type (drives render colour):
      1. r.label_hint if valid in the schema
      2. heuristic (returns "unknown" except for very narrow hallway shapes)
    """
    out: list[dict] = []
    for r in regions:
        hint = (r.label_hint or "").strip().lower().replace(" ", "_")
        if hint and hint in _SCHEMA_TYPES:
            otype = hint
        else:
            otype = classify_room_heuristic(r.bbox_pct)

        # Use the text Gemini actually read from the plan when available;
        # otherwise fall back to the canonical type's display name.
        label_text = (r.label_text or "").strip()
        display_label = label_text or otype.replace("_", " ").title()

        out.append(
            {
                "id": r.id,
                "type": otype,
                "label": display_label,
                "position": dict(r.bbox_pct),
                "partial": False,
                "confidence": "high" if (label_text or hint) else "low",
                "door_type": None,
                "door_swing": None,
                "accessible": True,
                "width_m": None,
                "notes": None,
                "seen_in_tiles": 1,
                "source": r.source,
            }
        )
    return out


def extract_rooms_cv_multiscale(
    image_bgr: np.ndarray,
    *,
    seal_pxs: tuple[int, ...] = (5, 9, 13),
    threshold: int | None = None,
    min_wall_component_px: int = 20,
    min_area_frac: float = 0.0015,
    max_area_frac: float = 0.45,
    iou_thresh: float = 0.30,
) -> tuple[list[RoomRegion], dict[str, np.ndarray]]:
    """
    Run `extract_rooms_cv_v2` at multiple door-seal scales and union the results.

    Rationale: a single seal can't recover both tightly-packed small rooms and
    rooms with wider door openings. Running multiple scales and merging via
    IoU (plus the intersection/containment rules inside `_dedup_regions`)
    recovers both without over-merging.
    """
    all_regions: list[RoomRegion] = []
    debug: dict[str, np.ndarray] = {}
    for seal in seal_pxs:
        regs, dbg = extract_rooms_cv_v2(
            image_bgr,
            threshold=threshold,
            door_seal_px=seal,
            min_wall_component_px=min_wall_component_px,
            min_area_frac=min_area_frac,
            max_area_frac=max_area_frac,
        )
        all_regions.extend(regs)
        debug[f"seal_{seal}_space"] = dbg.get("space")
    merged = _dedup_regions(all_regions, iou_thresh=iou_thresh)
    merged = [
        RoomRegion(
            id=f"cv_room_{i}",
            bbox_px=r.bbox_px,
            bbox_pct=r.bbox_pct,
            area_px=r.area_px,
            source=r.source,
            label_hint=r.label_hint,
            label_text=r.label_text,
        )
        for i, r in enumerate(merged)
    ]
    return merged, debug


def merge_regions(*groups: Iterable[RoomRegion], iou_thresh: float = 0.6) -> list[RoomRegion]:
    """Merge multiple region lists, keeping higher-area on conflict."""
    flat: list[RoomRegion] = []
    for g in groups:
        flat.extend(list(g))
    # Re-id so merged set is stable
    deduped = _dedup_regions(flat, iou_thresh=iou_thresh)
    return [
        RoomRegion(
            id=f"room_{i}",
            bbox_px=r.bbox_px,
            bbox_pct=r.bbox_pct,
            area_px=r.area_px,
            source=r.source,
            label_hint=r.label_hint,
            label_text=r.label_text,
        )
        for i, r in enumerate(deduped)
    ]
