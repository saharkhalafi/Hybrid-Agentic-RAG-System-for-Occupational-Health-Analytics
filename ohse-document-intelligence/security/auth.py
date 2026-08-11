"""API key authentication for production endpoints."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass


@dataclass
class AuthContext:
    api_key_id: str
    role: str  # "query" | "admin" | "review"


class APIKeyAuth:
    """Simple HMAC-validated API key auth.

    Keys are stored as SHA-256 hashes in settings (never plaintext in config).
    In production, load from GCP Secret Manager or env.
    """

    def __init__(self, key_hashes: dict[str, str], *, require_auth: bool = True) -> None:
        # key_hashes: {key_id: sha256_hex_of_key}
        self._hashes = key_hashes
        self.require_auth = require_auth

    @staticmethod
    def hash_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()

    @staticmethod
    def generate_key() -> str:
        return f"ohse_{secrets.token_urlsafe(32)}"

    def authenticate(self, raw_key: str | None) -> AuthContext | None:
        if not raw_key:
            if not self.require_auth:
                return AuthContext(api_key_id="anonymous", role="query")
            return None
        key_hash = self.hash_key(raw_key)
        for key_id, stored_hash in self._hashes.items():
            if hmac.compare_digest(key_hash, stored_hash):
                role = "admin" if key_id.startswith("admin_") else "query"
                return AuthContext(api_key_id=key_id, role=role)
        return None

    def authenticate_review(self, raw_key: str | None, reviewer_id: str) -> AuthContext | None:
        ctx = self.authenticate(raw_key)
        if ctx is None:
            return None
        if reviewer_id.startswith("llm:"):
            return None
        return ctx
