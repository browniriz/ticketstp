from __future__ import annotations

import hashlib
import hmac


def media_signature(secret: str, token: str, user_id: str, expires: int) -> str:
    message = f"{token}\n{user_id}\n{expires}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_media_signature(secret: str, token: str, user_id: str, expires: int, signature: str) -> bool:
    expected = media_signature(secret, token, user_id, expires)
    return hmac.compare_digest(expected, signature.lower())
