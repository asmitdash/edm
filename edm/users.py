"""Users + invites repository. Single source of truth for credential checks
and role assignments."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import text
from sqlalchemy.orm import Session

from edm.db import session_scope

Role = Literal["admin", "senior", "junior"]
_hasher = PasswordHasher()


@dataclass
class User:
    id: UUID
    username: str
    role: Role
    active: bool
    must_change_password: bool


def _row_to_user(r) -> User:
    return User(
        id=r["id"],
        username=r["username"],
        role=r["role"],
        active=r["active"],
        must_change_password=r["must_change_password"],
    )


def has_any_user() -> bool:
    with session_scope() as s:
        return s.execute(text("SELECT EXISTS(SELECT 1 FROM users)")).scalar_one()


def has_active_admin() -> bool:
    with session_scope() as s:
        return s.execute(
            text("SELECT EXISTS(SELECT 1 FROM users WHERE role='admin' AND active=true)")
        ).scalar_one()


def create_user(username: str, password: str, role: Role, must_change: bool = False) -> User:
    pw_hash = _hasher.hash(password)
    with session_scope() as s:
        row = s.execute(
            text(
                """
                INSERT INTO users (username, password_hash, role, must_change_password)
                VALUES (:u, :p, :r, :mc)
                RETURNING id, username, role, active, must_change_password
                """
            ),
            {"u": username, "p": pw_hash, "r": role, "mc": must_change},
        ).mappings().one()
    return _row_to_user(row)


def authenticate(username: str, password: str) -> User | None:
    with session_scope() as s:
        row = s.execute(
            text(
                "SELECT id, username, role, active, must_change_password, password_hash "
                "FROM users WHERE username = :u AND active = true"
            ),
            {"u": username},
        ).mappings().first()
        if row is None:
            try:
                _hasher.verify(_hasher.hash("decoy"), "wrong")
            except VerifyMismatchError:
                pass
            return None
        try:
            _hasher.verify(row["password_hash"], password)
        except VerifyMismatchError:
            return None
        s.execute(text("UPDATE users SET last_login_at = now() WHERE id = :id"), {"id": row["id"]})
        return _row_to_user(row)


def change_password(user_id: UUID, new_password: str) -> None:
    pw_hash = _hasher.hash(new_password)
    with session_scope() as s:
        s.execute(
            text(
                "UPDATE users SET password_hash = :p, must_change_password = false WHERE id = :id"
            ),
            {"p": pw_hash, "id": user_id},
        )


def get_user(user_id: UUID) -> User | None:
    with session_scope() as s:
        row = s.execute(
            text(
                "SELECT id, username, role, active, must_change_password "
                "FROM users WHERE id = :id"
            ),
            {"id": user_id},
        ).mappings().first()
    return _row_to_user(row) if row else None


def list_users() -> list[User]:
    with session_scope() as s:
        rows = s.execute(
            text("SELECT id, username, role, active, must_change_password FROM users ORDER BY role, username")
        ).mappings().all()
    return [_row_to_user(r) for r in rows]


def deactivate_user(user_id: UUID) -> None:
    with session_scope() as s:
        s.execute(text("UPDATE users SET active = false WHERE id = :id"), {"id": user_id})


# ---------- Invites ----------

@dataclass
class Invite:
    id: UUID
    token: str
    role: Role
    expires_at: datetime
    redeemed_by: UUID | None
    revoked_at: datetime | None
    note: str | None


def create_invite(*, created_by: UUID, role: Role, hours: int = 48, note: str | None = None) -> Invite:
    if role not in ("senior", "junior"):
        raise ValueError("invites only support senior or junior")
    token = secrets.token_urlsafe(24)
    expires = datetime.now(timezone.utc) + timedelta(hours=hours)
    with session_scope() as s:
        row = s.execute(
            text(
                """
                INSERT INTO invites (token, role, created_by, expires_at, note)
                VALUES (:t, :r, :c, :e, :n)
                RETURNING id, token, role, expires_at, redeemed_by, revoked_at, note
                """
            ),
            {"t": token, "r": role, "c": created_by, "e": expires, "n": note},
        ).mappings().one()
    return Invite(**dict(row))


def get_invite_by_token(token: str) -> Invite | None:
    with session_scope() as s:
        row = s.execute(
            text(
                "SELECT id, token, role, expires_at, redeemed_by, revoked_at, note "
                "FROM invites WHERE token = :t"
            ),
            {"t": token},
        ).mappings().first()
    return Invite(**dict(row)) if row else None


def redeem_invite(token: str, *, username: str, password: str) -> User:
    invite = get_invite_by_token(token)
    if invite is None:
        raise ValueError("invalid invite")
    if invite.revoked_at is not None:
        raise ValueError("invite revoked")
    if invite.redeemed_by is not None:
        raise ValueError("invite already used")
    if invite.expires_at < datetime.now(timezone.utc):
        raise ValueError("invite expired")
    user = create_user(username, password, invite.role)
    with session_scope() as s:
        s.execute(
            text("UPDATE invites SET redeemed_by = :u, redeemed_at = now() WHERE id = :id"),
            {"u": user.id, "id": invite.id},
        )
    return user


def list_invites() -> list[Invite]:
    with session_scope() as s:
        rows = s.execute(
            text(
                "SELECT id, token, role, expires_at, redeemed_by, revoked_at, note "
                "FROM invites ORDER BY expires_at DESC LIMIT 100"
            )
        ).mappings().all()
    return [Invite(**dict(r)) for r in rows]
