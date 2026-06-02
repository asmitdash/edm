"""Google OAuth 2.0 device-flow helpers for Gmail read access.

Why device flow:
  - The user runs EDM on a self-hosted box; we cannot rely on a redirect URL.
  - Device flow returns a short user_code the user pastes at
    https://www.google.com/device — same UX as the GitHub flow we already use.

Scope:
  https://www.googleapis.com/auth/gmail.readonly  (read-only)

We do NOT bundle a Google OAuth client_id/secret because each install must
register its own (Google requires that the consent screen identify the app).
The setup wizard collects client_id / client_secret on the /setup/gmail page;
we then drive the device flow against Google's endpoint.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


GMAIL_DEVICE_AUTH_URL = "https://oauth2.googleapis.com/device/code"
GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

DEFAULT_SCOPE = "https://www.googleapis.com/auth/gmail.readonly openid email profile"


@dataclass
class GmailDeviceCode:
    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


@dataclass
class GmailToken:
    access_token: str
    refresh_token: str | None
    scope: str
    expires_in: int
    id_token: str | None = None


def request_device_code(*, client_id: str, scope: str = DEFAULT_SCOPE) -> GmailDeviceCode:
    r = httpx.post(GMAIL_DEVICE_AUTH_URL, data={"client_id": client_id, "scope": scope}, timeout=10)
    r.raise_for_status()
    j = r.json()
    return GmailDeviceCode(
        device_code=j["device_code"],
        user_code=j["user_code"],
        verification_url=j.get("verification_url") or j.get("verification_uri") or "https://www.google.com/device",
        expires_in=int(j.get("expires_in", 1800)),
        interval=int(j.get("interval", 5)),
    )


def poll_for_token(
    device: GmailDeviceCode,
    *,
    client_id: str,
    client_secret: str,
    max_wait: int = 6,
) -> GmailToken | None:
    """Poll once, briefly. Returns None when still pending so the caller can
    re-render the wait page; raises on hard error.
    """
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        r = httpx.post(
            GMAIL_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "device_code": device.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=10,
        )
        if r.status_code == 200:
            j = r.json()
            return GmailToken(
                access_token=j["access_token"],
                refresh_token=j.get("refresh_token"),
                scope=j.get("scope", DEFAULT_SCOPE),
                expires_in=int(j.get("expires_in", 3600)),
                id_token=j.get("id_token"),
            )
        try:
            err = r.json().get("error")
        except Exception:
            err = r.text
        if err == "authorization_pending":
            time.sleep(device.interval)
            continue
        if err == "slow_down":
            time.sleep(device.interval + 2)
            continue
        if err in ("access_denied", "expired_token"):
            raise RuntimeError(f"Gmail OAuth: {err}")
        # any other error
        raise RuntimeError(f"Gmail OAuth error: {err} ({r.status_code})")
    return None


def refresh_access_token(*, client_id: str, client_secret: str, refresh_token: str) -> GmailToken:
    r = httpx.post(
        GMAIL_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=10,
    )
    r.raise_for_status()
    j = r.json()
    return GmailToken(
        access_token=j["access_token"],
        refresh_token=refresh_token,
        scope=j.get("scope", DEFAULT_SCOPE),
        expires_in=int(j.get("expires_in", 3600)),
    )


def get_email(access_token: str) -> str | None:
    r = httpx.get(GMAIL_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    if r.status_code != 200:
        return None
    return r.json().get("email")
