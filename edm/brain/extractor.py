"""Procedure extractor: source row -> ProcedureExtractionResult.

Mirrors `edm.extract.extractor` but for the Company-Brain layer. Same
LLMProvider abstraction, different schema and prompt.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from edm.brain.prompts import (
    PROCEDURE_PROMPT_VERSION,
    PROCEDURE_SYSTEM,
    PROCEDURE_USER_TEMPLATE,
)
from edm.brain.schemas import ProcedureExtractionResult
from edm.extract.providers import LLMProvider, get_llm_provider
from edm.logging import get_logger

log = get_logger(__name__)


_PROCEDURE_TOOL_SCHEMA: dict[str, Any] = {
    "name": "record_procedures",
    "description": "Record extracted procedures with steps and guardrails.",
    "input_schema": {
        "type": "object",
        "properties": {
            "procedures": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "local_id": {"type": "string"},
                        "function_slug": {"type": "string"},
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "trigger_description": {"type": "string"},
                        "owner_role": {"type": "string"},
                        "steps": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "step_index": {"type": "integer"},
                                    "instruction": {"type": "string"},
                                    "actor_role": {"type": "string"},
                                    "tool_or_system": {"type": "string"},
                                    "expected_outcome": {"type": "string"},
                                },
                                "required": ["step_index", "instruction"],
                            },
                        },
                        "guardrails": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "kind": {
                                        "type": "string",
                                        "enum": [
                                            "always", "never", "condition",
                                            "escalation", "exception", "compliance",
                                        ],
                                    },
                                    "statement": {"type": "string"},
                                    "condition_expr": {"type": "string"},
                                    "severity": {
                                        "type": "string",
                                        "enum": ["low", "medium", "high", "critical"],
                                    },
                                },
                                "required": ["kind", "statement"],
                            },
                        },
                    },
                    "required": ["local_id", "function_slug", "title", "summary"],
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["procedures"],
    },
}


class ProcedureExtractionRun:
    def __init__(
        self,
        result: ProcedureExtractionResult,
        raw_response: dict[str, Any],
        prompt_version: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        self.result = result
        self.raw_response = raw_response
        self.prompt_version = prompt_version
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def extract_procedures_from_source(
    *,
    kind: str,
    title: str | None,
    author: str | None,
    occurred_at: Any,
    body: str,
    provider: LLMProvider | None = None,
) -> ProcedureExtractionRun:
    provider = provider or get_llm_provider()
    user = PROCEDURE_USER_TEMPLATE.format(
        kind=kind,
        title=title or "(untitled)",
        author=author or "(unknown)",
        occurred_at=occurred_at,
        body=body[:25000],
    )
    log.info(
        "brain.extract.call",
        provider=provider.name, model=provider.model,
        prompt_version=PROCEDURE_PROMPT_VERSION,
    )
    llm = provider.extract(system=PROCEDURE_SYSTEM, user=user, schema=_PROCEDURE_TOOL_SCHEMA)
    try:
        parsed = ProcedureExtractionResult.model_validate(llm.payload)
    except ValidationError as e:
        raise RuntimeError(f"Procedure extraction failed schema validation: {e}") from e

    return ProcedureExtractionRun(
        result=parsed,
        raw_response=llm.raw_response,
        prompt_version=PROCEDURE_PROMPT_VERSION,
        model=llm.model,
        input_tokens=llm.input_tokens,
        output_tokens=llm.output_tokens,
    )
