"""Provider abstraction for LLM extraction + verification, and embeddings.

Why this exists:
- The pipeline shape (ingest -> extract -> graph -> reason) is provider-agnostic.
- We want to swap LLMs (Anthropic, Gemini) without touching the pipeline.
- We want a deterministic Stub provider so the offline pytest suite verifies
  pipeline correctness without spending tokens or needing network.

Selection is driven by env vars:
  EDM_LLM_PROVIDER       = anthropic | gemini | stub   (default: stub)
  EDM_EMBEDDING_PROVIDER = voyage | stub               (default: stub)

Each provider returns an `LLMResult` capturing model, raw response, and token
counts for the audit trail.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential

from edm.extract.schemas import ExtractionResult


# ----- shared types ------------------------------------------------------

@dataclass
class LLMResult:
    """Container for any LLM call: parsed payload + audit fields."""
    payload: dict[str, Any]
    raw_response: dict[str, Any]
    model: str
    input_tokens: int
    output_tokens: int


class LLMProvider(Protocol):
    name: str
    model: str

    def extract(self, *, system: str, user: str, schema: dict[str, Any]) -> LLMResult: ...
    def verify(self, *, system: str, user: str, schema: dict[str, Any]) -> LLMResult: ...
    def vision_extract(self, *, image_bytes: bytes, mime_type: str, prompt: str) -> LLMResult: ...


class EmbeddingProvider(Protocol):
    name: str
    dimensions: int

    def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]: ...


# ----- Anthropic provider -----------------------------------------------

class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str) -> None:
        from anthropic import Anthropic

        self._client = Anthropic(api_key=api_key)
        self.model = model

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def _call(self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> LLMResult:
        tool_name = schema["name"]
        response = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            tools=[schema],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": user}],
        )
        tool_use = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_use is None:
            raise RuntimeError(f"Anthropic did not call {tool_name}")
        return LLMResult(
            payload=tool_use.input,
            raw_response=json.loads(response.model_dump_json()),
            model=self.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def extract(self, *, system: str, user: str, schema: dict[str, Any]) -> LLMResult:
        return self._call(system=system, user=user, schema=schema, max_tokens=4096)

    def verify(self, *, system: str, user: str, schema: dict[str, Any]) -> LLMResult:
        return self._call(system=system, user=user, schema=schema, max_tokens=512)

    def vision_extract(self, *, image_bytes: bytes, mime_type: str, prompt: str) -> LLMResult:
        import base64
        b64 = base64.b64encode(image_bytes).decode("ascii")
        response = self._client.messages.create(
            model=self.model,
            max_tokens=2048,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        text_out = "".join(b.text for b in response.content if b.type == "text")
        return LLMResult(
            payload={"text": text_out},
            raw_response=json.loads(response.model_dump_json()),
            model=self.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


# ----- Gemini provider --------------------------------------------------

class GeminiProvider:
    """Google Gemini provider. Uses the new google-genai SDK and the
    Gemini-flavoured 'function calling' / 'structured output' API.

    We pass the same JSON Schema that Anthropic accepts as a tool input_schema.
    Gemini's `response_schema` accepts a near-superset of JSON Schema, so we
    rewrite minimally: drop `enum` constraints onto string types is fine,
    but `oneOf`/`anyOf` aren't accepted -> we lower `["string","null"]` to
    `string` since the LLM can emit empty string.
    """

    name = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        # google-genai is the unified SDK as of late-2024+; google-generativeai works too.
        from google import genai
        from google.genai import types

        self._genai = genai
        self._types = types
        self._client = genai.Client(api_key=api_key)
        self.model = model

    def _to_gemini_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Walk the JSON Schema and lower constructs Gemini doesn't accept."""
        def lower(node: Any) -> Any:
            if isinstance(node, dict):
                t = node.get("type")
                # collapse type unions like ["string","null"] to "string"
                if isinstance(t, list):
                    node = dict(node)
                    node["type"] = next((x for x in t if x != "null"), t[0])
                # Gemini does not allow `additionalProperties` at this level
                node = {k: lower(v) for k, v in node.items() if k != "additionalProperties"}
                return node
            if isinstance(node, list):
                return [lower(x) for x in node]
            return node

        return lower(schema["input_schema"])

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def _call(self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> LLMResult:
        gemini_schema = self._to_gemini_schema(schema)
        cfg = self._types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=gemini_schema,
            max_output_tokens=max_tokens,
            temperature=0.0,
        )
        response = self._client.models.generate_content(
            model=self.model,
            contents=user,
            config=cfg,
        )
        text = response.text or ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Gemini returned non-JSON: {text[:300]}") from e

        usage = getattr(response, "usage_metadata", None)
        in_tok = getattr(usage, "prompt_token_count", 0) or 0
        out_tok = getattr(usage, "candidates_token_count", 0) or 0

        raw = {
            "text": text,
            "candidates": [
                {"finish_reason": getattr(c, "finish_reason", None)}
                for c in (response.candidates or [])
            ],
            "model": self.model,
        }
        return LLMResult(
            payload=payload,
            raw_response=raw,
            model=self.model,
            input_tokens=int(in_tok),
            output_tokens=int(out_tok),
        )

    def extract(self, *, system: str, user: str, schema: dict[str, Any]) -> LLMResult:
        return self._call(system=system, user=user, schema=schema, max_tokens=4096)

    def verify(self, *, system: str, user: str, schema: dict[str, Any]) -> LLMResult:
        return self._call(system=system, user=user, schema=schema, max_tokens=512)

    def vision_extract(self, *, image_bytes: bytes, mime_type: str, prompt: str) -> LLMResult:
        part = self._types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        cfg = self._types.GenerateContentConfig(
            max_output_tokens=2048,
            temperature=0.0,
        )
        response = self._client.models.generate_content(
            model=self.model,
            contents=[prompt, part],
            config=cfg,
        )
        text_out = response.text or ""
        usage = getattr(response, "usage_metadata", None)
        return LLMResult(
            payload={"text": text_out},
            raw_response={"text": text_out, "model": self.model},
            model=self.model,
            input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
        )


# ----- OpenAI provider --------------------------------------------------

class OpenAIProvider:
    """OpenAI provider using `response_format` JSON Schema."""

    name = "openai"

    def __init__(self, api_key: str, model: str, *, base_url: str | None = None, name: str = "openai") -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
        self.model = model
        self.name = name  # type: ignore[assignment]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def _call(self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> LLMResult:
        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=0.0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema["name"],
                    "schema": schema["input_schema"],
                    "strict": False,
                },
            },
        )
        text_out = response.choices[0].message.content or ""
        try:
            payload = json.loads(text_out)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"OpenAI returned non-JSON: {text_out[:300]}") from e
        usage = response.usage
        return LLMResult(
            payload=payload,
            raw_response={"id": response.id, "model": response.model, "text": text_out},
            model=self.model,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )

    def extract(self, *, system: str, user: str, schema: dict[str, Any]) -> LLMResult:
        return self._call(system=system, user=user, schema=schema, max_tokens=4096)

    def verify(self, *, system: str, user: str, schema: dict[str, Any]) -> LLMResult:
        return self._call(system=system, user=user, schema=schema, max_tokens=512)

    def vision_extract(self, *, image_bytes: bytes, mime_type: str, prompt: str) -> LLMResult:
        import base64
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{mime_type};base64,{b64}"
        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=2048,
            temperature=0.0,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
        )
        text_out = response.choices[0].message.content or ""
        usage = response.usage
        return LLMResult(
            payload={"text": text_out},
            raw_response={"id": response.id, "model": response.model, "text": text_out},
            model=self.model,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter is OpenAI-compatible at /api/v1. Falls back to JSON-mode when
    the underlying model lacks strict schema support."""

    def __init__(self, api_key: str, model: str) -> None:
        super().__init__(api_key=api_key, model=model, base_url="https://openrouter.ai/api/v1", name="openrouter")


# ----- Stub LLM provider (offline tests) --------------------------------

class StubLLMProvider:
    """Deterministic, offline LLM. Pattern-matches the source body to produce
    plausible structured output. The pipeline tests only that wiring is correct,
    not that LLMs are smart."""

    name = "stub"
    model = "stub-llm-v1"

    def __init__(self, fixture_path: str | None = None) -> None:
        # Optional override: a JSON file mapping `source_external_id` -> ExtractionResult dict.
        self._fixtures: dict[str, dict] = {}
        if fixture_path and os.path.exists(fixture_path):
            with open(fixture_path) as f:
                self._fixtures = json.load(f)

    def _hash(self, s: str) -> str:
        return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]

    def _stub_extract_payload(self, user: str) -> dict[str, Any]:
        # Look for an explicit fixture key embedded in the prompt.
        for key, payload in self._fixtures.items():
            if key in user:
                return payload

        body_lc = user.lower()
        decisions: list[dict] = []
        assumptions: list[dict] = []
        constraints: list[dict] = []
        alternatives: list[dict] = []
        edges: list[dict] = []

        # Heuristic: if "postgres" + "primary" or "oltp" appears, emit a Postgres adoption decision.
        if "postgres" in body_lc and ("primary" in body_lc or "oltp" in body_lc):
            decisions.append({
                "local_id": "d1",
                "title": "Adopt Postgres as primary OLTP store",
                "summary": "The project will use Postgres for its primary transactional store across services.",
                "quoted_evidence": "We will use Postgres",
            })
            if "p99" in body_lc or "100ms" in body_lc:
                constraints.append({
                    "local_id": "c1",
                    "statement": "Read-path P99 must stay under 100ms at projected peak.",
                    "kind": "latency",
                    "quoted_evidence": "P99 < 100ms",
                })
                edges.append({"src_local_id": "d1", "dst_local_id": "c1", "relation": "motivated_by", "confidence": 0.9})
            if "5k tps" in body_lc or "write throughput" in body_lc:
                assumptions.append({
                    "local_id": "a1",
                    "statement": "Write throughput will not exceed 5k tps in the near term.",
                    "quoted_evidence": "write throughput will not exceed 5k tps",
                })
                edges.append({"src_local_id": "d1", "dst_local_id": "a1", "relation": "depends_on", "confidence": 0.9})
            if "mongodb" in body_lc:
                alternatives.append({
                    "local_id": "alt1",
                    "decision_local_id": "d1",
                    "description": "MongoDB",
                    "rejection_reason": "weaker transactional semantics for financial workflows",
                })
                edges.append({"src_local_id": "d1", "dst_local_id": "alt1", "relation": "considered", "confidence": 1.0})

        elif "mongodb" in body_lc and ("migrat" in body_lc or "move" in body_lc):
            decisions.append({
                "local_id": "d1",
                "title": "Migrate user-events service to MongoDB",
                "summary": "The user-events service will move from Postgres to MongoDB to handle write spikes.",
                "quoted_evidence": "Migrating the user-events service from Postgres to MongoDB",
            })
            if "12k tps" in body_lc or "spike" in body_lc:
                assumptions.append({
                    "local_id": "a1",
                    "statement": "Write throughput will exceed 5k tps and spike to ~12k tps during launches.",
                    "quoted_evidence": "spike to ~12k tps",
                })
                edges.append({"src_local_id": "d1", "dst_local_id": "a1", "relation": "depends_on", "confidence": 0.9})

        return {
            "decisions": decisions,
            "assumptions": assumptions,
            "constraints": constraints,
            "alternatives": alternatives,
            "edges": edges,
            "notes": None,
        }

    def extract(self, *, system: str, user: str, schema: dict[str, Any]) -> LLMResult:
        payload = self._stub_extract_payload(user)
        return LLMResult(
            payload=payload,
            raw_response={"stub": True, "input_hash": self._hash(user)},
            model=self.model,
            input_tokens=len(user) // 4,
            output_tokens=len(json.dumps(payload)) // 4,
        )

    def verify(self, *, system: str, user: str, schema: dict[str, Any]) -> LLMResult:
        # If both 'Postgres' and 'MongoDB' titles appear, declare a contradiction.
        u = user.lower()
        contradicts = ("postgres" in u and "mongodb" in u)
        payload = {
            "contradicts": contradicts,
            "severity": "high" if contradicts else "low",
            "rationale": (
                "The new decision migrates a service away from Postgres to MongoDB, "
                "directly conflicting with the prior decision to standardize on Postgres "
                "as the primary OLTP store."
            ) if contradicts else "Topics overlap but no actual conflict.",
        }
        return LLMResult(
            payload=payload,
            raw_response={"stub": True, "input_hash": self._hash(user)},
            model=self.model,
            input_tokens=len(user) // 4,
            output_tokens=64,
        )

    def vision_extract(self, *, image_bytes: bytes, mime_type: str, prompt: str) -> LLMResult:
        text_out = (
            "STUB: Architecture diagram. Components observed: Service A, Service B, Postgres. "
            "Edges suggest Service A depends_on Postgres."
        )
        return LLMResult(
            payload={"text": text_out},
            raw_response={"stub": True, "bytes": len(image_bytes)},
            model=self.model,
            input_tokens=128,
            output_tokens=64,
        )


# ----- Embedding providers ----------------------------------------------

class VoyageEmbedder:
    name = "voyage"
    dimensions = 1024

    def __init__(self, api_key: str, model: str) -> None:
        import voyageai

        self._client = voyageai.Client(api_key=api_key)
        self.model = model

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
        if not texts:
            return []
        result = self._client.embed(texts, model=self.model, input_type=input_type)
        return result.embeddings


class StubEmbedder:
    """Deterministic embeddings for offline tests. Two texts that share keywords
    end up with similar vectors so the contradiction-detector retrieval step
    actually surfaces real candidates."""

    name = "stub"
    dimensions = 1024

    def __init__(self) -> None:
        self.model = "stub-emb-v1"

    def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
        return [_text_to_vec(t, self.dimensions) for t in texts]


def _text_to_vec(text: str, dim: int) -> list[float]:
    """Hash-based bag-of-words embedding. Deterministic, no learning,
    but words map to the same dimensions across calls — so semantically
    overlapping texts have non-trivial cosine similarity."""
    vec = [0.0] * dim
    for token in _tokenize(text):
        h = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16)
        idx = h % dim
        sign = 1.0 if (h >> 32) & 1 else -1.0
        vec[idx] += sign
    # L2 normalize so cosine similarity is the dot product.
    n = sum(x * x for x in vec) ** 0.5
    if n == 0:
        return vec
    return [x / n for x in vec]


def _tokenize(text: str) -> list[str]:
    out = []
    cur = []
    for ch in text.lower():
        if ch.isalnum():
            cur.append(ch)
        else:
            if cur:
                out.append("".join(cur))
                cur = []
    if cur:
        out.append("".join(cur))
    return out


# ----- Factories --------------------------------------------------------

def _provider_name(env_key: str, default: str) -> str:
    """Resolve a provider name. Reads from os.environ first (so tests can
    pin via setdefault before edm imports), then falls back to .env via
    pydantic-settings, then the default."""
    if env_key in os.environ:
        return os.environ[env_key].lower()

    # Trigger .env load through pydantic-settings even if no env var is set.
    from edm.config import get_settings

    get_settings()
    return os.environ.get(env_key, default).lower()


def _ensure_dotenv_loaded() -> None:
    """If config.Settings hasn't been instantiated, doing so now loads .env into
    os.environ via pydantic-settings (extra=ignore means it loads everything)."""
    from edm.config import get_settings

    get_settings()
    # pydantic-settings does NOT push .env vars back into os.environ. So do it
    # ourselves for the keys the factories need.
    if not os.environ.get("EDM_LLM_PROVIDER") or not os.environ.get("EDM_EMBEDDING_PROVIDER"):
        from dotenv import load_dotenv

        load_dotenv(override=False)


def build_provider(provider: str, *, api_key: str, model: str) -> LLMProvider:
    """Pure factory: build by explicit args. Used by the setup wizard to
    validate a key before persisting."""
    p = provider.lower()
    if p == "anthropic":
        return AnthropicProvider(api_key=api_key, model=model)
    if p == "gemini":
        return GeminiProvider(api_key=api_key, model=model)
    if p == "openai":
        return OpenAIProvider(api_key=api_key, model=model)
    if p == "openrouter":
        return OpenRouterProvider(api_key=api_key, model=model)
    if p == "stub":
        return StubLLMProvider(fixture_path=os.environ.get("EDM_LLM_STUB_FIXTURES"))
    raise ValueError(f"Unknown LLM provider: {provider}")


def get_llm_provider() -> LLMProvider:
    """Resolve the active provider. Order:
    1. EDM_LLM_PROVIDER=stub -> stub (used by tests)
    2. DB-stored install config (set via the setup wizard)
    3. Env vars (legacy fallback for CLI / cron)
    """
    _ensure_dotenv_loaded()
    if (os.environ.get("EDM_LLM_PROVIDER") or "").lower() == "stub":
        return StubLLMProvider(fixture_path=os.environ.get("EDM_LLM_STUB_FIXTURES"))

    try:
        from edm.install_config import get_llm_config
        cfg = get_llm_config()
        if cfg.provider and cfg.api_key and cfg.model:
            return build_provider(cfg.provider, api_key=cfg.api_key, model=cfg.model)
    except Exception:
        pass  # schema may not exist yet (pre-setup); fall through to env

    from edm.config import get_settings
    s = get_settings()
    env_name = (os.environ.get("EDM_LLM_PROVIDER") or "").lower()
    if env_name == "anthropic":
        return AnthropicProvider(api_key=s.anthropic_api_key or "", model=s.anthropic_model)
    if env_name == "gemini":
        return GeminiProvider(api_key=s.gemini_api_key or "", model=s.gemini_model)
    if env_name == "openai":
        return OpenAIProvider(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        )
    if env_name == "openrouter":
        return OpenRouterProvider(
            api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            model=os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
        )
    raise RuntimeError(
        "No LLM provider configured. Run `edm setup` or visit /setup in the UI, "
        "or set EDM_LLM_PROVIDER."
    )


def get_embedding_provider() -> EmbeddingProvider:
    _ensure_dotenv_loaded()
    from edm.config import get_settings

    s = get_settings()
    name = (os.environ.get("EDM_EMBEDDING_PROVIDER") or "stub").lower()
    if name == "voyage":
        return VoyageEmbedder(api_key=s.voyage_api_key or "", model=s.voyage_model)
    if name == "stub":
        return StubEmbedder()
    raise ValueError(f"Unknown EDM_EMBEDDING_PROVIDER: {name}")


# ----- Validation helper ------------------------------------------------

def parse_extraction(payload: dict[str, Any]) -> ExtractionResult:
    try:
        return ExtractionResult.model_validate(payload)
    except ValidationError as e:
        raise RuntimeError(f"Extraction payload failed schema validation: {e}") from e
