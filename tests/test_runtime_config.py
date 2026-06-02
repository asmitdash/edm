"""Runtime config + auto_setup tests (no DB required for these)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def test_runtime_config_roundtrip(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("EDM_RUNTIME_DIR", td)
        from edm import runtime_config
        # fresh module-state — no caching; functions read the env each call
        runtime_config.update(database_url="postgresql+psycopg://x:y@h/d", embedding_provider="stub")
        data = runtime_config.load()
        assert data["database_url"].startswith("postgresql+psycopg://")
        assert data["embedding_provider"] == "stub"

        # ensure_session_secret is stable across calls
        s1 = runtime_config.ensure_session_secret()
        s2 = runtime_config.ensure_session_secret()
        assert s1 == s2 and len(s1) >= 32


def test_normalize_database_url():
    from edm.auto_setup import normalize_database_url

    assert normalize_database_url("postgresql://u:p@h:5432/d").startswith("postgresql+psycopg://")
    assert normalize_database_url("postgresql+psycopg://u:p@h/d").startswith("postgresql+psycopg://")

    import pytest
    with pytest.raises(ValueError):
        normalize_database_url("mysql://u:p@h/d")
