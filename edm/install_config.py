"""Install-level config (single-row tables for LLM + GitHub).

Encapsulates token encryption / decryption so callers only see plaintext at the
boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import text

from edm.crypto import decrypt, encrypt
from edm.db import session_scope

LLMProviderName = Literal["openrouter", "openai", "gemini", "anthropic"]


@dataclass
class LLMConfig:
    provider: LLMProviderName | None
    model: str | None
    api_key: str | None
    configured_by: UUID | None
    configured_at: datetime | None
    last_validated_at: datetime | None
    last_validation_status: str | None


@dataclass
class GitHubInstall:
    github_login: str | None
    org_login: str | None
    repo_full: str | None
    access_token: str | None
    scope: str | None
    connected_by: UUID | None
    connected_at: datetime | None
    last_validated_at: datetime | None
    last_validation_status: str | None


def get_llm_config() -> LLMConfig:
    with session_scope() as s:
        row = s.execute(
            text(
                """
                SELECT provider, model, api_key_enc, configured_by, configured_at,
                       last_validated_at, last_validation_status
                FROM llm_config WHERE id = 1
                """
            )
        ).mappings().one()
    api_key = decrypt(row["api_key_enc"]) if row["api_key_enc"] else None
    return LLMConfig(
        provider=row["provider"],
        model=row["model"],
        api_key=api_key,
        configured_by=row["configured_by"],
        configured_at=row["configured_at"],
        last_validated_at=row["last_validated_at"],
        last_validation_status=row["last_validation_status"],
    )


def save_llm_config(*, provider: LLMProviderName, model: str, api_key: str, configured_by: UUID, status: str = "ok") -> None:
    enc = encrypt(api_key)
    with session_scope() as s:
        s.execute(
            text(
                """
                UPDATE llm_config
                   SET provider = :p, model = :m, api_key_enc = :k,
                       configured_by = :u, configured_at = now(),
                       last_validated_at = now(), last_validation_status = :st
                 WHERE id = 1
                """
            ),
            {"p": provider, "m": model, "k": enc, "u": configured_by, "st": status},
        )


def get_github_install() -> GitHubInstall:
    with session_scope() as s:
        row = s.execute(
            text(
                """
                SELECT github_login, org_login, repo_full, access_token_enc, scope,
                       connected_by, connected_at, last_validated_at, last_validation_status
                FROM github_install WHERE id = 1
                """
            )
        ).mappings().one()
    token = decrypt(row["access_token_enc"]) if row["access_token_enc"] else None
    return GitHubInstall(
        github_login=row["github_login"],
        org_login=row["org_login"],
        repo_full=row["repo_full"],
        access_token=token,
        scope=row["scope"],
        connected_by=row["connected_by"],
        connected_at=row["connected_at"],
        last_validated_at=row["last_validated_at"],
        last_validation_status=row["last_validation_status"],
    )


def save_github_install(
    *,
    github_login: str,
    access_token: str,
    scope: str,
    org_login: str | None,
    repo_full: str | None,
    connected_by: UUID,
    status: str = "ok",
) -> None:
    enc = encrypt(access_token)
    with session_scope() as s:
        s.execute(
            text(
                """
                UPDATE github_install
                   SET github_login = :gl, access_token_enc = :tok, scope = :sc,
                       org_login = :org, repo_full = :rf,
                       connected_by = :u, connected_at = now(),
                       last_validated_at = now(), last_validation_status = :st
                 WHERE id = 1
                """
            ),
            {
                "gl": github_login, "tok": enc, "sc": scope,
                "org": org_login, "rf": repo_full,
                "u": connected_by, "st": status,
            },
        )


def disconnect_github() -> None:
    with session_scope() as s:
        s.execute(text(
            """
            UPDATE github_install
               SET github_login=NULL, org_login=NULL, repo_full=NULL,
                   access_token_enc=NULL, scope=NULL, connected_by=NULL,
                   connected_at=NULL, last_validated_at=NULL, last_validation_status=NULL
             WHERE id=1
            """
        ))
