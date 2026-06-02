"""Chatbot side of the SOP intake.

Two LLM calls per user turn:
  1. EXTRACT — pull DerivedExceptions from the latest user message
  2. ASK    — produce the next clarifying question, given the current form +
              accumulated exceptions. It also signals when intake is "done".

Both run through the same LLMProvider abstraction so swapping Gemini ↔ Claude
needs no changes here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from edm.extract.providers import LLMProvider, get_llm_provider
from edm.sop import sessions as session_repo
from edm.sop.schemas import DerivedException


_EXTRACT_SCHEMA: dict[str, Any] = {
    "name": "extract_exceptions",
    "description": "Extract structured exceptions / conditional rules from the user's last message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "exceptions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "when": {"type": "string"},
                        "then": {"type": "string"},
                        "severity": {
                            "type": "string",
                            "enum": ["info", "low", "medium", "high"],
                        },
                    },
                    "required": ["when", "then"],
                },
            }
        },
        "required": ["exceptions"],
    },
}

_ASK_SCHEMA: dict[str, Any] = {
    "name": "ask_or_finish",
    "description": "Either ask the next clarifying question or signal intake is complete.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_done": {
                "type": "boolean",
                "description": "True only if you have enough to write a complete SOP.",
            },
            "next_question": {
                "type": "string",
                "description": "Conversational, single question. Empty when is_done=true.",
            },
            "rationale": {
                "type": "string",
                "description": "Why this question (or why we are done).",
            },
        },
        "required": ["is_done", "next_question"],
    },
}


_EXTRACT_SYSTEM = """\
You are extracting EXCEPTIONS from a user message during SOP intake.

An EXCEPTION is a conditional rule the SOP must encode. Examples:
  user: "uniforms are required, but on Fridays casuals are OK as long as no black"
   ->  [{"when":"every day except Fridays","then":"uniform required","severity":"medium"},
        {"when":"on Fridays","then":"casuals allowed but no black clothes","severity":"low"}]

  user: "refunds over $500 need manager approval"
   ->  [{"when":"refund amount > $500","then":"manager approval required","severity":"high"}]

If the message contains no exceptions, return {"exceptions": []}.
Use the `extract_exceptions` tool to respond.
"""


_ASK_SYSTEM = """\
You are an SOP intake assistant. Your job is to gather every detail needed to \
write a thorough, defensible SOP — covering the standard flow plus exceptions, \
edge cases, escalation, and compliance.

Rules:
1. Ask ONE question at a time.
2. Pull on threads the user already mentioned. If they said "uniforms compulsory", \
   probe for exceptions before moving on.
3. Probe for: standard flow, exceptions / conditionals, who is in charge, when to \
   escalate, what tools / systems are used, frequency, compliance constraints.
4. Avoid repeating questions whose answers are already in the form data or chat history.
5. When you have enough to write the SOP — and ONLY then — set is_done=true and stop.
6. Keep the question short, conversational, and specific.

Output via the `ask_or_finish` tool only.
"""


@dataclass
class TurnResult:
    extracted_exceptions: list[DerivedException]
    next_question: str
    is_done: bool
    rationale: str | None


def _format_history(messages: list[dict], limit: int = 20) -> str:
    tail = messages[-limit:]
    out = []
    for m in tail:
        out.append(f"{m['role'].upper()}: {m['content']}")
    return "\n".join(out)


def _format_form(form_data: dict) -> str:
    if not form_data:
        return "(form not filled yet)"
    return json.dumps(form_data, indent=2)


def _format_exceptions(exceptions: list[dict]) -> str:
    if not exceptions:
        return "(none yet)"
    return "\n".join(f"- WHEN {e['when']} -> THEN {e['then']} ({e.get('severity','medium')})" for e in exceptions)


def take_user_turn(
    session_id: UUID,
    user_message: str,
    *,
    provider: LLMProvider | None = None,
) -> TurnResult:
    """Append the user message, run EXTRACT + ASK, persist the assistant reply."""
    provider = provider or get_llm_provider()

    sess = session_repo.get_session(session_id)
    if sess is None:
        raise ValueError(f"session {session_id} not found")

    session_repo.append_chat_message(session_id, role="user", content=user_message)

    # 1) extract exceptions from the new user message in isolation
    extract_user = (
        f"Form data so far:\n{_format_form(sess.form_data)}\n\n"
        f"Existing exceptions:\n{_format_exceptions(sess.derived_exceptions)}\n\n"
        f"USER MESSAGE:\n{user_message}"
    )
    extract_llm = provider.extract(system=_EXTRACT_SYSTEM, user=extract_user, schema=_EXTRACT_SCHEMA)
    new_exceptions: list[DerivedException] = []
    for raw in extract_llm.payload.get("exceptions", []):
        try:
            new_exceptions.append(DerivedException.model_validate(raw))
        except ValidationError:
            continue

    if new_exceptions:
        session_repo.add_derived_exceptions(session_id, new_exceptions)

    # 2) decide next question (or finish)
    sess = session_repo.get_session(session_id)  # refresh
    history = session_repo.get_messages(session_id)
    ask_user = (
        f"Form data:\n{_format_form(sess.form_data)}\n\n"
        f"Accumulated exceptions:\n{_format_exceptions(sess.derived_exceptions)}\n\n"
        f"Recent conversation:\n{_format_history(history)}"
    )
    ask_llm = provider.extract(system=_ASK_SYSTEM, user=ask_user, schema=_ASK_SCHEMA)
    is_done = bool(ask_llm.payload.get("is_done"))
    next_q = (ask_llm.payload.get("next_question") or "").strip()
    rationale = ask_llm.payload.get("rationale")

    if is_done:
        assistant_msg = (
            "I have enough to draft the SOP now. Click 'Generate SOP' when you're ready, "
            "or keep talking if there's more context to add."
        )
        session_repo.append_chat_message(
            session_id, role="assistant", content=assistant_msg,
            metadata={"is_done": True, "rationale": rationale},
        )
        session_repo.set_status(session_id, "ready_to_generate")
    else:
        if not next_q:
            next_q = "Anything else worth capturing before I draft the SOP?"
        session_repo.append_chat_message(
            session_id, role="assistant", content=next_q,
            metadata={"is_done": False, "rationale": rationale},
        )

    return TurnResult(
        extracted_exceptions=new_exceptions,
        next_question="" if is_done else next_q,
        is_done=is_done,
        rationale=rationale,
    )


def kickoff_message(session_id: UUID, *, provider: LLMProvider | None = None) -> str:
    """Generate the assistant's opening question after form intake."""
    provider = provider or get_llm_provider()
    sess = session_repo.get_session(session_id)
    if sess is None:
        raise ValueError("session not found")

    user = (
        f"Form data:\n{_format_form(sess.form_data)}\n\n"
        f"Existing exceptions:\n{_format_exceptions(sess.derived_exceptions)}\n\n"
        "No conversation has happened yet. Open the conversation with one specific, "
        "useful question to start gathering exceptions and edge cases."
    )
    res = provider.extract(system=_ASK_SYSTEM, user=user, schema=_ASK_SCHEMA)
    q = (res.payload.get("next_question") or "").strip()
    if not q:
        q = (
            "Walk me through the standard flow first — what's the trigger, who runs it, "
            "and what's the expected outcome?"
        )
    session_repo.append_chat_message(
        session_id, role="assistant", content=q,
        metadata={"is_done": False, "rationale": "kickoff"},
    )
    return q
