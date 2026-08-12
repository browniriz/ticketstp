from __future__ import annotations

from contextlib import asynccontextmanager
import json
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select

from .auth import AuthError, verify_init_data
from .config import Settings, get_settings
from .db import Database
from .legacy_api import router
from .media_tokens import media_signature, verify_media_signature
from .models import Attachment, Role, Ticket
from .workers import WorkerManager


class RequestBodyLimitMiddleware:
    """Reject oversized bodies before route-level JSON/base64 decoding."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            if int(headers.get(b"content-length", b"0")) > self.max_bytes:
                return await JSONResponse({"detail": "Request body too large"}, status_code=413)(scope, receive, send)
        except ValueError:
            return await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(scope, receive, send)
        chunks, total, more = [], 0, True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.max_bytes:
                return await JSONResponse({"detail": "Request body too large"}, status_code=413)(scope, receive, send)
            chunks.append(chunk)
            more = message.get("more_body", False)
        body = b"".join(chunks)
        delivered = False

        async def replay_receive():
            nonlocal delivered
            if delivered:
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay_receive, send)


def create_app(settings: Settings | None = None, telegram_client=None, sheet_client=None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.validate_public_base_url()
        await db.initialize()
        settings.media_dir.mkdir(parents=True, exist_ok=True)
        workers = WorkerManager(db, settings, telegram_client, sheet_client)
        app.state.workers = workers
        if settings.workers_enabled:
            workers.start()
        yield
        await workers.close()
        await db.close()

    app = FastAPI(title="Ticketsbot", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.telegram_client = telegram_client
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins,
                       allow_credentials=False, allow_methods=["GET", "POST", "OPTIONS"],
                       allow_headers=["Content-Type", "X-Telegram-Init-Data"])

    @app.get("/health")
    async def health():
        return {"status": "ok", "database": await db.integrity_check()}

    @app.post("/media/session")
    async def media_session(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
            token = str(body.get("token") or "")
            init_data = request.headers.get("X-Telegram-Init-Data") or body.get("init_data")
            if len(token) > 128 or len(str(init_data or "")) > settings.max_init_data_length:
                raise ValueError
            actor = verify_init_data(init_data, settings.bot_token,
                                     settings.init_data_max_age_seconds,
                                     settings.init_data_future_skew_seconds)
        except (AuthError, ValueError, json.JSONDecodeError):
            raise HTTPException(404)
        async with db.session() as session:
            attachment = (await session.execute(select(Attachment).where(Attachment.token == token))).scalar_one_or_none()
            if not attachment or attachment.stale_at:
                raise HTTPException(404)
            ticket = await session.get(Ticket, attachment.ticket_id)
            role = await session.get(Role, str(actor.id))
            if not ticket or (ticket.creator_id != str(actor.id) and (not role or role.role != "админ")):
                raise HTTPException(404)
        expires = int(time.time()) + settings.media_url_ttl_seconds
        signature = media_signature(settings.bot_token, token, str(actor.id), expires)
        url = settings.public_base_url.rstrip("/") + f"/media/{token}?user={actor.id}&expires={expires}&sig={signature}"
        return {"url": url, "expires": expires}

    @app.get("/media/{token}")
    async def media(token: str, user: str = "", expires: int = 0, sig: str = ""):
        now = int(time.time())
        if expires < now or expires > now + settings.media_url_ttl_seconds + 5:
            raise HTTPException(404)
        if not verify_media_signature(settings.bot_token, token, user, expires, sig):
            raise HTTPException(404)
        async with db.session() as session:
            attachment = (await session.execute(select(Attachment).where(Attachment.token == token))).scalar_one_or_none()
            if not attachment or attachment.stale_at:
                raise HTTPException(404)
            ticket = await session.get(Ticket, attachment.ticket_id)
            role = await session.get(Role, user)
            if not ticket or (ticket.creator_id != user and (not role or role.role != "админ")):
                raise HTTPException(404)
            media_root = settings.media_dir.resolve()
            path = (media_root / attachment.stored_name).resolve()
            try:
                path.relative_to(media_root)
            except ValueError:
                raise HTTPException(404)
            if not path.is_file():
                raise HTTPException(404)
            return FileResponse(path, media_type=attachment.mime_type, filename=attachment.original_name,
                                headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    app.include_router(router)
    return app
