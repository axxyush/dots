"""ADA-oriented compliance reporting from parsed floor-plan JSON.

The output is a structured preliminary report (not legal certification).
It cites 2010 ADA Standards section IDs where applicable and records:
- observed condition
- likely impact
- recommended remediation
- confidence / evidence constraints
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_STAIR_TYPES = {"stairs"}
_ELEVATOR_TYPES = {"elevator"}
_RESTROOM_TYPES = {"restroom", "bathroom"}
_ENTRY_TYPES = {"entrance"}
_PUBLIC_ROOM_TYPES = {
    "store",
    "restaurant",
    "office",
    "cafe",
    "service_counter",
    "classroom",
    "laboratory",
    "library",
    "auditorium",
    "gym",
    "music_room",
    "art_room",
    "staff_room",
    "reading_room",
    "computer_lab",
    "multimedia_room",
    "general_office",
    "lobby",
    "reception",
}


@dataclass
class Finding:
    finding_id: str
    category: str
    severity: str
    ada_reference: str
    requirement: str
    observed_condition: str
    impact: str
    remediation: str
    evidence: str
    confidence: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "finding_id": self.finding_id,
            "category": self.category,
            "severity": self.severity,
            "ada_reference": self.ada_reference,
            "requirement": self.requirement,
            "observed_condition": self.observed_condition,
            "impact": self.impact,
            "remediation": self.remediation,
            "evidence": self.evidence,
            "confidence": self.confidence,
        }


def _all_objects(fp: dict[str, Any]) -> list[dict[str, Any]]:
    objs: list[dict[str, Any]] = []
    for bucket in ("rooms", "doors", "verticals", "emergency", "labels"):
        objs.extend(fp.get(bucket, []) or [])
    return objs


def _type_count(objects: list[dict[str, Any]], kind: set[str]) -> int:
    return sum(1 for o in objects if o.get("type") in kind)


def _objects_by_type(objects: list[dict[str, Any]], kind: set[str]) -> list[dict[str, Any]]:
    return [o for o in objects if o.get("type") in kind]


def _position_center(pos: dict[str, Any]) -> tuple[float, float]:
    x = float(pos.get("x", 0.0))
    y = float(pos.get("y", 0.0))
    w = float(pos.get("w", 0.0))
    h = float(pos.get("h", 0.0))
    return x + (w / 2.0), y + (h / 2.0)


def _nearest_distance(a: dict[str, Any], many: list[dict[str, Any]]) -> float | None:
    apos = a.get("position") or {}
    if not apos or not many:
        return None
    ax, ay = _position_center(apos)
    best: float | None = None
    for obj in many:
        bpos = obj.get("position") or {}
        if not bpos:
            continue
        bx, by = _position_center(bpos)
        d = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
        if best is None or d < best:
            best = d
    return best


def _build_report_text(
    source_image: str,
    summary: dict[str, int],
    findings: list[Finding],
) -> str:
    status = "No major accessibility issues detected from available geometry."
    if summary["high_severity"] > 0:
        status = "Potential non-compliance risks detected. Prioritize high-severity remediation."
    elif summary["medium_severity"] > 0:
        status = "Review required. Medium-severity accessibility gaps were identified."

    lines: list[str] = [
        "ADA ACCESSIBILITY PRELIMINARY COMPLIANCE REPORT",
        f"Source Plan: {source_image or 'unknown'}",
        "Standard Basis: 2010 ADA Standards for Accessible Design (selected applicable sections).",
        "",
        "Executive Summary",
        (
            f"Report Status: {status} "
            f"Findings={summary['total_findings']} "
            f"(High={summary['high_severity']}, Medium={summary['medium_severity']}, Low={summary['low_severity']})."
        ),
        "",
    ]

    if not findings:
        lines.append(
            "No specific non-compliance findings were generated from current parsed data. "
            "Field verification is still required."
        )
        return "\n".join(lines)

    lines.append("Detailed Findings")
    for f in findings:
        lines.extend(
            [
                "",
                f"{f.finding_id} | Severity: {f.severity.upper()} | Reference: {f.ada_reference}",
                f"Requirement: {f.requirement}",
                f"Observed Condition: {f.observed_condition}",
                f"Impact: {f.impact}",
                f"Recommended Corrective Action: {f.remediation}",
                f"Evidence: {f.evidence}",
                f"Assessment Confidence: {f.confidence}",
            ]
        )
    lines.extend(
        [
            "",
            "Limitations and Professional Review Note",
            (
                "This is a plan-level automated screening report. Final ADA compliance must be "
                "validated by a licensed professional with dimensioned drawings, site context, "
                "and local code overlays."
            ),
        ]
    )
    return "\n".join(lines)


def generate_ada_recommendations(floor_plan: dict[str, Any]) -> dict[str, Any]:
    """Return structured ADA preliminary compliance report from a floor plan dict."""
    objects = _all_objects(floor_plan)
    corridors = floor_plan.get("corridors", []) or []
    nav_edges = (floor_plan.get("navigation_graph") or {}).get("edges", []) or []

    findings: list[Finding] = []

    stairs = _objects_by_type(objects, _STAIR_TYPES)
    elevators = _objects_by_type(objects, _ELEVATOR_TYPES)
    restrooms = _objects_by_type(objects, _RESTROOM_TYPES)
    entries = _objects_by_type(objects, _ENTRY_TYPES)
    public_rooms = _objects_by_type(objects, _PUBLIC_ROOM_TYPES)

    # 1) Vertical accessibility: stairs without elevator nearby usually need lift/ramp alternative.
    if stairs and not elevators:
        findings.append(
            Finding(
                finding_id="ADA-F001",
                category="vertical_access",
                severity="high",
                ada_reference="2010 ADA 206.2 / 206.6 / 402",
                requirement=(
                    "Accessible routes must connect accessible spaces and passenger elevators "
                    "must comply where provided as part of vertical circulation."
                ),
                observed_condition=(
                    "Stair circulation is present but no elevator/lift object was detected in the plan."
                ),
                impact=(
                    "Wheelchair users may be unable to reach upper/lower level program areas."
                ),
                remediation=(
                    "Provide an accessible vertical connection (elevator preferred; ramp where feasible), "
                    "located on the primary circulation path."
                ),
                evidence=f"Detected {len(stairs)} stair area(s) and 0 elevator area(s).",
                confidence="medium",
            )
        )

    # 2) Accessible restroom presence in public layouts.
    if public_rooms and not restrooms:
        findings.append(
            Finding(
                finding_id="ADA-F002",
                category="restroom_access",
                severity="high",
                ada_reference="2010 ADA 213.1 / 213.2 / 603",
                requirement=(
                    "Where toilet facilities are provided, compliant toilet rooms must be accessible."
                ),
                observed_condition="No restroom/bathroom areas were detected in a public-facing layout.",
                impact="Accessible sanitary facilities may be absent or not clearly planned.",
                remediation=(
                    "Add at least one accessible toilet room on an accessible route with required clearances."
                ),
                evidence=(
                    f"Detected {len(public_rooms)} public room(s) but 0 restroom/bathroom objects."
                ),
                confidence="low",
            )
        )

    # 3) Entry route cue.
    if not entries:
        findings.append(
            Finding(
                finding_id="ADA-F003",
                category="entry_access",
                severity="medium",
                ada_reference="2010 ADA 206.4 / 404 / 216.6",
                requirement=(
                    "Required entrances must be on accessible routes and accessible entrances should be identified."
                ),
                observed_condition="No explicit entrance object was identified in the parsed plan.",
                impact="Accessible approach and wayfinding may be ambiguous for users with disabilities.",
                remediation=(
                    "Designate and detail a compliant accessible entrance with proper signage and door clearances."
                ),
                evidence="No object with type 'entrance' found in parsed floor plan.",
                confidence="medium",
            )
        )

    # 4) Door width sanity check where width_m is available.
    narrow_doors = [
        d
        for d in floor_plan.get("doors", []) or []
        if isinstance(d.get("width_m"), (int, float)) and float(d["width_m"]) < 0.9
    ]
    if narrow_doors:
        findings.append(
            Finding(
                finding_id="ADA-F004",
                category="door_clearance",
                severity="high",
                ada_reference="2010 ADA 404.2.3",
                requirement="Door openings on accessible routes must provide 32 inches minimum clear width.",
                observed_condition="One or more door widths are below recommended accessible clearance.",
                impact="Insufficient door clearance can block wheelchair access and safe maneuvering.",
                remediation=(
                    "Increase door clear widths to meet or exceed ADA minimums and verify maneuvering clearances."
                ),
                evidence=f"{len(narrow_doors)} door(s) reported below 0.9m width.",
                confidence="high",
            )
        )

    # 5) Corridor width check where width_m is available.
    narrow_corridors = [
        c
        for c in corridors
        if isinstance(c.get("width_m"), (int, float)) and float(c["width_m"]) < 1.2
    ]
    if narrow_corridors:
        findings.append(
            Finding(
                finding_id="ADA-F005",
                category="circulation_width",
                severity="high",
                ada_reference="2010 ADA 403.5.1 / 403.5.3",
                requirement=(
                    "Accessible route clear width must be 36 inches minimum, with passing spaces where required."
                ),
                observed_condition="Corridor segments were detected below 1.2m width in metadata.",
                impact="Users may be unable to pass or maneuver safely along the route.",
                remediation=(
                    "Widen route segments and provide passing spaces/turning areas at required intervals."
                ),
                evidence=f"{len(narrow_corridors)} corridor segment(s) reported below 1.2m width.",
                confidence="high",
            )
        )

    # 6) Connectivity warning when nav graph is missing.
    if not nav_edges:
        findings.append(
            Finding(
                finding_id="ADA-F006",
                category="route_continuity",
                severity="medium",
                ada_reference="2010 ADA 206.2 / 402 / 403",
                requirement=(
                    "A continuous accessible route should connect required spaces and circulation components."
                ),
                observed_condition=(
                    "No route graph connectivity was available from parsed output."
                ),
                impact="Continuity of accessible travel path cannot be confirmed from current data.",
                remediation=(
                    "Produce or validate route connectivity from entrance to major rooms and amenities."
                ),
                evidence="Navigation graph has no edges, so route connectivity is unknown.",
                confidence="low",
            )
        )

    # 7) Stairs near restroom hint for ramp/lift adjacency (heuristic).
    if stairs and restrooms and not elevators:
        near_pairs = 0
        for rr in restrooms:
            d = _nearest_distance(rr, stairs)
            if d is not None and d < 12.0:  # normalized coords scale
                near_pairs += 1
        if near_pairs:
            findings.append(
                Finding(
                    finding_id="ADA-F007",
                    category="service_access",
                    severity="medium",
                    ada_reference="2010 ADA 206.2 / 402 / 213",
                    requirement=(
                        "Accessible routes should connect sanitary/service destinations without stair-only barriers."
                    ),
                    observed_condition=(
                        "Restroom areas appear close to stair zones while no elevator was detected."
                    ),
                    impact=(
                        "Users with mobility impairments may face interrupted access to key services."
                    ),
                    remediation=(
                        "Add an accessible vertical route and confirm uninterrupted route to restroom/service areas."
                    ),
                    evidence=(
                        f"{near_pairs} restroom area(s) appear close to stairs with no elevator detected."
                    ),
                    confidence="low",
                )
            )

    # Keep high-signal top findings first.
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    findings.sort(
        key=lambda f: (severity_rank.get(f.severity, 9), f.category, f.finding_id)
    )

    summary = {
        "total_findings": len(findings),
        "high_severity": sum(1 for f in findings if f.severity == "high"),
        "medium_severity": sum(1 for f in findings if f.severity == "medium"),
        "low_severity": sum(1 for f in findings if f.severity == "low"),
    }
    report_text = _build_report_text(
        source_image=str(floor_plan.get("source_image", "")),
        summary=summary,
        findings=findings,
    )
    return {
        "summary": summary,
        "findings": [f.as_dict() for f in findings],
        "report_text": report_text,
        "note": (
            "Automated preliminary screening. Final ADA compliance requires licensed professional review."
        ),
    }
