from __future__ import annotations

import json
from datetime import datetime, timezone
from sqlalchemy import update

from ..models import NotificationOutbox, RateLimit, SheetSyncOutbox
from .roles import UserError


def sheet(session, key: str, entity_type: str, entity_id: str, operation: str, payload: dict | None = None):
    session.add(SheetSyncOutbox(dedupe_key=key, entity_type=entity_type,
        entity_id=str(entity_id), operation=operation,
        payload_json=json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))))


def notify(session, key: str, chat_id: str, text: str, *, thread_id=None, payload: dict | None = None):
    if not str(chat_id or "").strip():
        return
    data = dict(payload or {})
    data.update({"chat_id": str(chat_id), "thread_id": str(thread_id) if thread_id else None, "text": text})
    session.add(NotificationOutbox(
        dedupe_key=key, chat_id=str(chat_id),
        thread_id=str(thread_id) if thread_id else None, text=text,
        payload_json=json.dumps(data, ensure_ascii=False, separators=(",", ":")),
    ))


def notify_work(session, key: str, text: str, *, payload: dict | None = None):
    settings = session.info["settings"]
    notify(session, key, settings.notify_chat_id, text,
           thread_id=settings.notify_thread_id, payload=payload)


async def rate_limit(session, tg_id: str, bucket: str, maximum: int):
    window = int(datetime.now(timezone.utc).timestamp() // 3600)
    result = await session.execute(update(RateLimit).where(
        RateLimit.tg_id == tg_id, RateLimit.bucket == bucket,
        RateLimit.window == window, RateLimit.count < maximum
    ).values(count=RateLimit.count + 1))
    if result.rowcount == 0:
        row = await session.get(RateLimit, (tg_id, bucket, window))
        if row is not None:
            raise UserError("Слишком много запросов. Попробуйте позже.")
        session.add(RateLimit(tg_id=tg_id, bucket=bucket, window=window, count=1))
        await session.flush()
