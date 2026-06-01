"""Sanity tests for the stub provider's heuristic outputs.

These guard against regressions in the stubs themselves — the offline pipeline
test depends on the stub returning specific shapes for specific inputs.
"""

from __future__ import annotations

from edm.extract.providers import StubLLMProvider, StubEmbedder, parse_extraction


def test_stub_extract_recognises_postgres_pr():
    p = StubLLMProvider()
    user = (
        "Source kind: pr\nSource title: Adopt Postgres for primary OLTP\n\n"
        "We will use Postgres as the primary OLTP store. P99 < 100ms. "
        "Write throughput will not exceed 5k tps. Considered MongoDB."
    )
    result = p.extract(system="...", user=user, schema={"name": "x", "input_schema": {}})
    parsed = parse_extraction(result.payload)
    assert len(parsed.decisions) == 1
    assert parsed.decisions[0].local_id == "d1"
    assert len(parsed.assumptions) == 1
    assert len(parsed.constraints) == 1
    assert any(e.relation == "depends_on" for e in parsed.edges)
    assert any(e.relation == "considered" for e in parsed.edges)


def test_stub_extract_recognises_mongo_migration():
    p = StubLLMProvider()
    user = "Migrating the user-events service from Postgres to MongoDB. spike to ~12k tps."
    result = p.extract(system="...", user=user, schema={"name": "x", "input_schema": {}})
    parsed = parse_extraction(result.payload)
    assert len(parsed.decisions) == 1
    assert "MongoDB" in parsed.decisions[0].title


def test_stub_verify_flags_postgres_vs_mongo_as_contradiction():
    p = StubLLMProvider()
    prompt = "PRIOR: Adopt Postgres ... NEW: Move user-events to MongoDB ..."
    result = p.verify(system="...", user=prompt, schema={"name": "x", "input_schema": {}})
    assert result.payload["contradicts"] is True
    assert result.payload["severity"] == "high"


def test_stub_verify_returns_no_contradiction_for_unrelated_text():
    p = StubLLMProvider()
    result = p.verify(system="...", user="totally unrelated content", schema={"name": "x", "input_schema": {}})
    assert result.payload["contradicts"] is False


def test_stub_embedder_is_deterministic_and_normalised():
    e = StubEmbedder()
    v1, v2 = e.embed(["postgres oltp store", "postgres oltp store"])
    assert v1 == v2
    norm = sum(x * x for x in v1) ** 0.5
    assert abs(norm - 1.0) < 1e-6 or norm == 0.0


def test_stub_embedder_overlapping_text_has_higher_similarity_than_disjoint():
    e = StubEmbedder()
    a, b, c = e.embed([
        "we adopt postgres as the primary oltp store",
        "postgres oltp store choice for primary services",
        "kittens are great and have nothing to do with databases",
    ])
    sim_ab = sum(x * y for x, y in zip(a, b))
    sim_ac = sum(x * y for x, y in zip(a, c))
    assert sim_ab > sim_ac
