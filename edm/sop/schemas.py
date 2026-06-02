"""Pydantic schemas for SOP intake + generation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


SessionStatus = Literal[
    "open", "collecting_form", "chatting", "ready_to_generate",
    "generating", "completed", "abandoned",
]


class SOPFormData(BaseModel):
    """Canonical structured form fields. Optional fields stay None when not
    captured yet; the chatbot can fill in missing pieces."""

    org_name: str | None = None
    industry: str | None = None
    function_slug: str | None = None
    title: str | None = None
    purpose: str | None = None
    scope: str | None = None
    in_charge_role: str | None = None
    escalation_contact: str | None = None
    expected_frequency: str | None = None
    primary_systems: list[str] = Field(default_factory=list)
    compliance_tags: list[str] = Field(default_factory=list)


class DerivedException(BaseModel):
    """A single exception / conditional rule pulled from chat."""

    when: str = Field(description="Trigger or condition: 'on Fridays', 'if order > $500'")
    then: str = Field(description="The exception itself: 'casuals OK', 'manager approval required'")
    severity: Literal["info", "low", "medium", "high"] = "medium"


class GenerateSOPRequest(BaseModel):
    session_id: UUID
    consent_to_train: bool = True


class SessionSummary(BaseModel):
    id: UUID
    status: SessionStatus
    title: str | None
    function_slug: str | None
    org_label: str | None
    industry: str | None
    form_complete: bool
    n_chat_messages: int
    n_exceptions: int
    n_retrieved_seed_sops: int
    generated_sop_id: UUID | None
    generated_skills_file_id: UUID | None
    created_at: datetime
    updated_at: datetime


# ---------- the LLM-side schemas (synthesis output) ----------


class SynthesizedSOP(BaseModel):
    title: str
    body_markdown: str = Field(description="Full SOP in markdown, sections + numbered steps.")


class SkillsAction(BaseModel):
    action_id: str = Field(description="stable id within this skills file, e.g. step_01")
    name: str
    instruction: str
    actor_role: str | None = None
    tool_or_system: str | None = None
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    expected_outcome: str | None = None
    next_action_ids: list[str] = Field(default_factory=list)


class SkillsGuardrail(BaseModel):
    kind: Literal["always", "never", "condition", "escalation", "exception", "compliance"]
    statement: str
    applies_to_action_ids: list[str] = Field(default_factory=list)
    severity: Literal["low", "medium", "high", "critical"] = "medium"


class SkillsFileBody(BaseModel):
    """The executable artefact agents read."""

    schema_version: Literal["skills.v1"] = "skills.v1"
    sop_title: str
    function_slug: str
    trigger: str | None = None
    owner_role: str | None = None
    actions: list[SkillsAction]
    guardrails: list[SkillsGuardrail] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
