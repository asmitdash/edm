"""Auth: signed-cookie session over DB-backed users + roles.

The first-run wizard creates the admin user. After that, login goes through
edm.users.authenticate; the session stores user_id + role for middleware checks.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from edm import users as users_repo

Role = Literal["admin", "senior", "junior"]


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("user_id"))


def current_user(request: Request) -> users_repo.User | None:
    uid = request.session.get("user_id")
    if not uid:
        return None
    try:
        return users_repo.get_user(UUID(uid))
    except Exception:
        return None


def require_auth(request: Request) -> RedirectResponse | None:
    if is_authenticated(request):
        return None
    next_url = request.url.path
    return RedirectResponse(url=f"/login?next={next_url}", status_code=303)


def require_role(request: Request, *roles: Role) -> RedirectResponse | None:
    """Return a redirect if not authed; raise 403 if authed but wrong role."""
    redirect = require_auth(request)
    if redirect is not None:
        return redirect
    user = current_user(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if user.role not in roles:
        raise HTTPException(status_code=403, detail=f"role required: {' or '.join(roles)}")
    return None


def login_session(request: Request, user: users_repo.User) -> None:
    request.session["user_id"] = str(user.id)
    request.session["username"] = user.username
    request.session["role"] = user.role


def logout(request: Request) -> None:
    request.session.clear()


def authenticate_user(username: str, password: str) -> users_repo.User | None:
    return users_repo.authenticate(username, password)
