"""Fix 4 + Fix 5: physical constraint solver and grid snapping."""
from __future__ import annotations
import logging
from schema import Corridor, FloorObject, FloorPlan, Position

log = logging.getLogger(__name__)

GRID_SIZE_PCT = 1.0
_DEFAULT_CORRIDOR_HALF_W = 3.5  # pct — used when width_pct unknown
_CONF_RANK = {"high": 2, "medium": 1, "low": 0}

# Legal nesting: child type → set of allowed parent types.
# If an object of a child type is contained in / adjacent to a parent of an
# allowed type, we treat them as a nested pair rather than an overlap conflict.
_NESTING_PARENTS: dict[str, frozenset[str]] = {
    "bathroom":  frozenset({"bedroom", "living_room"}),
    "kitchen":   frozenset({"living_room", "dining_room"}),
    "dining_room": frozenset({"living_room"}),
}


# ── Geometry helpers ─────────────────────────────────────────────────────────

def _box(obj: FloorObject) -> dict:
    p = obj.position
    return {"x": p.x, "y": p.y, "w": p.w, "h": p.h}


def _iou_dicts(a: dict, b: dict) -> float:
    ax0, ay0 = a["x"], a["y"]
    ax1, ay1 = ax0 + a["w"], ay0 + a["h"]
    bx0, by0 = b["x"], b["y"]
    bx1, by1 = bx0 + b["w"], by0 + b["h"]
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / union if union > 0 else 0.0


def _containment_fraction(inner: FloorObject, outer: FloorObject) -> float:
    """Fraction of `inner`'s area that falls inside `outer`'s bbox."""
    ip = inner.position
    op = outer.position
    ix0, iy0 = max(ip.x, op.x), max(ip.y, op.y)
    ix1, iy1 = min(ip.x + ip.w, op.x + op.w), min(ip.y + ip.h, op.y + op.h)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    inner_area = max(1e-6, ip.w * ip.h)
    return inter / inner_area


def _edge_adjacent(a: FloorObject, b: FloorObject, tol: float = 2.0) -> bool:
    """True if `a` and `b` share (or nearly share) an edge without overlapping much."""
    ap, bp = a.position, b.position
    ax0, ay0, ax1, ay1 = ap.x, ap.y, ap.x + ap.w, ap.y + ap.h
    bx0, by0, bx1, by1 = bp.x, bp.y, bp.x + bp.w, bp.y + bp.h
    # vertical-edge neighbor
    vertical = (
        abs(ax1 - bx0) <= tol or abs(bx1 - ax0) <= tol
    ) and not (ay1 <= by0 or by1 <= ay0)
    # horizontal-edge neighbor
    horizontal = (
        abs(ay1 - by0) <= tol or abs(by1 - ay0) <= tol
    ) and not (ax1 <= bx0 or bx1 <= ax0)
    return vertical or horizontal


def _is_legal_nesting(a: FloorObject, b: FloorObject) -> bool:
    """True if the smaller of (a,b) is a legal child of the larger (by type)."""
    ap_area = a.position.w * a.position.h
    bp_area = b.position.w * b.position.h
    inner, outer = (a, b) if ap_area <= bp_area else (b, a)
    allowed_parents = _NESTING_PARENTS.get(inner.type, frozenset())
    if outer.type not in allowed_parents:
        return False
    return _containment_fraction(inner, outer) >= 0.6


def _overlap_fraction(room: FloorObject, obstacle: dict) -> float:
    """Fraction of room's area covered by obstacle."""
    rp = room.position
    rx0, ry0 = rp.x, rp.y
    rx1, ry1 = rx0 + rp.w, ry0 + rp.h
    ox0, oy0 = obstacle["x"], obstacle["y"]
    ox1, oy1 = ox0 + obstacle["w"], oy0 + obstacle["h"]
    ix0, iy0 = max(rx0, ox0), max(ry0, oy0)
    ix1, iy1 = min(rx1, ox1), min(ry1, oy1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    room_area = rp.w * rp.h
    return inter / room_area if room_area > 0 else 0.0


def _clip_away(room: FloorObject, obstacle: dict) -> FloorObject:
    """Shrink room so it no longer overlaps obstacle (pick the cut that preserves most area)."""
    rp = room.position
    rx0, ry0 = rp.x, rp.y
    rx1, ry1 = rx0 + rp.w, ry0 + rp.h
    ox0, oy0 = obstacle["x"], obstacle["y"]
    ox1, oy1 = ox0 + obstacle["w"], oy0 + obstacle["h"]

    options: list[tuple[float, float, float, float, float]] = []  # (area, x, y, w, h)

    # clip room's right edge to obstacle's left
    nw = ox0 - rx0
    if nw > 1:
        options.append((nw * rp.h, rx0, ry0, nw, rp.h))
    # clip room's left edge to obstacle's right
    nx = ox1
    nw2 = rx1 - ox1
    if nw2 > 1:
        options.append((nw2 * rp.h, nx, ry0, nw2, rp.h))
    # clip room's bottom edge to obstacle's top
    nh = oy0 - ry0
    if nh > 1:
        options.append((rp.w * nh, rx0, ry0, rp.w, nh))
    # clip room's top edge to obstacle's bottom
    ny = oy1
    nh2 = ry1 - oy1
    if nh2 > 1:
        options.append((rp.w * nh2, rx0, ny, rp.w, nh2))

    if not options:
        return room

    _, best_x, best_y, best_w, best_h = max(options, key=lambda o: o[0])
    return room.model_copy(update={"position": Position(x=best_x, y=best_y, w=best_w, h=best_h)})


def _corridor_boxes(corridors: list[Corridor]) -> list[dict]:
    """Convert each corridor's centerline to an AABB expanded by half-width."""
    boxes = []
    for corr in corridors:
        if not corr.centerline:
            continue
        half_w = _DEFAULT_CORRIDOR_HALF_W
        xs = [p.x for p in corr.centerline]
        ys = [p.y for p in corr.centerline]
        boxes.append({
            "x": min(xs) - half_w,
            "y": min(ys) - half_w,
            "w": (max(xs) - min(xs)) + 2 * half_w,
            "h": (max(ys) - min(ys)) + 2 * half_w,
            "id": corr.id,
        })
    return boxes


# ── Auto-nesting (fixes LLM-produced side-by-side splits) ────────────────────

def _auto_nest(rooms: list[FloorObject]) -> tuple[list[FloorObject], list[str]]:
    """
    For each child-type room (bathroom, kitchen, …) adjacent to a legal
    parent-type room (bedroom, living_room, …), extend the parent's bbox to
    cover both. The child keeps its original bbox, so it now sits nested
    inside the parent. This is the common "LLM reported the two halves of one
    unit side-by-side" failure mode.

    Returns (new_rooms, list_of_pair_strings) for reporting.
    """
    # Index rooms by id; work on a mutable list so we can update parents.
    out = [r for r in rooms]
    id_to_idx = {r.id: i for i, r in enumerate(out)}
    pairs: list[str] = []

    for child in list(rooms):
        parent_types = _NESTING_PARENTS.get(child.type)
        if not parent_types:
            continue
        # Find the best parent candidate: same or adjacent, of a legal type.
        best_parent_idx: int | None = None
        best_score = -1.0
        for i, cand in enumerate(out):
            if cand.id == child.id or cand.type not in parent_types:
                continue
            # Already contained? No work to do — but record the pair.
            contained = _containment_fraction(child, cand)
            if contained >= 0.6:
                best_parent_idx = i
                best_score = 999.0
                break
            # Edge-adjacent → LLM split. Score by how well the child's height/
            # width matches the parent's (sibling halves of one unit).
            if _edge_adjacent(child, cand):
                # prefer the closest, similarly-sized candidate
                sim = 1.0 - min(
                    abs(child.position.h - cand.position.h) / max(1.0, cand.position.h),
                    abs(child.position.w - cand.position.w) / max(1.0, cand.position.w),
                )
                if sim > best_score:
                    best_score = sim
                    best_parent_idx = i

        if best_parent_idx is None:
            continue

        parent = out[best_parent_idx]
        # If already contained, nothing to extend.
        if _containment_fraction(child, parent) >= 0.6:
            continue

        # Extend parent to cover BOTH bboxes.
        cp, pp = child.position, parent.position
        nx0 = min(cp.x, pp.x)
        ny0 = min(cp.y, pp.y)
        nx1 = max(cp.x + cp.w, pp.x + pp.w)
        ny1 = max(cp.y + cp.h, pp.y + pp.h)
        extended = parent.model_copy(update={"position": Position(
            x=nx0, y=ny0, w=max(0.5, nx1 - nx0), h=max(0.5, ny1 - ny0),
        )})
        out[best_parent_idx] = extended
        pairs.append(f"{child.id}⊂{parent.id}")

    return out, pairs


# ── Constraint solver ────────────────────────────────────────────────────────

def validate_and_resolve(floor_plan: FloorPlan) -> tuple[FloorPlan, list[str]]:
    issues: list[str] = []

    # ── Invariant 3: clamp all objects to [0, 100] ───────────────────────────
    def clamp(obj: FloorObject) -> FloorObject:
        p = obj.position
        x0 = max(0.0, p.x)
        y0 = max(0.0, p.y)
        x1 = min(100.0, p.x + p.w)
        y1 = min(100.0, p.y + p.h)
        if x0 == p.x and y0 == p.y and x1 == p.x + p.w and y1 == p.y + p.h:
            return obj
        issues.append(f"out_of_bounds:{obj.id}")
        return obj.model_copy(update={"position": Position(
            x=x0, y=y0, w=max(0.5, x1 - x0), h=max(0.5, y1 - y0)
        )})

    floor_plan.rooms = [clamp(o) for o in floor_plan.rooms]
    floor_plan.doors = [clamp(o) for o in floor_plan.doors]
    floor_plan.verticals = [clamp(o) for o in floor_plan.verticals]
    floor_plan.emergency = [clamp(o) for o in floor_plan.emergency]

    # ── Invariant 2: rooms must not overlap corridors ─────────────────────────
    corr_boxes = _corridor_boxes(floor_plan.corridors)
    fixed_rooms: list[FloorObject] = []
    for room in floor_plan.rooms:
        for cb in corr_boxes:
            if _overlap_fraction(room, cb) > 0.1:
                issues.append(f"room_eats_corridor:{room.id}+{cb['id']}")
                room = _clip_away(room, cb)
        fixed_rooms.append(room)
    floor_plan.rooms = fixed_rooms

    # ── Auto-nesting pre-pass ────────────────────────────────────────────────
    # If the LLM split a bedroom unit into two adjacent rectangles — e.g. a
    # "bedroom" next to a "bathroom" — extend the parent so it covers BOTH and
    # leave the child bbox untouched (now nested inside its parent). This
    # prevents the overlap invariant below from destroying the pair.
    floor_plan.rooms, nested = _auto_nest(floor_plan.rooms)
    for pair in nested:
        issues.append(f"auto_nested:{pair}")

    # ── Invariant 1: rooms must not overlap each other ───────────────────────
    rooms = list(floor_plan.rooms)
    for i in range(len(rooms)):
        for j in range(i + 1, len(rooms)):
            a, b = rooms[i], rooms[j]
            if _overlap_fraction(a, _box(b)) > 0.1 or _overlap_fraction(b, _box(a)) > 0.1:
                # Legitimate nesting (e.g. bathroom inside bedroom) is NOT a
                # conflict — keep both bboxes as-is.
                if _is_legal_nesting(a, b):
                    continue
                issues.append(f"room_overlap:{a.id}+{b.id}")
                # shrink the lower-confidence one
                a_rank = _CONF_RANK.get(a.confidence, 0)
                b_rank = _CONF_RANK.get(b.confidence, 0)
                if a_rank >= b_rank:
                    rooms[j] = _clip_away(b, _box(a))
                else:
                    rooms[i] = _clip_away(a, _box(b))
    floor_plan.rooms = rooms

    if issues:
        log.info("Constraint solver: %d issue(s) resolved (first 10: %s)", len(issues), issues[:10])

    return floor_plan, issues


# ── Grid snapping (Fix 5) ────────────────────────────────────────────────────

def snap_all_coordinates(floor_plan: FloorPlan, grid: float = GRID_SIZE_PCT) -> FloorPlan:
    def snap(v: float) -> float:
        return round(v / grid) * grid

    def snap_obj(obj: FloorObject) -> FloorObject:
        p = obj.position
        return obj.model_copy(update={"position": Position(
            x=snap(p.x), y=snap(p.y),
            w=max(grid, snap(p.w)),
            h=max(grid, snap(p.h)),
        )})

    floor_plan.rooms = [snap_obj(o) for o in floor_plan.rooms]
    floor_plan.doors = [snap_obj(o) for o in floor_plan.doors]
    floor_plan.verticals = [snap_obj(o) for o in floor_plan.verticals]
    floor_plan.emergency = [snap_obj(o) for o in floor_plan.emergency]
    floor_plan.labels = [snap_obj(o) for o in floor_plan.labels]
    return floor_plan
