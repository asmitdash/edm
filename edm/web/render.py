"""Render the decision graph to an interactive HTML using pyvis.

Demo-cut intentionally avoids a frontend build. v1 will replace this with a
Next.js + React Flow page that supports filtering, searching, and provenance
click-through.
"""

from __future__ import annotations

from pathlib import Path

from pyvis.network import Network
from sqlalchemy import text

from edm.db import session_scope


_NODE_COLORS = {
    "decision": "#3B82F6",
    "assumption": "#10B981",
    "constraint": "#F59E0B",
    "alternative": "#9CA3AF",
}

_RELATION_COLORS = {
    "depends_on": "#3B82F6",
    "motivated_by": "#F59E0B",
    "supersedes": "#8B5CF6",
    "contradicts": "#EF4444",
    "related_to": "#9CA3AF",
    "considered": "#6B7280",
}


def render_graph_to_file(out: Path) -> Path:
    net = Network(height="850px", width="100%", bgcolor="#0B0F19", font_color="#E5E7EB", directed=True)
    net.barnes_hut(gravity=-8000, central_gravity=0.3, spring_length=140)

    with session_scope() as session:
        for r in session.execute(text("SELECT id, title, summary FROM decisions")).mappings():
            net.add_node(
                f"decision:{r['id']}",
                label=_truncate(r["title"], 40),
                title=r["summary"],
                color=_NODE_COLORS["decision"],
                shape="box",
            )
        for r in session.execute(text("SELECT id, statement FROM assumptions")).mappings():
            net.add_node(
                f"assumption:{r['id']}",
                label=_truncate(r["statement"], 36),
                title=r["statement"],
                color=_NODE_COLORS["assumption"],
                shape="ellipse",
            )
        for r in session.execute(text("SELECT id, statement, kind FROM constraints")).mappings():
            net.add_node(
                f"constraint:{r['id']}",
                label=f"[{r['kind']}] {_truncate(r['statement'], 30)}",
                title=r["statement"],
                color=_NODE_COLORS["constraint"],
                shape="diamond",
            )
        for r in session.execute(text("SELECT id, description FROM decision_alternatives")).mappings():
            net.add_node(
                f"alternative:{r['id']}",
                label=_truncate(r["description"], 32),
                title=r["description"],
                color=_NODE_COLORS["alternative"],
                shape="dot",
            )

        for r in session.execute(
            text(
                """
                SELECT src_kind, src_id, dst_kind, dst_id, relation, confidence
                FROM edges
                """
            )
        ).mappings():
            net.add_edge(
                f"{r['src_kind']}:{r['src_id']}",
                f"{r['dst_kind']}:{r['dst_id']}",
                title=f"{r['relation']} (conf {r['confidence']:.2f})",
                color=_RELATION_COLORS.get(r["relation"], "#9CA3AF"),
                arrows="to",
                width=1 + 2 * float(r["confidence"]),
                dashes=(r["relation"] == "contradicts"),
            )

    out.parent.mkdir(parents=True, exist_ok=True)
    net.write_html(str(out), notebook=False, open_browser=False)
    return out


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"
