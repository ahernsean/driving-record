from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid


class InvalidFormToken(ValueError):
    pass


def create_form_token(secret: str, action: str, *, now: int | None = None, ttl: int = 900) -> str:
    issued = int(time.time() if now is None else now)
    payload = f"{action}|{issued + ttl}|{uuid.uuid4()}".encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + b"|" + signature).decode().rstrip("=")


def verify_form_token(secret: str, token: str, action: str, *, now: int | None = None) -> None:
    try:
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded)
        if len(decoded) < 34 or decoded[-33:-32] != b"|":
            raise ValueError("token has no signature")
        payload, signature = decoded[:-33], decoded[-32:]
        token_action, expiration, _nonce = payload.decode().split("|", 2)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidFormToken("malformed form token") from exc
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise InvalidFormToken("invalid form token signature")
    if token_action != action:
        raise InvalidFormToken("form token action mismatch")
    current = int(time.time() if now is None else now)
    if int(expiration) < current:
        raise InvalidFormToken("form token expired")
