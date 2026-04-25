"""PDF rendering for ADA preliminary compliance reports."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    # Keep line breaks from report text while preserving paragraph wrapping.
    return Paragraph((text or "").replace("\n", "<br/>"), style)


def write_ada_report_pdf(
    *,
    ada_report: dict[str, Any],
    source_image: str,
    output_path: Path,
) -> Path:
    """Write a formatted ADA report PDF and return the written path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=LETTER,
        rightMargin=0.7 * inch,
        leftMargin=0.7 * inch,
        topMargin=0.7 * inch,
        bottomMargin=0.7 * inch,
        title="ADA Preliminary Compliance Report",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleCustom",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=17,
        spaceAfter=10,
    )
    h_style = ParagraphStyle(
        "H2Custom",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=12,
        spaceBefore=8,
        spaceAfter=4,
    )
    body_style = ParagraphStyle(
        "BodyCustom",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
    )
    small_style = ParagraphStyle(
        "SmallCustom",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=8.5,
        textColor=colors.HexColor("#555555"),
        leading=11,
    )

    summary = ada_report.get("summary", {}) or {}
    findings = ada_report.get("findings", []) or []

    story: list[Any] = []
    story.append(_p("ADA Accessibility Preliminary Compliance Report", title_style))
    story.append(
        _p(
            (
                f"Source Plan: {source_image or 'unknown'}<br/>"
                f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}<br/>"
                "Standard Basis: 2010 ADA Standards for Accessible Design (selected applicable sections)"
            ),
            body_style,
        )
    )
    story.append(Spacer(1, 0.15 * inch))

    table_data = [
        ["Total Findings", str(summary.get("total_findings", 0))],
        ["High Severity", str(summary.get("high_severity", 0))],
        ["Medium Severity", str(summary.get("medium_severity", 0))],
        ["Low Severity", str(summary.get("low_severity", 0))],
    ]
    summary_table = Table(table_data, colWidths=[1.7 * inch, 1.2 * inch])
    summary_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F4F7")),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D0D5DD")),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(_p("Executive Summary", h_style))
    story.append(summary_table)
    story.append(Spacer(1, 0.15 * inch))

    story.append(_p("Detailed Findings", h_style))
    if not findings:
        story.append(_p("No reportable findings were generated from current parsed data.", body_style))
    else:
        for i, f in enumerate(findings, start=1):
            fid = f.get("finding_id", f"F-{i:03d}")
            sev = str(f.get("severity", "unknown")).upper()
            ref = f.get("ada_reference", "N/A")
            story.append(_p(f"{fid} | Severity: {sev} | Reference: {ref}", body_style))
            story.append(_p(f"<b>Requirement:</b> {f.get('requirement', '')}", body_style))
            story.append(_p(f"<b>Observed Condition:</b> {f.get('observed_condition', '')}", body_style))
            story.append(_p(f"<b>Impact:</b> {f.get('impact', '')}", body_style))
            story.append(_p(f"<b>Recommended Corrective Action:</b> {f.get('remediation', '')}", body_style))
            story.append(_p(f"<b>Evidence:</b> {f.get('evidence', '')}", body_style))
            story.append(_p(f"<b>Assessment Confidence:</b> {f.get('confidence', '')}", body_style))
            story.append(Spacer(1, 0.12 * inch))

    story.append(Spacer(1, 0.1 * inch))
    story.append(_p("Professional Review Note", h_style))
    story.append(
        _p(
            (
                "This document is an automated preliminary screening report. Final ADA compliance "
                "must be confirmed by licensed professionals using full architectural dimensions, "
                "site constraints, and applicable local/state code overlays."
            ),
            small_style,
        )
    )

    doc.build(story)
    return output_path
