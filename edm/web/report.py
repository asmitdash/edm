"""PDF report builder.

A multi-page PDF: page 1 is an executive summary cover (counts + top
contributors + a contributor-bar chart), then pages with member details,
decisions, contradictions. ReportLab — no external services."""

from __future__ import annotations

import io
from datetime import datetime, timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)
from sqlalchemy import text

from edm.db import session_scope
from edm.ingest.members import member_stats
from edm.install_config import get_github_install


_PRIMARY = colors.HexColor("#3B82F6")
_INK = colors.HexColor("#0B0F19")
_MUTED = colors.HexColor("#6B7280")
_ROSE = colors.HexColor("#EF4444")
_AMBER = colors.HexColor("#F59E0B")


def _styles():
    ss = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=ss["Title"], textColor=_INK, fontSize=22, leading=26, spaceAfter=4),
        "h2": ParagraphStyle("h2", parent=ss["Heading2"], textColor=_INK, fontSize=14, spaceBefore=14, spaceAfter=6),
        "body": ParagraphStyle("b", parent=ss["BodyText"], fontSize=9, leading=12),
        "muted": ParagraphStyle("m", parent=ss["BodyText"], fontSize=8, leading=10, textColor=_MUTED),
    }


def _make_contributor_chart(stats) -> bytes:
    """Bar chart of top 10 contributors by decision count. Returns PNG bytes."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    top = [s for s in stats if s.n_decisions > 0][:10]
    if not top:
        top = stats[:10] if stats else []
    labels = [s.github_login for s in top]
    decisions = [s.n_decisions for s in top]
    prs = [s.n_prs for s in top]

    fig, ax = plt.subplots(figsize=(7.5, 3.0))
    if not labels:
        ax.text(0.5, 0.5, "No contributor data", ha="center", va="center", fontsize=10, color="#666")
        ax.axis("off")
    else:
        x = range(len(labels))
        ax.bar([i - 0.2 for i in x], decisions, width=0.4, color="#3B82F6", label="Decisions")
        ax.bar([i + 0.2 for i in x], prs, width=0.4, color="#10B981", label="PRs")
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
        ax.legend(fontsize=8, frameon=False)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.set_ylabel("Count", fontsize=8)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _fetch_dashboard_counts():
    with session_scope() as s:
        return s.execute(text(
            """
            SELECT
              (SELECT COUNT(*) FROM sources)        AS sources,
              (SELECT COUNT(*) FROM decisions)      AS decisions,
              (SELECT COUNT(*) FROM assumptions)    AS assumptions,
              (SELECT COUNT(*) FROM constraints)    AS constraints,
              (SELECT COUNT(*) FROM conflict_findings WHERE status='open') AS open_findings
            """
        )).mappings().one()


def _fetch_decisions():
    with session_scope() as s:
        return s.execute(text(
            """
            SELECT d.title, d.summary, d.created_at,
                   sr.author AS author, sr.url AS source_url, sr.title AS source_title
            FROM decisions d
            LEFT JOIN extractions e ON e.id = d.extraction_id
            LEFT JOIN sources sr ON sr.id = e.source_id
            ORDER BY d.created_at DESC
            """
        )).mappings().all()


def _fetch_findings():
    with session_scope() as s:
        return s.execute(text(
            """
            SELECT cf.severity, cf.rationale, cf.created_at, cf.status,
                   d.title AS prior_title,
                   sr.url AS triggering_url, sr.title AS triggering_title, sr.author AS triggering_author
            FROM conflict_findings cf
            JOIN decisions d ON d.id = cf.conflicting_decision_id
            JOIN sources sr ON sr.id = cf.triggering_source_id
            ORDER BY cf.created_at DESC
            """
        )).mappings().all()


def build_pdf() -> bytes:
    styles = _styles()
    counts = _fetch_dashboard_counts()
    install = get_github_install()
    org = install.org_login
    stats = member_stats(org) if org else []
    decisions = _fetch_decisions()
    findings = _fetch_findings()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=1.6 * cm, rightMargin=1.6 * cm, topMargin=1.6 * cm, bottomMargin=1.6 * cm,
        title="Engineering Decision Memory — Report",
    )
    story = []

    # ---- Cover (page 1) ----
    story.append(Paragraph("Engineering Decision Memory", styles["title"]))
    story.append(Paragraph(
        f"Report generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        + (f" · GitHub org <b>{org}</b>" if org else " · GitHub not connected"),
        styles["muted"],
    ))
    story.append(Spacer(1, 0.6 * cm))

    summary_data = [
        ["Sources", "Decisions", "Assumptions", "Constraints", "Open findings"],
        [str(counts["sources"]), str(counts["decisions"]), str(counts["assumptions"]),
         str(counts["constraints"]), str(counts["open_findings"])],
    ]
    t = Table(summary_data, colWidths=[3.4 * cm] * 5, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
        ("TEXTCOLOR", (0, 0), (-1, 0), _MUTED),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 1), (-1, 1), 18),
        ("TEXTCOLOR", (0, 1), (-1, 1), _INK),
        ("TEXTCOLOR", (4, 1), (4, 1), _ROSE),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, 0), 0.4, colors.HexColor("#D1D5DB")),
    ]))
    story.append(t)
    story.append(Spacer(1, 0.5 * cm))

    if org:
        story.append(Paragraph("Top contributors (by decisions and PRs)", styles["h2"]))
        chart_png = _make_contributor_chart(stats)
        story.append(Image(io.BytesIO(chart_png), width=16 * cm, height=6.5 * cm))

    if findings:
        story.append(Paragraph("Recent contradictions", styles["h2"]))
        rows = [["Severity", "Triggering PR", "Prior decision", "Author"]]
        for f in findings[:5]:
            rows.append([
                f["severity"],
                Paragraph((f["triggering_title"] or "")[:60], styles["body"]),
                Paragraph((f["prior_title"] or "")[:60], styles["body"]),
                f["triggering_author"] or "—",
            ])
        ft = Table(rows, colWidths=[2 * cm, 6 * cm, 6 * cm, 3 * cm])
        ft.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8),
            ("FONTSIZE", (0, 1), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TEXTCOLOR", (0, 1), (0, -1), _ROSE),
            ("LINEBELOW", (0, 0), (-1, 0), 0.4, colors.HexColor("#D1D5DB")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAFAFA")]),
        ]))
        story.append(ft)

    # ---- Page 2: Members ----
    story.append(PageBreak())
    story.append(Paragraph("Members", styles["title"]))
    if not stats:
        story.append(Paragraph(
            "No member data. Connect GitHub from <b>Settings → GitHub</b> and refresh from the <b>Members</b> tab.",
            styles["body"],
        ))
    else:
        rows = [["Member", "Role", "PRs", "Decisions", "Conflicts", "Last activity"]]
        for m in stats:
            rows.append([
                f"@{m.github_login}" + (f" ({m.display_name})" if m.display_name else ""),
                m.role_in_org or "—",
                str(m.n_prs),
                str(m.n_decisions),
                str(m.n_findings),
                (m.last_activity_at or "")[:10] or "—",
            ])
        mt = Table(rows, colWidths=[6 * cm, 2 * cm, 1.5 * cm, 2.4 * cm, 2.2 * cm, 2.6 * cm], repeatRows=1)
        mt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAFAFA")]),
            ("TEXTCOLOR", (3, 1), (3, -1), _PRIMARY),
            ("TEXTCOLOR", (4, 1), (4, -1), _ROSE),
        ]))
        story.append(mt)

    # ---- Page 3+: Decisions ----
    story.append(PageBreak())
    story.append(Paragraph(f"Decisions ({len(decisions)})", styles["title"]))
    for d in decisions[:80]:
        story.append(Paragraph(d["title"], styles["h2"]))
        meta = []
        if d["author"]:
            meta.append(f"@{d['author']}")
        if d["created_at"]:
            meta.append(d["created_at"].strftime("%Y-%m-%d"))
        story.append(Paragraph(" · ".join(meta), styles["muted"]))
        story.append(Paragraph(d["summary"] or "", styles["body"]))
        if d["source_url"]:
            story.append(Paragraph(f'<font color="#3B82F6">Source: {d["source_url"]}</font>', styles["muted"]))
        story.append(Spacer(1, 0.2 * cm))

    # ---- Page N: Contradictions ----
    story.append(PageBreak())
    story.append(Paragraph(f"Contradictions ({len(findings)})", styles["title"]))
    if not findings:
        story.append(Paragraph("No contradictions detected.", styles["body"]))
    else:
        for f in findings:
            sev_color = {"high": _ROSE, "medium": _AMBER, "low": _MUTED}.get(f["severity"], _MUTED)
            story.append(Paragraph(
                f'<font color="{sev_color.hexval()}">[{f["severity"]}]</font> '
                + (f["triggering_title"] or "Triggering source"),
                styles["h2"],
            ))
            story.append(Paragraph(
                f'<b>Conflicts with:</b> {f["prior_title"]}',
                styles["body"],
            ))
            story.append(Paragraph(f["rationale"] or "", styles["body"]))
            if f["triggering_url"]:
                story.append(Paragraph(f'<font color="#3B82F6">{f["triggering_url"]}</font>', styles["muted"]))
            story.append(Spacer(1, 0.25 * cm))

    doc.build(story)
    return buf.getvalue()
