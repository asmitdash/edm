"""Builds the JSON payload for the Cytoscape /api/graph endpoint."""

from __future__ import annotations

from sqlalchemy import text

from edm.db import session_scope


def graph_payload() -> dict:
    nodes = []
    edges = []
    with session_scope() as s:
        for r in s.execute(text(
            """
            SELECT d.id, d.title, d.summary,
                   sr.url AS source_url, sr.title AS source_title, sr.author AS author
            FROM decisions d
            LEFT JOIN extractions e ON e.id = d.extraction_id
            LEFT JOIN sources sr ON sr.id = e.source_id
            """
        )).mappings():
            nodes.append({
                "id": f"decision:{r['id']}",
                "kind": "decision",
                "label": r["title"][:48],
                "full": r["summary"],
                "extra": {"source_url": r["source_url"], "source_title": r["source_title"], "author": r["author"]},
            })
        for r in s.execute(text("SELECT id, statement FROM assumptions")).mappings():
            nodes.append({
                "id": f"assumption:{r['id']}", "kind": "assumption",
                "label": r["statement"][:42], "full": r["statement"], "extra": {},
            })
        for r in s.execute(text("SELECT id, statement, kind FROM constraints")).mappings():
            nodes.append({
                "id": f"constraint:{r['id']}", "kind": "constraint",
                "label": f"[{r['kind']}] {r['statement'][:32]}", "full": r["statement"], "extra": {},
            })
        for r in s.execute(text("SELECT id, description FROM decision_alternatives")).mappings():
            nodes.append({
                "id": f"alternative:{r['id']}", "kind": "alternative",
                "label": r["description"][:36], "full": r["description"], "extra": {},
            })
        for r in s.execute(text(
            "SELECT id, src_kind, src_id, dst_kind, dst_id, relation, confidence FROM edges"
        )).mappings():
            edges.append({
                "id": f"e:{r['id']}",
                "source": f"{r['src_kind']}:{r['src_id']}",
                "target": f"{r['dst_kind']}:{r['dst_id']}",
                "relation": r["relation"],
                "confidence": float(r["confidence"]),
            })
    return {"nodes": nodes, "edges": edges}
