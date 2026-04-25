from __future__ import annotations
import itertools
import logging
import math
from typing import NamedTuple

from schema import Corridor, FloorObject, NavEdge, NavNode, NavigationGraph, Point

log = logging.getLogger(__name__)


# ── IoU helpers ──────────────────────────────────────────────────────────────

def _iou(a: FloorObject, b: FloorObject) -> float:
    ax0, ay0 = a.position.x, a.position.y
    ax1, ay1 = ax0 + a.position.w, ay0 + a.position.h
    bx0, by0 = b.position.x, b.position.y
    bx1, by1 = bx0 + b.position.w, by0 + b.position.h

    inter_x0 = max(ax0, bx0)
    inter_y0 = max(ay0, by0)
    inter_x1 = min(ax1, bx1)
    inter_y1 = min(ay1, by1)

    if inter_x1 <= inter_x0 or inter_y1 <= inter_y0:
        return 0.0

    inter_area = (inter_x1 - inter_x0) * (inter_y1 - inter_y0)
    area_a = (ax1 - ax0) * (ay1 - ay0)
    area_b = (bx1 - bx0) * (by1 - by0)
    union_area = area_a + area_b - inter_area
    return inter_area / union_area if union_area > 0 else 0.0


_CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}


def _higher_conf(a: FloorObject, b: FloorObject) -> FloorObject:
    return a if _CONFIDENCE_RANK[a.confidence] >= _CONFIDENCE_RANK[b.confidence] else b


# ── Main dedup ───────────────────────────────────────────────────────────────

_SMALL_TYPES = frozenset({"door", "fire_extinguisher", "fire_alarm"})
_LARGE_TYPES = frozenset({
    "store", "restaurant", "office", "cafe", "rest_area", "restroom",
    "service_counter", "entrance",
    # residential rooms are also "large" for dedup purposes — same lax IoU rules
    "bedroom", "bathroom", "living_room", "kitchen", "dining_room", "hallway",
})


def _centroid_dist(a: FloorObject, b: FloorObject) -> float:
    cx_a = a.position.x + a.position.w / 2
    cy_a = a.position.y + a.position.h / 2
    cx_b = b.position.x + b.position.w / 2
    cy_b = b.position.y + b.position.h / 2
    return math.hypot(cx_a - cx_b, cy_a - cy_b)


def _containment(a: FloorObject, b: FloorObject) -> float:
    """Fraction of the smaller bbox that sits inside the larger bbox."""
    ax0, ay0 = a.position.x, a.position.y
    ax1, ay1 = ax0 + a.position.w, ay0 + a.position.h
    bx0, by0 = b.position.x, b.position.y
    bx1, by1 = bx0 + b.position.w, by0 + b.position.h

    ix0 = max(ax0, bx0); iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1); iy1 = min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0

    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max(1e-6, (ax1 - ax0) * (ay1 - ay0))
    area_b = max(1e-6, (bx1 - bx0) * (by1 - by0))
    return inter / min(area_a, area_b)


def _should_merge(a: FloorObject, b: FloorObject) -> bool:
    """Type-aware merge decision using centroid distance + IoU + containment."""
    if a.type != b.type:
        return False  # never merge different types

    dist = _centroid_dist(a, b)
    iou = _iou(a, b)
    cont = _containment(a, b)

    # Containment catches the common "partial bedroom at tile edge" case where
    # one tile reports a sliver and another reports the full room.
    if cont >= 0.6:
        return True

    if a.type in _SMALL_TYPES:
        return dist < 2.0

    if a.type in _LARGE_TYPES:
        return iou > 0.2 or (dist < 5.0 and iou > 0) or dist < 3.0

    # default (corridors-as-objects, labels, verticals, emergency)
    return iou > 0.4 or dist < 3.0


def dedup_objects(objects: list[FloorObject]) -> list[FloorObject]:
    """
    Union-find dedup with type-aware merge logic (Fix 3).
    Returns deduplicated list with seen_in_tiles and confidence updated.
    """
    n = len(objects)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    for i, j in itertools.combinations(range(n), 2):
        if _should_merge(objects[i], objects[j]):
            union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    result: list[FloorObject] = []
    for indices in groups.values():
        best = objects[indices[0]]
        for idx in indices[1:]:
            best = _higher_conf(best, objects[idx])
        seen = len(indices)
        best = best.model_copy(update={
            "seen_in_tiles": seen,
            "confidence": "high" if seen > 1 else best.confidence,
        })
        result.append(best)

    return result


# ── Corridor dedup (by centerline proximity) ─────────────────────────────────

def _corridor_center(c: Corridor) -> tuple[float, float]:
    xs = [p.x for p in c.centerline]
    ys = [p.y for p in c.centerline]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def dedup_corridors(corridors: list[Corridor], dist_threshold: float = 5.0) -> list[Corridor]:
    n = len(corridors)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    centers = [_corridor_center(c) for c in corridors]
    for i, j in itertools.combinations(range(n), 2):
        cx0, cy0 = centers[i]
        cx1, cy1 = centers[j]
        if math.hypot(cx1 - cx0, cy1 - cy0) < dist_threshold:
            parent[find(i)] = find(j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    result: list[Corridor] = []
    for indices in groups.values():
        # keep longest centerline as representative
        best = max((corridors[i] for i in indices), key=lambda c: len(c.centerline))
        best = best.model_copy(update={"seen_in_tiles": len(indices)})
        result.append(best)
    return result


# ── Navigation graph ─────────────────────────────────────────────────────────

def _point_dist(a: Point, b: Point) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def build_nav_graph(
    corridors: list[Corridor],
    rooms: list[FloorObject],
    doors: list[FloorObject],
    verticals: list[FloorObject],
) -> NavigationGraph:
    nodes: list[NavNode] = []
    edges: list[NavEdge] = []
    node_counter = itertools.count()

    def add_node(pos: Point, ntype: str, accessible: bool = True) -> str:
        nid = f"node_{next(node_counter)}"
        nodes.append(NavNode(id=nid, position=pos, type=ntype, accessible=accessible))
        return nid

    # endpoints of each corridor become nodes; corridor becomes an edge
    for corr in corridors:
        if len(corr.centerline) < 2:
            continue
        start_pos = corr.centerline[0]
        end_pos = corr.centerline[-1]
        n0 = add_node(start_pos, "corridor_junction", corr.accessible)
        n1 = add_node(end_pos, "corridor_junction", corr.accessible)
        edges.append(NavEdge(
            id=f"edge_{corr.id}",
            from_node=n0,
            to_node=n1,
            width_m=corr.width_m,
            accessible=corr.accessible,
            corridor_id=corr.id,
        ))

    # add nodes for doors (room entrances)
    for door in doors:
        cx = door.position.x + door.position.w / 2
        cy = door.position.y + door.position.h / 2
        add_node(Point(x=cx, y=cy), "room_entrance", door.accessible)

    # add nodes for elevators and stairs
    for obj in verticals:
        cx = obj.position.x + obj.position.w / 2
        cy = obj.position.y + obj.position.h / 2
        ntype = "elevator" if obj.type == "elevator" else "stairs"
        add_node(Point(x=cx, y=cy), ntype, obj.accessible)

    # add exit nodes
    for obj in rooms:
        if obj.type in ("fire_exit", "entrance"):
            cx = obj.position.x + obj.position.w / 2
            cy = obj.position.y + obj.position.h / 2
            add_node(Point(x=cx, y=cy), "exit", obj.accessible)

    return NavigationGraph(nodes=nodes, edges=edges)
