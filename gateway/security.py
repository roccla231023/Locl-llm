"""Password hashing and short-lived in-memory admin sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from http.cookies import SimpleCookie


PBKDF2_ITERATIONS = 310_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text)
        expected = base64.urlsafe_b64decode(digest_text)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


class SessionStore:
    def __init__(self, ttl_seconds: int = 12 * 60 * 60):
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge_locked()
            self._sessions[token] = time.time() + self.ttl_seconds
        return token

    def valid(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            self._purge_locked()
            expiry = self._sessions.get(token)
            return bool(expiry and expiry > time.time())

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def token_from_cookie(self, cookie_header: str | None) -> str:
        if not cookie_header:
            return ""
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
            morsel = cookie.get("llm_gateway_session")
            return morsel.value if morsel else ""
        except Exception:
            return ""

    def _purge_locked(self) -> None:
        now = time.time()
        expired = [token for token, expiry in self._sessions.items() if expiry <= now]
        for token in expired:
            self._sessions.pop(token, None)
