from __future__ import annotations

import asyncio
import json
import logging
import secrets
import hashlib
from datetime import timedelta, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy import or_, select, update

from .models import (NotificationOutbox, Role, SheetSyncOutbox, SyncState, Ticket,
                     TicketTombstone, utcnow)
from .services.constants import TICKET_TYPES
from .services.tickets import aware, format_min_sec

log = logging.getLogger(__name__)

# Asia/Yekaterinburg has been fixed UTC+05:00 since 2014. A fixed offset keeps
# this portable on minimal Windows/Python installs that do not ship tzdata.
SHEET_TIMEZONE = timezone(timedelta(hours=5), "Asia/Yekaterinburg")
SHEET_DATETIME_FORMAT = "%d.%m.%Y %H:%M:%S"
ROLE_HEADERS = ["tg_id", "имя", "роль", "username", "photo_url", *TICKET_TYPES]


def sheet_datetime(value) -> str:
    if not value:
        return ""
    return aware(value).astimezone(SHEET_TIMEZONE).strftime(SHEET_DATETIME_FORMAT)


def ticket_row(t: Ticket) -> list:
    """Canonical legacy 18-column mirror row."""
    return [
        t.number, sheet_datetime(t.created_at), t.type, t.city,
        t.office, t.sender_name, t.description, t.status, t.creator_id, t.admin_id or "",
        t.admin_name or "", sheet_datetime(t.work_started_at),
        format_min_sec(t.elapsed_seconds), sheet_datetime(t.resolved_at),
        sheet_datetime(t.updated_at), format_min_sec(t.idle_seconds) if t.idle_seconds is not None else "",
        t.file_url or "", t.reason or "",
    ]


class TelegramClient:
    def __init__(self, token: str): self.token = token
    async def send_message(self, chat_id, text, thread_id=None):
        data = {"chat_id": chat_id, "text": text}
        if thread_id: data["message_thread_id"] = thread_id
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json=data)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or result.get("ok") is not True:
                description = result.get("description", "Telegram error") if isinstance(result, dict) else "invalid Telegram response"
                raise RuntimeError(str(description))
            if not isinstance(result.get("result"), dict) or "message_id" not in result["result"]:
                raise RuntimeError("invalid Telegram success response")
            return result["result"]


class BridgeClient:
    ALLOWED_HOSTS = {"script.google.com", "script.googleusercontent.com"}
    REQUEST_TIMEOUT_SECONDS = 120

    def __init__(self, url: str, secret: str):
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname not in self.ALLOWED_HOSTS
                or parsed.username or parsed.password):
            raise ValueError("sheet bridge URL must be HTTPS on an allowed Google host")
        self.url, self.secret = url, secret

    async def call(self, action: str, **payload):
        body = {"action": action, "bridge_secret": self.secret, **payload}
        async with httpx.AsyncClient(timeout=self.REQUEST_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.post(self.url, json=body)
            if response.is_redirect:
                location = response.headers.get("location", "")
                target = urlparse(location)
                if (target.scheme != "https" or target.hostname != "script.googleusercontent.com"
                        or target.username or target.password):
                    raise RuntimeError("unsafe bridge redirect")
                # Never replay the POST body or bridge secret across hosts.
                response = await client.get(location, headers={"Accept": "application/json"})
            response.raise_for_status(); result = response.json()
        if not result.get("success"): raise RuntimeError(str(result.get("error", "bridge error")))
        return result.get("data", {})


class WorkerManager:
    def __init__(self, db, settings, telegram=None, bridge=None):
        self.db, self.settings = db, settings
        self.telegram = telegram or (TelegramClient(settings.bot_token) if settings.bot_token else None)
        self.bridge = bridge or (BridgeClient(settings.sheet_bridge_url, settings.sheet_bridge_secret)
                                 if settings.sheet_bridge_url and settings.sheet_bridge_secret else None)
        self.tasks: list[asyncio.Task] = []
        self.stop = asyncio.Event()

    def start(self):
        if any(not task.done() for task in self.tasks): return
        self.stop.clear(); self.tasks = []
        if self.telegram: self.tasks.append(asyncio.create_task(self._loop(self.notifications_once), name="notification-outbox"))
        if self.bridge:
            self.tasks.append(asyncio.create_task(self._loop(self.sheets_once), name="sheet-outbox"))
            self.tasks.append(asyncio.create_task(self._roles_loop(), name="roles-pull"))

    async def close(self):
        self.stop.set()
        for task in self.tasks: task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def _loop(self, function):
        while not self.stop.is_set():
            try: await function()
            except asyncio.CancelledError: raise
            except Exception: log.exception("background worker iteration failed")
            await asyncio.sleep(self.settings.worker_poll_seconds)

    async def _roles_loop(self):
        while not self.stop.is_set():
            try: await self.pull_roles_once()
            except asyncio.CancelledError: raise
            except Exception: log.exception("roles pull failed")
            await asyncio.sleep(self.settings.roles_pull_seconds)

    async def notifications_once(self):
        for row_id, token, chat_id, text, thread_id in await self._claim(NotificationOutbox):
            try: await self.telegram.send_message(chat_id, text, thread_id)
            except Exception as exc: await self._finish(NotificationOutbox, row_id, token, exc)
            else: await self._finish(NotificationOutbox, row_id, token)

    async def sheets_once(self):
        for item in await self._claim(SheetSyncOutbox):
            row_id, token, entity_type, entity_id, operation, payload_json, dedupe_key = item
            try:
                if entity_type == "ticket":
                    payload = json.loads(payload_json)
                    sequence = payload.get("sequence")
                    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
                        raise RuntimeError("ticket upsert has no valid sequence")
                    async with self.db.session() as session:
                        ticket = await session.get(Ticket, int(entity_id))
                        snapshot = ticket_row(ticket) if ticket else None
                    if snapshot is None: raise RuntimeError(f"ticket {entity_id} no longer exists")
                    ack = await self.bridge.call("bridgeUpsertTicket", row=snapshot, sequence=sequence,
                                                 dedupe_key=dedupe_key)
                    if (not isinstance(ack, dict) or str(ack.get("number", "")) != str(snapshot[0])
                            or ack.get("sequence") != sequence
                            or str(ack.get("dedupe_key", "")) != dedupe_key):
                        raise RuntimeError("mismatched ticket bridge acknowledgment")
                elif entity_type == "ticket_tombstone" and operation == "delete_ticket":
                    payload = json.loads(payload_json)
                    number, sequence = str(payload.get("number") or ""), payload.get("sequence")
                    ack = await self.bridge.call("bridgeDeleteTicket", number=number,
                                                 sequence=sequence, dedupe_key=dedupe_key)
                    if (not isinstance(ack, dict) or str(ack.get("number", "")) != number
                            or ack.get("sequence") != sequence
                            or ack.get("current_sequence") != sequence
                            or ack.get("absent") is not True
                            or str(ack.get("dedupe_key", "")) != dedupe_key):
                        raise RuntimeError("mismatched ticket delete acknowledgment")
                    async with self.db.session() as session:
                        tombstone = (await session.execute(select(TicketTombstone).where(
                            TicketTombstone.number == number))).scalar_one_or_none()
                        current = (await session.execute(select(Ticket).where(
                            Ticket.number == number))).scalar_one_or_none()
                        if tombstone is None or tombstone.sequence != sequence or current is not None:
                            raise RuntimeError("ticket delete postcondition failed")
                        tombstone.google_deleted_at = utcnow()
                        await session.commit()
                else:
                    payload = json.loads(payload_json)
                    tg_id = str(payload.get("tg_id") or payload.get("creator_id") or "")
                    sequence = payload.get("sequence")
                    ack = await self.bridge.call("bridgeMirrorAccess", operation=operation,
                                                 payload=payload, dedupe_key=dedupe_key)
                    if (not isinstance(ack, dict) or str(ack.get("tg_id", "")) != tg_id
                            or str(ack.get("operation", "")) != operation
                            or ack.get("sequence") != sequence
                            or str(ack.get("dedupe_key", "")) != dedupe_key):
                        raise RuntimeError("mismatched access bridge acknowledgment")
            except Exception as exc: await self._finish(SheetSyncOutbox, row_id, token, exc)
            else: await self._finish(SheetSyncOutbox, row_id, token)

    async def _claim(self, model):
        """Atomically lease due rows, committing before any network operation."""
        now, token = utcnow(), secrets.token_urlsafe(24)
        expired = now - timedelta(seconds=self.settings.worker_claim_timeout_seconds)
        due_ids = select(model.id).where(
            model.delivered.is_(False), model.next_attempt_at <= now,
            model.attempts < self.settings.worker_max_attempts,
            or_(model.claimed_at.is_(None), model.claimed_at < expired),
        ).order_by(model.id).limit(self.settings.worker_batch_size).scalar_subquery()
        async with self.db.session() as session:
            ids = list((await session.execute(update(model).where(model.id.in_(due_ids)).values(
                claimed_at=now, claim_token=token, attempts=model.attempts + 1
            ).returning(model.id))).scalars())
            await session.commit()
        if not ids: return []
        async with self.db.session() as session:
            rows = list((await session.execute(select(model).where(
                model.id.in_(ids), model.claim_token == token).order_by(model.id))).scalars())
            if model is NotificationOutbox:
                return [(r.id, token, r.chat_id, r.text, r.thread_id) for r in rows]
            return [(r.id, token, r.entity_type, r.entity_id, r.operation,
                     r.payload_json, r.dedupe_key) for r in rows]

    async def _finish(self, model, row_id, token, error=None):
        async with self.db.session() as session:
            row = (await session.execute(select(model).where(
                model.id == row_id, model.claim_token == token))).scalar_one_or_none()
            if not row: return
            row.claimed_at = None; row.claim_token = ""
            if error is None:
                row.delivered=True; row.delivered_at=utcnow(); row.last_error=""
            else: self._failed(row, error)
            await session.commit()

    def _failed(self, row, exc):
        message = str(exc)
        if row.attempts >= self.settings.worker_max_attempts:
            message = f"permanently failed after {row.attempts} attempts: {message}"
        row.last_error = message[:2000]
        row.next_attempt_at = utcnow() + timedelta(seconds=min(3600, max(30, 2 ** min(row.attempts, 11))))

    async def pull_roles_once(self):
        data = await self.bridge.call("bridgePullRoles")
        if data.get("headers") != ROLE_HEADERS:
            raise ValueError("invalid roles headers")
        rows = data.get("rows", [])
        revision, count = data.get("revision"), data.get("count")
        supplied_hash = str(data.get("hash") or "")
        canonical = json.dumps({"headers": ROLE_HEADERS, "rows": rows}, ensure_ascii=False,
                               separators=(",", ":"))
        computed_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if (not isinstance(revision, int) or revision < 1 or count != len(rows)
                or not secrets.compare_digest(supplied_hash, computed_hash)):
            raise ValueError("invalid roles snapshot integrity")
        if len(rows) > 5000 or any(not isinstance(r, list) or len(r) != 14 for r in rows):
            raise ValueError("invalid roles snapshot")
        ids = [str(r[0]).strip() for r in rows]
        if any(not value or len(value) > 32 for value in ids) or len(ids) != len(set(ids)):
            raise ValueError("invalid or duplicate role id")
        if any(str(r[2] or "") not in ("сотрудник", "админ") for r in rows):
            raise ValueError("invalid role value")
        id_set = set(ids)
        async with self.db.session() as session:
            existing = {r.tg_id: r for r in (await session.execute(select(Role))).scalars()}
            revision_state = await session.get(SyncState, "roles_snapshot_revision")
            hash_state = await session.get(SyncState, "roles_snapshot_hash")
            count_state = await session.get(SyncState, "roles_snapshot_count")
            previous_revision = int(revision_state.value) if revision_state else 0
            previous_hash = hash_state.value if hash_state else ""
            previous_count = int(count_state.value) if count_state else len(existing)
            if revision < previous_revision or (revision == previous_revision and supplied_hash != previous_hash):
                raise ValueError("stale roles snapshot revision")
            if revision == previous_revision and supplied_hash == previous_hash:
                return
            if not rows and existing:
                raise ValueError("refusing suspicious empty roles snapshot")
            if previous_count >= 4 and len(rows) < max(1, (previous_count * 3) // 4):
                raise ValueError("refusing suspicious partial roles snapshot")
            if (any(r.role == "админ" for r in existing.values())
                    and not any(str(r[2]) == "админ" for r in rows)):
                raise ValueError("refusing snapshot that deletes last admin")
            for values in rows:
                tg_id=str(values[0]).strip()
                role=existing.get(tg_id) or Role(tg_id=tg_id)
                if tg_id not in existing: session.add(role)
                role.name=str(values[1] or ""); role.role=str(values[2] or "сотрудник")
                role.username=str(values[3] or ""); role.photo_url=str(values[4] or "")
                flags=values[5:14]
                role.allowed_types_json=json.dumps([t for t, flag in zip(TICKET_TYPES, flags) if flag is True or str(flag).lower() in ("true","1","да","yes","x","✓")],ensure_ascii=False)
            for tg_id, role in existing.items():
                if tg_id not in id_set: await session.delete(role)
            for key, value in (("roles_snapshot_count", str(len(rows))),
                               ("roles_snapshot_revision", str(revision)),
                               ("roles_snapshot_hash", supplied_hash)):
                state = await session.get(SyncState, key)
                if state is None: session.add(SyncState(key=key, value=value))
                else: state.value = value
            await session.commit()
