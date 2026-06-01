"""GitHub OAuth Device Flow + org/member fetch.

Device Flow is the right CLI-native auth pattern: we ask GitHub for a device
code; the user opens github.com/login/device and types it in; we poll until
authorized; we get a user-token. No client secret required for public OAuth
Apps in device flow, but the App ID does need to be registered (we use
GitHub's published 'gh' CLI client_id by default since that App is widely
trusted, but allow override via env).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator

import httpx

from edm.config import get_settings


# The official `gh` CLI client_id. Publicly known, no secret needed for
# device flow. Override by setting EDM_GITHUB_CLIENT_ID for a custom app.
_DEFAULT_CLIENT_ID = "178c6fc778ccc68e1d6a"


@dataclass
class DeviceCode:
    device_code: str
    user_code: str           # what the human types into github.com/login/device
    verification_uri: str
    expires_in: int
    interval: int            # min seconds between polls


@dataclass
class TokenResponse:
    access_token: str
    scope: str
    token_type: str


def _client_id() -> str:
    import os
    return os.environ.get("EDM_GITHUB_CLIENT_ID") or _DEFAULT_CLIENT_ID


def request_device_code(scope: str = "read:org repo") -> DeviceCode:
    """Step 1: ask GitHub for a device code."""
    resp = httpx.post(
        "https://github.com/login/device/code",
        headers={"Accept": "application/json"},
        data={"client_id": _client_id(), "scope": scope},
        timeout=15.0,
    )
    resp.raise_for_status()
    j = resp.json()
    return DeviceCode(
        device_code=j["device_code"],
        user_code=j["user_code"],
        verification_uri=j.get("verification_uri") or j.get("verification_uri_complete") or "https://github.com/login/device",
        expires_in=int(j.get("expires_in", 900)),
        interval=int(j.get("interval", 5)),
    )


def poll_for_token(device: DeviceCode, *, max_wait: int | None = None) -> TokenResponse:
    """Step 2: poll until the user authorizes (or we time out)."""
    deadline = time.monotonic() + (max_wait or device.expires_in)
    interval = device.interval
    while time.monotonic() < deadline:
        resp = httpx.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": _client_id(),
                "device_code": device.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=15.0,
        )
        j = resp.json()
        err = j.get("error")
        if not err and "access_token" in j:
            return TokenResponse(
                access_token=j["access_token"],
                scope=j.get("scope", ""),
                token_type=j.get("token_type", "bearer"),
            )
        if err == "authorization_pending":
            time.sleep(interval)
            continue
        if err == "slow_down":
            interval += 5
            time.sleep(interval)
            continue
        if err == "expired_token":
            raise TimeoutError("Device code expired before user authorized")
        if err == "access_denied":
            raise PermissionError("User denied the authorization request")
        # Unknown error
        raise RuntimeError(f"GitHub error: {err}: {j}")
    raise TimeoutError("Device flow polling timed out")


def poll_for_token_iter(device: DeviceCode) -> Iterator[TokenResponse | None]:
    """Generator variant: yields None while pending, the token when ready.
    Lets callers (web wizard) interleave with their own UI updates."""
    deadline = time.monotonic() + device.expires_in
    interval = device.interval
    while time.monotonic() < deadline:
        resp = httpx.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": _client_id(),
                "device_code": device.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=15.0,
        )
        j = resp.json()
        err = j.get("error")
        if not err and "access_token" in j:
            yield TokenResponse(
                access_token=j["access_token"],
                scope=j.get("scope", ""),
                token_type=j.get("token_type", "bearer"),
            )
            return
        if err in ("authorization_pending", "slow_down"):
            yield None
            time.sleep(interval)
            continue
        raise RuntimeError(f"GitHub error: {err}: {j}")


# ---------- API helpers ----------

def get_authenticated_user(token: str) -> dict:
    resp = httpx.get(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


def list_user_orgs(token: str) -> list[dict]:
    resp = httpx.get(
        "https://api.github.com/user/orgs",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


def list_org_repos(token: str, org: str, *, per_page: int = 50) -> list[dict]:
    resp = httpx.get(
        f"https://api.github.com/orgs/{org}/repos",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        params={"per_page": per_page, "sort": "updated"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


def list_org_members(token: str, org: str, *, per_page: int = 100) -> list[dict]:
    """Returns minimal member data for the report. Uses /orgs/{org}/members
    which requires read:org scope and only includes public members (or all
    members if the user is themselves a member of the org)."""
    out: list[dict] = []
    page = 1
    while True:
        resp = httpx.get(
            f"https://api.github.com/orgs/{org}/members",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            params={"per_page": per_page, "page": page},
            timeout=20.0,
        )
        resp.raise_for_status()
        page_data = resp.json()
        if not page_data:
            break
        out.extend(page_data)
        if len(page_data) < per_page:
            break
        page += 1
        if page > 10:  # safety cap (1000 members)
            break
    return out
