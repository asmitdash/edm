"""Embeddings facade. Delegates to whichever EmbeddingProvider is active."""

from __future__ import annotations

from edm.extract.providers import EmbeddingProvider, get_embedding_provider


_provider: EmbeddingProvider | None = None


def _get() -> EmbeddingProvider:
    global _provider
    if _provider is None:
        _provider = get_embedding_provider()
    return _provider


def embed_texts(texts: list[str], *, input_type: str = "document") -> list[list[float]]:
    if not texts:
        return []
    return _get().embed(texts, input_type=input_type)


def embed_one(text: str, *, input_type: str = "document") -> list[float]:
    return embed_texts([text], input_type=input_type)[0]


def reset_for_tests() -> None:
    """Reset the singleton so tests can swap providers between cases."""
    global _provider
    _provider = None
