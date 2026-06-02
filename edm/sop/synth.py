"""SOP synthesis: form + chat exceptions + retrieved precedent -> SOP markdown.

Then post-synthesis:
  * insert into sops (source_kind=generated)
  * call skills.build_skills_file to produce the executable artefact
  * link both to the originating session
  * (optional) write a `sources` row + run brain.extractor over the new SOP body
    so its procedures land in the Company Brain graph too.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import text

from edm.brain.extractor import extract_procedures_from_source
from edm.brain.retrieval import find_similar_sops
from edm.brain.writer import write_procedures
from edm.db import session_scope
from edm.extract.providers import LLMProvider, get_llm_provider
from edm.logging import get_logger
from edm.sop import corpus, sessions as session_repo
from edm.sop.schemas import SkillsFileBody, SynthesizedSOP

log = get_logger(__name__)


_SOP_SCHEMA: dict[str, Any] = {
    "name": "write_sop",
    "description": "Produce a finished SOP markdown document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "body_markdown": {
                "type": "string",
                "description": (
                    "Full SOP in markdown. Include sections: Purpose, Scope, "
                    "Roles & Responsibilities, Trigger, Procedure Steps "
                    "(numbered), Exceptions & Conditional Rules, Escalation, "
                    "Compliance Notes, Revision History."
                ),
            },
        },
        "required": ["title", "body_markdown"],
    },
}


_SKILLS_SCHEMA: dict[str, Any] = {
    "name": "write_skills_file",
    "description": "Produce an executable skills file (action graph) for the same SOP.",
    "input_schema": {
        "type": "object",
        "properties": {
            "schema_version": {"type": "string", "enum": ["skills.v1"]},
            "sop_title": {"type": "string"},
            "function_slug": {"type": "string"},
            "trigger": {"type": "string"},
            "owner_role": {"type": "string"},
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action_id": {"type": "string"},
                        "name": {"type": "string"},
                        "instruction": {"type": "string"},
                        "actor_role": {"type": "string"},
                        "tool_or_system": {"type": "string"},
                        "inputs": {"type": "array", "items": {"type": "string"}},
                        "outputs": {"type": "array", "items": {"type": "string"}},
                        "expected_outcome": {"type": "string"},
                        "next_action_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["action_id", "name", "instruction"],
                },
            },
            "guardrails": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["always", "never", "condition", "escalation", "exception", "compliance"],
                        },
                        "statement": {"type": "string"},
                        "applies_to_action_ids": {"type": "array", "items": {"type": "string"}},
                        "severity": {
                            "type": "string",
                            "enum": ["low", "medium", "high", "critical"],
                        },
                    },
                    "required": ["kind", "statement"],
                },
            },
        },
        "required": ["sop_title", "function_slug", "actions"],
    },
}


_SOP_SYSTEM = """\
You are a senior operations writer. You produce SOP (Standard Operating \
Procedure) documents from structured intake.

Your input is:
  - form data (org, function, role owners, etc.)
  - exceptions / conditional rules collected from a chat session
  - retrieved precedent SOPs (seed templates and / or the customer's own prior SOPs)

Rules:
1. The SOP must include sections: Purpose, Scope, Roles & Responsibilities, \
   Trigger, Procedure Steps (numbered, atomic), Exceptions & Conditional Rules, \
   Escalation, Compliance Notes, Revision History.
2. Pull structure and phrasing from the customer's own prior SOPs when available; \
   fall back to the seed templates only for missing structure.
3. Every exception captured in the chat must appear under "Exceptions & Conditional Rules".
4. Numbered steps must be atomic. One action per step.
5. Do not invent compliance frameworks or systems that were not mentioned. If a \
   compliance section has no input, write "None specified at intake."
6. Use the org_label and function name in the title when present.

Output via the `write_sop` tool only.
"""


_SKILLS_SYSTEM = """\
You produce an EXECUTABLE skills file (action graph) for an AI agent to run \
the same SOP an operator would follow. Inputs: the SOP markdown + the original \
intake data.

Rules:
1. Each action is atomic (one observable effect). Use stable action_ids \
   (step_01, step_02, ...).
2. next_action_ids encode flow. A linear SOP is just step_01 -> step_02 -> step_03.
3. Branches go through guardrails of kind 'condition'. The condition's \
   applies_to_action_ids is the set of actions whose execution is gated by it.
4. Every exception captured in intake must appear as a guardrail (kind \
   'exception' or 'condition' depending on shape).
5. Compliance constraints become guardrails of kind 'compliance', severity \
   'high' or 'critical'.
6. If a step requires a human (refund > $500 needs manager approval), add an \
   'escalation' guardrail and reference that step in applies_to_action_ids.
7. function_slug must mirror the SOP's function.

Output via the `write_skills_file` tool only.
"""


def _format_form(form_data: dict) -> str:
    if not form_data:
        return "(no form provided)"
    return json.dumps(form_data, indent=2)


def _format_exceptions(exceptions: list[dict]) -> str:
    if not exceptions:
        return "(no exceptions captured)"
    return "\n".join(
        f"- WHEN {e['when']} -> THEN {e['then']} (severity={e.get('severity', 'medium')})"
        for e in exceptions
    )


def _format_retrieved(seed_sops: list) -> str:
    if not seed_sops:
        return "(no precedent SOPs retrieved)"
    blocks = []
    for hit in seed_sops:
        blocks.append(
            f"### {hit.title} ({hit.source_kind}, score={hit.score:.2f})\n"
            f"{hit.body_markdown[:2400]}"
        )
    return "\n\n".join(blocks)


def _build_synthesis_query(form_data: dict, exceptions: list[dict]) -> str:
    parts = [form_data.get("title") or "", form_data.get("purpose") or "", form_data.get("scope") or ""]
    parts.extend(f"{e.get('when','')} {e.get('then','')}" for e in exceptions)
    return " ".join(p for p in parts if p).strip()


def generate_sop_for_session(
    session_id: UUID,
    *,
    consent_to_train: bool | None = None,
    provider: LLMProvider | None = None,
    also_index_in_brain: bool = True,
) -> dict:
    """End-to-end synthesis. Returns a summary dict with sop_id, skills_file_id."""
    provider = provider or get_llm_provider()
    sess = session_repo.get_session(session_id)
    if sess is None:
        raise ValueError(f"session {session_id} not found")

    if consent_to_train is None:
        consent_to_train = sess.consent_to_train

    session_repo.set_status(session_id, "generating")

    form_data = sess.form_data or {}
    exceptions = sess.derived_exceptions or []

    # 1) retrieve precedent
    query = _build_synthesis_query(form_data, exceptions) or (sess.title or "standard operating procedure")
    with session_scope() as s:
        seed_hits = find_similar_sops(
            s, query,
            function_slug=sess.function_slug,
            top_k=6,
            prefer_uploaded=True,
        )
    retrieved_ids = [h.id for h in seed_hits]

    # 2) synthesize human SOP
    sop_user = (
        f"Form data:\n{_format_form(form_data)}\n\n"
        f"Captured exceptions:\n{_format_exceptions(exceptions)}\n\n"
        f"Retrieved precedent SOPs (use as scaffolding, not verbatim):\n"
        f"{_format_retrieved(seed_hits)}\n\n"
        f"Org: {sess.org_label or '(unspecified)'} | Industry: {sess.industry or '(unspecified)'} | "
        f"Function: {sess.function_slug or '(unspecified)'}"
    )
    sop_llm = provider.extract(system=_SOP_SYSTEM, user=sop_user, schema=_SOP_SCHEMA)
    try:
        synthesized = SynthesizedSOP.model_validate(sop_llm.payload)
    except ValidationError as e:
        session_repo.set_status(session_id, "ready_to_generate")
        raise RuntimeError(f"SOP synthesis returned invalid payload: {e}") from e

    sop_id = corpus.store_generated_sop(
        title=synthesized.title,
        body_markdown=synthesized.body_markdown,
        function_slug=sess.function_slug,
        org_label=sess.org_label,
        industry=sess.industry,
        consent_to_train=consent_to_train,
        created_by=sess.initiated_by,
    )

    # 3) build the skills file (executable artefact)
    skills_user = (
        f"Generated SOP markdown:\n\n{synthesized.body_markdown}\n\n"
        f"Original intake form:\n{_format_form(form_data)}\n\n"
        f"Exceptions captured:\n{_format_exceptions(exceptions)}\n\n"
        f"Function slug: {sess.function_slug or 'operations'}"
    )
    skills_llm = provider.extract(system=_SKILLS_SYSTEM, user=skills_user, schema=_SKILLS_SCHEMA)
    try:
        skills_body = SkillsFileBody.model_validate(skills_llm.payload)
    except ValidationError as e:
        # Don't fail the whole gen; the human SOP is still good. Log and continue.
        log.warning("sop.skills_invalid", session_id=str(session_id), error=str(e))
        skills_body = SkillsFileBody(
            sop_title=synthesized.title,
            function_slug=sess.function_slug or "operations",
            actions=[],
        )

    body_json = skills_body.model_dump_json()
    checksum = hashlib.sha256(body_json.encode("utf-8")).hexdigest()
    with session_scope() as s:
        skills_file_id = s.execute(
            text(
                """
                INSERT INTO skills_files (sop_id, version, schema_version, body, checksum)
                VALUES (:sop, 1, :sv, CAST(:b AS JSONB), :ck)
                ON CONFLICT (sop_id, version) DO UPDATE
                  SET body = EXCLUDED.body,
                      checksum = EXCLUDED.checksum
                RETURNING id
                """
            ),
            {"sop": sop_id, "sv": "skills.v1", "b": body_json, "ck": checksum},
        ).scalar_one()

    session_repo.attach_generated(
        session_id,
        sop_id=sop_id,
        skills_file_id=skills_file_id,
        retrieved_sop_ids=retrieved_ids,
    )

    # 4) optional: pipe the new SOP through the Company Brain so its
    # procedures appear in /procedures, the graph, etc.
    if also_index_in_brain and consent_to_train:
        try:
            _index_sop_in_brain(
                sop_id=sop_id,
                title=synthesized.title,
                body_markdown=synthesized.body_markdown,
                provider=provider,
            )
        except Exception as e:
            log.warning("sop.brain_index_failed", sop_id=str(sop_id), error=str(e))

    return {
        "session_id": str(session_id),
        "sop_id": str(sop_id),
        "skills_file_id": str(skills_file_id),
        "retrieved_sop_ids": [str(x) for x in retrieved_ids],
        "n_exceptions": len(exceptions),
        "n_actions": len(skills_body.actions),
    }


def _index_sop_in_brain(*, sop_id: UUID, title: str, body_markdown: str, provider: LLMProvider) -> None:
    """Write the generated SOP body as a `sources` row, run procedure extraction,
    and link extracted procedures back to the SOP via procedures.sop_id."""
    body_hash = hashlib.sha256(body_markdown.encode("utf-8")).hexdigest()
    external_id = f"sop_generated/{sop_id}"
    with session_scope() as s:
        from datetime import datetime, timezone
        source_id = s.execute(
            text(
                """
                INSERT INTO sources
                  (kind, external_id, title, body, occurred_at, content_hash, metadata)
                VALUES
                  ('sop_generated', :ext, :t, :b, :ts, :h, CAST(:m AS JSONB))
                ON CONFLICT (kind, external_id) DO UPDATE
                  SET title = EXCLUDED.title, body = EXCLUDED.body,
                      content_hash = EXCLUDED.content_hash
                RETURNING id
                """
            ),
            {
                "ext": external_id, "t": title, "b": body_markdown,
                "ts": datetime.now(timezone.utc), "h": body_hash,
                "m": json.dumps({"sop_id": str(sop_id)}),
            },
        ).scalar_one()

    run = extract_procedures_from_source(
        kind="sop_generated", title=title, author=None, occurred_at=None,
        body=body_markdown, provider=provider,
    )
    if run.result.is_empty():
        return
    with session_scope() as s:
        write_procedures(s, source_id, run, sop_id=sop_id)
