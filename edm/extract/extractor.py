"""Decision extractor: source row -> ExtractionResult, via the active LLMProvider.

The provider abstraction lives in `edm.extract.providers`. This module owns the
prompt templates, the JSON schema, and the audit-data shape (`ExtractionRun`).
"""

from __future__ import annotations

from typing import Any

from edm.extract.prompts import (
    EXTRACTION_PROMPT_VERSION,
    EXTRACTION_SYSTEM,
    EXTRACTION_USER_TEMPLATE,
)
from edm.extract.providers import LLMProvider, get_llm_provider, parse_extraction
from edm.extract.schemas import ExtractionResult
from edm.logging import get_logger

log = get_logger(__name__)


_TOOL_SCHEMA: dict[str, Any] = {
    "name": "record_extraction",
    "description": "Record the extracted decisions, assumptions, constraints, alternatives, and edges.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "local_id": {"type": "string"},
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "quoted_evidence": {"type": "string"},
                    },
                    "required": ["local_id", "title", "summary", "quoted_evidence"],
                },
            },
            "assumptions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "local_id": {"type": "string"},
                        "statement": {"type": "string"},
                        "quoted_evidence": {"type": "string"},
                    },
                    "required": ["local_id", "statement", "quoted_evidence"],
                },
            },
            "constraints": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "local_id": {"type": "string"},
                        "statement": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": [
                                "latency", "cost", "compliance", "security",
                                "team", "deadline", "compatibility", "other",
                            ],
                        },
                        "quoted_evidence": {"type": "string"},
                    },
                    "required": ["local_id", "statement", "kind", "quoted_evidence"],
                },
            },
            "alternatives": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "local_id": {"type": "string"},
                        "decision_local_id": {"type": "string"},
                        "description": {"type": "string"},
                        "rejection_reason": {"type": "string"},
                    },
                    "required": ["local_id", "decision_local_id", "description"],
                },
            },
            "edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "src_local_id": {"type": "string"},
                        "dst_local_id": {"type": "string"},
                        "relation": {
                            "type": "string",
                            "enum": [
                                "depends_on", "motivated_by", "supersedes",
                                "contradicts", "related_to", "considered",
                            ],
                        },
                        "confidence": {"type": "number"},
                    },
                    "required": ["src_local_id", "dst_local_id", "relation", "confidence"],
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["decisions", "assumptions", "constraints", "alternatives", "edges"],
    },
}


class ExtractionRun:
    """Output of a single extraction call: the parsed result + audit fields
    for the `extractions` table."""

    def __init__(
        self,
        result: ExtractionResult,
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


def extract_from_source(
    *,
    kind: str,
    title: str | None,
    author: str | None,
    occurred_at: Any,
    body: str,
    provider: LLMProvider | None = None,
) -> ExtractionRun:
    provider = provider or get_llm_provider()
    user = EXTRACTION_USER_TEMPLATE.format(
        kind=kind,
        title=title or "(untitled)",
        author=author or "(unknown)",
        occurred_at=occurred_at,
        body=body[:25000],
    )
    log.info("extract.call", provider=provider.name, model=provider.model, prompt_version=EXTRACTION_PROMPT_VERSION)
    llm_result = provider.extract(system=EXTRACTION_SYSTEM, user=user, schema=_TOOL_SCHEMA)
    parsed = parse_extraction(llm_result.payload)
    return ExtractionRun(
        result=parsed,
        raw_response=llm_result.raw_response,
        prompt_version=EXTRACTION_PROMPT_VERSION,
        model=llm_result.model,
        input_tokens=llm_result.input_tokens,
        output_tokens=llm_result.output_tokens,
    )
