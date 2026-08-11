from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

COOKIE_NAME = "driving_log_session"
SESSION_LIFETIME_SECONDS = 60 * 60 * 24 * 30
ACCOUNTS = {"sean": "Sean Ahern", "jen": "Jen Ahern"}


@dataclass(frozen=True)
class Authenticator:
    sean_password_hash: str
    jen_password_hash: str
    session_secret: str

    def authenticate(self, account: str, password: str) -> str | None:
        password_hash = {
            "sean": self.sean_password_hash,
            "jen": self.jen_password_hash,
        }.get(account)
        if not password_hash or not verify_password(password, password_hash):
            return None
        return ACCOUNTS[account]

    def make_session(self, user: str) -> str:
        payload = {
            "exp": int(time.time()) + SESSION_LIFETIME_SECONDS,
            "nonce": secrets.token_urlsafe(16),
            "user": user,
        }
        encoded = _encode(payload)
        signature = hmac.new(
            self.session_secret.encode(), encoded.encode(), hashlib.sha256
        ).hexdigest()
        return f"{encoded}.{signature}"

    def session_user(self, token: str | None) -> str | None:
        if not token or "." not in token:
            return None
        encoded, supplied_signature = token.rsplit(".", 1)
        expected_signature = hmac.new(
            self.session_secret.encode(), encoded.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        try:
            payload = json.loads(_decode(encoded))
            user = payload["user"]
            expires_at = payload["exp"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            not isinstance(user, str)
            or user not in ACCOUNTS.values()
            or not isinstance(expires_at, int)
            or expires_at < time.time()
        ):
            return None
        return user


def password_hash(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    salt_text = base64.urlsafe_b64encode(salt).decode()
    digest_text = base64.urlsafe_b64encode(digest).decode()
    return f"scrypt$16384$8$1${salt_text}${digest_text}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n_text, r_text, p_text, salt_text, digest_text = encoded.split("$")
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text)
        expected = base64.urlsafe_b64decode(digest_text)
        actual = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=int(n_text),
            r=int(r_text),
            p=int(p_text),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _encode(value: dict[str, object]) -> str:
    return (
        base64.urlsafe_b64encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def _decode(value: str) -> str:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
