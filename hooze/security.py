"""Security primitives. Everything here FAILS CLOSED.

Closes audit finding C1 (webhook auth was commented out in pd; never
implemented in savoury) *by construction*: a channel row with no webhook
secret cannot be activated, and verification of a channel with no secret
returns False rather than True.
"""
from __future__ import annotations
import hashlib, hmac, secrets
from dataclasses import dataclass
from typing import Optional


def new_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def secrets_match(provided: Optional[str], expected_hash: Optional[str]) -> bool:
    """Constant-time compare of a provided secret against a stored sha256 hash. Missing anything => False."""
    if not provided or not expected_hash:
        return False
    return hmac.compare_digest(sha256_hex(provided), expected_hash)


def hmac_sha512_hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()


@dataclass
class Principal:
    kind: str                 # "platform" | "tenant"
    tenant_id: Optional[str] = None
    key_id: Optional[str] = None   # which key acted -> per-person attribution (audit enhancement)


class Vault:
    """Encrypts per-tenant credentials (payment keys, BYO API keys) at rest with Fernet."""
    def __init__(self, master_key: str):
        from cryptography.fernet import Fernet
        self._f = Fernet(master_key.encode())

    @staticmethod
    def generate_key() -> str:
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode()

    def encrypt(self, plaintext: str) -> str:
        return self._f.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._f.decrypt(token.encode()).decode()


def decrypts(master_key: str, token: str) -> bool:
    """True if `token` (a Vault ciphertext) can be decrypted with `master_key`. Never raises; never returns plaintext."""
    try:
        Vault(master_key).decrypt(token)
        return True
    except Exception:
        return False
