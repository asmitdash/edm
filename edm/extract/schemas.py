"""Pydantic schemas for LLM extraction output.

Every extracted item carries enough information for graph_writer to:
  1. Create typed nodes (decisions / assumptions / constraints / alternatives)
  2. Create edges between them with confidence
  3. Provenance is added by the graph_writer itself (extraction_id), not the LLM.

The LLM must return JSON that matches `ExtractionResult`. We use Anthropic's
tool-use mechanism to enforce this — it's more reliable than free-form JSON.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DecisionItem(BaseModel):
    local_id: str = Field(description="Stable id within this extraction; use d1, d2, ...")
    title: str = Field(max_length=160)
    summary: str = Field(description="One paragraph, neutral tense, third person.")
    quoted_evidence: str = Field(description="Verbatim quote from the source supporting this decision.")


class AssumptionItem(BaseModel):
    local_id: str = Field(description="Stable id within this extraction; use a1, a2, ...")
    statement: str = Field(description="What the decision relied on being true.")
    quoted_evidence: str


class ConstraintItem(BaseModel):
    local_id: str = Field(description="Stable id within this extraction; use c1, c2, ...")
    statement: str
    kind: Literal["latency", "cost", "compliance", "security", "team", "deadline", "compatibility", "other"]
    quoted_evidence: str


class AlternativeItem(BaseModel):
    local_id: str = Field(description="Stable id within this extraction; use alt1, alt2, ...")
    decision_local_id: str = Field(description="Which decision this alternative was considered for.")
    description: str
    rejection_reason: str | None = None


class EdgeItem(BaseModel):
    src_local_id: str
    dst_local_id: str
    relation: Literal[
        "depends_on", "motivated_by", "supersedes", "contradicts", "related_to", "considered"
    ]
    confidence: float = Field(ge=0.0, le=1.0)


class ExtractionResult(BaseModel):
    decisions: list[DecisionItem] = Field(default_factory=list)
    assumptions: list[AssumptionItem] = Field(default_factory=list)
    constraints: list[ConstraintItem] = Field(default_factory=list)
    alternatives: list[AlternativeItem] = Field(default_factory=list)
    edges: list[EdgeItem] = Field(default_factory=list)
    notes: str | None = Field(
        default=None,
        description="Optional: anything notable that didn't fit a category, e.g. 'this PR appears to revert prior work'.",
    )

    def is_empty(self) -> bool:
        return not (self.decisions or self.assumptions or self.constraints or self.alternatives)
