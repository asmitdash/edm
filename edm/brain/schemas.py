"""Pydantic schemas for procedure extraction (Company Brain layer).

A procedure is a runnable how-X-gets-done unit:
  * trigger    - when does this fire
  * steps      - ordered, atomic, with actor + tool
  * guardrails - must / never / exceptions / escalations
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


GuardrailKind = Literal["always", "never", "condition", "escalation", "exception", "compliance"]
GuardrailSeverity = Literal["low", "medium", "high", "critical"]


class ProcedureStep(BaseModel):
    step_index: int = Field(ge=1)
    instruction: str
    actor_role: str | None = None
    tool_or_system: str | None = None
    expected_outcome: str | None = None


class ProcedureGuardrail(BaseModel):
    kind: GuardrailKind
    statement: str
    condition_expr: str | None = None
    severity: GuardrailSeverity = "medium"


class ProcedureItem(BaseModel):
    local_id: str = Field(description="Stable id within this extraction; use p1, p2, ...")
    function_slug: str = Field(
        description=(
            "Which company function this belongs to. One of: engineering, product, sales, "
            "support, success, marketing, finance, hr, legal, it, operations, security."
        )
    )
    title: str = Field(max_length=200)
    summary: str
    trigger_description: str | None = None
    owner_role: str | None = None
    steps: list[ProcedureStep] = Field(default_factory=list)
    guardrails: list[ProcedureGuardrail] = Field(default_factory=list)


class ProcedureExtractionResult(BaseModel):
    procedures: list[ProcedureItem] = Field(default_factory=list)
    notes: str | None = None

    def is_empty(self) -> bool:
        return not self.procedures
