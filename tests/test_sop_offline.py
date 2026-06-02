"""Offline tests for the SOP Generator: corpus seeding, intake session,
chatbot turn, retrieval, synthesis, skills file."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from edm.db import session_scope
from edm.sop import chatbot, corpus, sessions as session_repo, synth
from edm.sop.schemas import SOPFormData


def _make_admin() -> str:
    """Create a throwaway user row so initiated_by FK is satisfied."""
    with session_scope() as s:
        uid = s.execute(
            text(
                """
                INSERT INTO users (username, password_hash, role)
                VALUES (:u, 'unused', 'admin')
                RETURNING id
                """
            ),
            {"u": f"tester-{uuid4().hex[:8]}"},
        ).scalar_one()
    return uid


def test_load_seed_library():
    seed_dir = Path(__file__).resolve().parents[1] / "edm" / "sop" / "seed_library"
    assert seed_dir.is_dir(), f"seed dir missing: {seed_dir}"
    n = corpus.load_seed_library_from_disk(seed_dir)
    assert n >= 5, f"expected >=5 seed SOPs, loaded {n}"
    rows = corpus.list_sops(source_kind="seed")
    assert len(rows) >= 5
    titles = [r.title for r in rows]
    assert any("Refund" in t for t in titles)
    assert any("Incident" in t for t in titles)


def test_session_lifecycle_and_chatbot_turn():
    uid = _make_admin()
    sess = session_repo.create_session(
        initiated_by=uid,
        function_slug="support",
        title="Refund SOP",
        org_label="Acme",
        industry="saas",
    )
    assert sess.status == "collecting_form"

    form = SOPFormData(
        org_name="Acme", industry="saas", function_slug="support",
        title="Refund SOP", purpose="Test purpose", scope="Test scope",
        in_charge_role="Support Lead", escalation_contact="Head of Support",
    )
    sess2 = session_repo.attach_form(sess.id, form)
    assert sess2.status == "chatting"
    assert sess2.form_data["org_name"] == "Acme"

    # User message that contains "manager approval" + "$500" -> stub extracts an exception
    chatbot.take_user_turn(sess.id, "Refunds over $500 need manager approval")
    after = session_repo.get_session(sess.id)
    assert len(after.derived_exceptions) >= 1
    msgs = session_repo.get_messages(sess.id)
    assert any(m["role"] == "assistant" for m in msgs)


def test_synthesis_produces_sop_and_skills_file():
    uid = _make_admin()
    # seed corpus so retrieval has something to work with
    seed_dir = Path(__file__).resolve().parents[1] / "edm" / "sop" / "seed_library"
    corpus.load_seed_library_from_disk(seed_dir)

    sess = session_repo.create_session(
        initiated_by=uid, function_slug="support",
        title="Refund SOP", org_label="Acme", industry="saas",
    )
    session_repo.attach_form(
        sess.id,
        SOPFormData(
            org_name="Acme", industry="saas", function_slug="support",
            title="Refund SOP", purpose="Process refund requests.",
            scope="All paid plans.", in_charge_role="Support Lead",
        ),
    )
    chatbot.take_user_turn(sess.id, "Refunds over $500 need manager approval")
    chatbot.take_user_turn(sess.id, "On Fridays casuals are OK but no black clothes")

    summary = synth.generate_sop_for_session(sess.id, also_index_in_brain=False)
    assert summary["sop_id"]
    assert summary["skills_file_id"]

    after = session_repo.get_session(sess.id)
    assert after.status == "completed"
    assert after.generated_sop_id is not None
    assert after.generated_skills_file_id is not None

    # The generated SOP exists in the corpus.
    sop_rows = corpus.list_sops(source_kind="generated")
    assert any(r.id == after.generated_sop_id for r in sop_rows)

    # Skills file body has at least one action.
    with session_scope() as s:
        body = s.execute(
            text("SELECT body FROM skills_files WHERE id = :id"),
            {"id": after.generated_skills_file_id},
        ).scalar_one()
        # body comes back as dict from JSONB
        assert isinstance(body, dict)
        assert body.get("actions"), "skills file should have actions"
        assert body.get("schema_version") == "skills.v1"


def test_uploaded_sops_outrank_seed_for_same_topic():
    uid = _make_admin()
    seed_dir = Path(__file__).resolve().parents[1] / "edm" / "sop" / "seed_library"
    corpus.load_seed_library_from_disk(seed_dir)

    # Upload a customer SOP on the same topic as a seed entry.
    customer_sop = corpus.upload_sop(
        title="Acme Refund Handling",
        body_markdown="# Acme Refund Handling\n\nCustomer-specific refund rules.",
        function_slug="support",
        org_label="Acme",
        industry="saas",
        created_by=uid,
    )

    from edm.brain.retrieval import find_similar_sops
    with session_scope() as s:
        hits = find_similar_sops(s, "refund handling process", function_slug="support", top_k=5, prefer_uploaded=True)
    assert hits, "expected retrieval hits"
    # the customer's uploaded SOP should rank above the seed (uploaded gets a +0.05 boost)
    top = hits[0]
    assert top.id == customer_sop, f"expected uploaded SOP to top the list, got {top.title}"
