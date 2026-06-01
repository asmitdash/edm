"""At-rest secret encryption for `access_token_enc`, `api_key_enc`.

Key derivation: HKDF-SHA256 over EDM_SESSION_SECRET with a fixed app salt,
yielding a 32-byte key for Fernet. This means rotating EDM_SESSION_SECRET
invalidates all stored encrypted secrets — that's the right tradeoff for
this scale; we'd build a real KMS for v2.
"""

from __future__ import annotations

import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from edm.config import get_settings

_SALT = b"edm.v1.secrets"


def _key() -> bytes:
    secret = get_settings().session_secret.encode("utf-8")
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=b"fernet").derive(secret)
    return base64.urlsafe_b64encode(raw)


def encrypt(plaintext: str) -> str:
    return Fernet(_key()).encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    return Fernet(_key()).decrypt(ciphertext.encode("utf-8")).decode("utf-8")
