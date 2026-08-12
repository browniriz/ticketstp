from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import timedelta, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from sqlalchemy import select

from .models import AdminSession, utcnow

COOKIE_NAME = "ticketsbot_admin"
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_password(password: str, encoded: str) -> bool:
    """Verify the documented scrypt$n$r$p$salt$digest format without dependencies."""
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        n_i, r_i, p_i = int(n), int(r), int(p)
        if n_i < 16384 or n_i > 1048576 or r_i < 8 or p_i < 1:
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=_b64decode(salt),
                                n=n_i, r=r_i, p=p_i, dklen=len(_b64decode(expected)))
        return hmac.compare_digest(actual, _b64decode(expected))
    except (ValueError, TypeError):
        return False


def login_allowed(key: str, maximum: int, window: int) -> bool:
    now = time.monotonic()
    attempts = [stamp for stamp in _LOGIN_ATTEMPTS.get(key, []) if now - stamp < window]
    _LOGIN_ATTEMPTS[key] = attempts
    return len(attempts) < maximum


def record_login_failure(key: str) -> None:
    _LOGIN_ATTEMPTS.setdefault(key, []).append(time.monotonic())


def clear_login_failures(key: str) -> None:
    _LOGIN_ATTEMPTS.pop(key, None)


def _origin(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
            return None
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    except ValueError:
        return None


def require_admin_origin(request: Request) -> None:
    configured = request.app.state.settings.admin_allowed_origin.strip()
    allowed = _origin(configured) if configured else _origin(str(request.base_url))
    supplied = request.headers.get("origin")
    actual = _origin(supplied) if supplied else _origin(request.headers.get("referer", ""))
    if not allowed or not actual or not hmac.compare_digest(actual, allowed):
        raise HTTPException(403, "Invalid request origin")


def make_cookie(raw: str, secret: str) -> str:
    signature = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return raw + "." + signature


def parse_cookie(cookie: str, secret: str) -> str | None:
    try:
        raw, signature = cookie.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return raw if hmac.compare_digest(signature, expected) else None


async def require_admin_session(request: Request) -> AdminSession:
    settings, db = request.app.state.settings, request.app.state.db
    if not settings.admin_session_secret:
        raise HTTPException(503, "Admin authentication is not configured")
    raw = parse_cookie(request.cookies.get(COOKIE_NAME, ""), settings.admin_session_secret)
    if not raw:
        raise HTTPException(401, "Authentication required")
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    async with db.session() as session:
        row = (await session.execute(select(AdminSession).where(
            AdminSession.token_hash == token_hash))).scalar_one_or_none()
        if row is None or row.revoked_at is not None:
            raise HTTPException(401, "Authentication required")
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= utcnow():
            raise HTTPException(401, "Session expired")
        return row


async def require_admin_state_change(request: Request) -> AdminSession:
    require_admin_origin(request)
    row = await require_admin_session(request)
    supplied = request.headers.get("x-csrf-token", "")
    if not row.csrf_secret or not supplied or not hmac.compare_digest(supplied, row.csrf_secret):
        raise HTTPException(403, "Invalid CSRF token")
    return row


def new_session(ttl_seconds: int, remote_addr: str) -> tuple[str, AdminSession]:
    raw = secrets.token_urlsafe(32)
    row = AdminSession(id=secrets.token_hex(16), token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                       expires_at=utcnow() + timedelta(seconds=ttl_seconds), remote_addr=remote_addr[:64],
                       csrf_secret=secrets.token_urlsafe(32))
    return raw, row
