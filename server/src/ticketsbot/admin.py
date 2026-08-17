from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.sqlite import insert

from .admin_auth import (COOKIE_NAME, clear_login_failures, login_allowed, make_cookie,
                         new_session, record_login_failure, require_admin_origin,
                         require_admin_session, require_admin_state_change, verify_password)
from .models import (AccessRequest, AdminAudit, AdminSession, Attachment, Role,
                     SheetSyncOutbox, Ticket, TicketEvent, utcnow)
from .services.admin import archive_ticket, delete_ticket
from .services.access import _sheet_access

router = APIRouter(prefix="/admin/api", tags=["admin"])


class LoginBody(BaseModel):
    password: str = Field(max_length=1024)


class DeleteBody(BaseModel):
    confirm_number: str = Field(max_length=32)


class AddEmployeeBody(BaseModel):
    tg_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    name: str = Field(min_length=1, max_length=80)
    username: str = Field(default="", max_length=33)

    @field_validator("tg_id")
    @classmethod
    def valid_telegram_user_id(cls, value: str) -> str:
        if int(value) > (2 ** 52 - 1):
            raise ValueError("Telegram ID is outside the supported range")
        return value

    @field_validator("name")
    @classmethod
    def valid_employee_name(cls, value: str) -> str:
        value = unicodedata.normalize("NFKC", value).strip()
        if (not value or len(value) > 80
                or any(unicodedata.category(char).startswith("C") for char in value)):
            raise ValueError("Employee name contains unsupported characters")
        return value

    @field_validator("username")
    @classmethod
    def valid_username(cls, value: str) -> str:
        value = unicodedata.normalize("NFKC", value).strip()
        if value.startswith("@"):
            value = value[1:]
        if value and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", value):
            raise ValueError("Telegram username is invalid")
        return value


class EditEmployeeBody(AddEmployeeBody):
    role: Literal["сотрудник", "админ"]


def value(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def model_dict(row) -> dict:
    return {column.name: value(getattr(row, column.name)) for column in row.__table__.columns}


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    require_admin_origin(request)
    settings, db = request.app.state.settings, request.app.state.db
    if not settings.admin_password_hash or len(settings.admin_session_secret) < 32:
        raise HTTPException(503, "Admin authentication is not configured")
    remote = request.client.host if request.client else "unknown"
    key = f"{id(db)}:{remote}"
    if not login_allowed(key, settings.admin_login_max_attempts, settings.admin_login_window_seconds):
        raise HTTPException(429, "Too many login attempts")
    if not verify_password(body.password, settings.admin_password_hash):
        record_login_failure(key)
        raise HTTPException(401, "Invalid credentials")
    clear_login_failures(key)
    raw, row = new_session(settings.admin_session_ttl_seconds, remote)
    async with db.session() as session:
        session.add(row); await session.commit()
    response.set_cookie(COOKIE_NAME, make_cookie(raw, settings.admin_session_secret),
                        max_age=settings.admin_session_ttl_seconds, httponly=True, secure=True,
                        samesite="strict", path="/admin")
    return {"authenticated": True, "expires_at": value(row.expires_at),
            "csrf_token": row.csrf_secret}


@router.get("/session")
async def session_status(admin: AdminSession = Depends(require_admin_session)):
    return {"authenticated": True, "expires_at": value(admin.expires_at),
            "csrf_token": admin.csrf_secret}


@router.post("/logout")
async def logout(request: Request, response: Response,
                 admin: AdminSession = Depends(require_admin_state_change)):
    async with request.app.state.db.session() as session:
        row = await session.get(AdminSession, admin.id)
        if row: row.revoked_at = utcnow()
        await session.commit()
    response.delete_cookie(COOKIE_NAME, path="/admin", secure=True, httponly=True, samesite="strict")
    return {"authenticated": False}


@router.get("/tickets")
async def tickets(request: Request, q: str = "", status: str = "", type: str = "", city: str = "",
                  page: int = 1, page_size: int = 50, include_archived: bool = False,
                  _admin: AdminSession = Depends(require_admin_session)):
    page, page_size = max(1, page), min(100, max(1, page_size))
    filters = [] if include_archived else [Ticket.archived_at.is_(None)]
    if q:
        term = f"%{q[:200]}%"
        filters.append(or_(Ticket.number.ilike(term), Ticket.description.ilike(term),
                           Ticket.sender_name.ilike(term), Ticket.office.ilike(term)))
    if status: filters.append(Ticket.status == status)
    if type: filters.append(Ticket.type == type)
    if city: filters.append(Ticket.city == city)
    async with request.app.state.db.session() as session:
        total = int((await session.execute(select(func.count(Ticket.id)).where(*filters))).scalar_one())
        rows = list((await session.execute(select(Ticket).where(*filters).order_by(
            Ticket.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars())
        summary_rows = (await session.execute(select(Ticket.status, func.count(Ticket.id)).where(
            *filters).group_by(Ticket.status))).all()
    return {"items": [model_dict(row) for row in rows], "total": total, "page": page,
            "page_size": page_size, "summary": {key: count for key, count in summary_rows}}


@router.get("/access")
async def access_list(request: Request, q: str = "", page: int = 1, page_size: int = 50,
                      _admin: AdminSession = Depends(require_admin_session)):
    page, page_size = max(1, page), min(100, max(1, page_size))
    filters = []
    query = q.strip()[:200]
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        term = f"%{escaped}%"
        filters.append(or_(Role.name.ilike(term, escape="\\"), Role.username.ilike(term, escape="\\"),
                           Role.tg_id.ilike(term, escape="\\")))
    async with request.app.state.db.session() as session:
        total = int((await session.execute(select(func.count(Role.tg_id)).where(*filters))).scalar_one())
        rows = list((await session.execute(select(Role).where(*filters).order_by(
            Role.name.asc(), Role.tg_id.asc()).offset((page - 1) * page_size).limit(page_size))).scalars())
    return {"items": [{"tg_id": row.tg_id, "name": row.name, "role": row.role,
                       "username": row.username} for row in rows],
            "total": total, "page": page, "page_size": page_size}


@router.post("/access", status_code=201)
async def add_employee(body: AddEmployeeBody, request: Request,
                       admin: AdminSession = Depends(require_admin_state_change)):
    tg_id, name, username = body.tg_id, body.name, body.username
    async with request.app.state.db.session() as session:
        inserted = (await session.execute(insert(Role).values(
            tg_id=tg_id, name=name, role="сотрудник", username=username, photo_url="",
            allowed_types_json=None,
        ).on_conflict_do_nothing(index_elements=[Role.tg_id]).returning(Role.tg_id))).scalar_one_or_none()
        if inserted is None:
            await session.rollback()
            raise HTTPException(409, "Access already exists")
        await session.execute(delete(AccessRequest).where(AccessRequest.tg_id == tg_id))
        event_key = f"role:{tg_id}:admin-approve:{uuid4().hex}"
        await _sheet_access(session, event_key, "role", tg_id, "approve", {
            "tg_id": tg_id, "name": name, "role": "сотрудник", "username": username, "photo_url": "",
        })
        session.add(AdminAudit(session_id=admin.id, action="add_employee_access",
                               payload_json=json.dumps({"tg_id": tg_id, "name": name,
                                                        "role": "сотрудник", "username": username},
                                                       ensure_ascii=False, separators=(",", ":"))))
        await session.commit()
    return {"employee": {"tg_id": tg_id, "name": name, "role": "сотрудник", "username": username},
            "google_pending": True}


@router.patch("/access/{current_tg_id}")
async def edit_employee(current_tg_id: str, body: EditEmployeeBody, request: Request,
                        admin: AdminSession = Depends(require_admin_state_change)):
    new_tg_id, name, username, role_name = body.tg_id, body.name, body.username, body.role
    async with request.app.state.db.session() as session:
        role = await session.get(Role, current_tg_id)
        if role is None:
            raise HTTPException(404, "Запись доступа не найдена")
        if new_tg_id != current_tg_id and await session.get(Role, new_tg_id) is not None:
            raise HTTPException(409, "Для нового Telegram ID уже существует доступ")
        if role.role == "админ" and role_name != "админ":
            admin_count = int((await session.execute(select(func.count(Role.tg_id)).where(
                Role.role == "админ"))).scalar_one())
            if admin_count <= 1:
                raise HTTPException(409, "Нельзя понизить роль последнего администратора")

        old = {"tg_id": role.tg_id, "name": role.name, "role": role.role,
               "username": role.username}
        try:
            allowed_types = json.loads(role.allowed_types_json) if role.allowed_types_json else None
        except (TypeError, ValueError):
            allowed_types = None
        role.tg_id = new_tg_id
        role.name = name
        role.username = username
        role.role = role_name
        await session.execute(delete(AccessRequest).where(AccessRequest.tg_id == new_tg_id))

        payload = {"tg_id": new_tg_id, "name": name, "role": role_name,
                   "username": username, "photo_url": role.photo_url}
        if isinstance(allowed_types, list):
            payload["allowed_types"] = allowed_types
        await _sheet_access(session, f"role:{new_tg_id}:admin-update:{uuid4().hex}",
                            "role", new_tg_id, "update", payload)
        if new_tg_id != current_tg_id:
            await _sheet_access(session, f"role:{current_tg_id}:admin-revoke:{uuid4().hex}",
                                "role", current_tg_id, "revoke", {"tg_id": current_tg_id})
        employee = {"tg_id": new_tg_id, "name": name, "role": role_name,
                    "username": username}
        session.add(AdminAudit(session_id=admin.id, action="edit_employee_access",
                               payload_json=json.dumps({"before": old, "after": employee},
                                                       ensure_ascii=False, separators=(",", ":"))))
        await session.commit()
    return {"employee": employee, "google_pending": True}


@router.get("/tickets/{number}")
async def ticket_detail(number: str, request: Request,
                        _admin: AdminSession = Depends(require_admin_session)):
    async with request.app.state.db.session() as session:
        ticket = (await session.execute(select(Ticket).where(Ticket.number == number))).scalar_one_or_none()
        if not ticket: raise HTTPException(404, "Ticket not found")
        events = list((await session.execute(select(TicketEvent).where(
            TicketEvent.ticket_id == ticket.id).order_by(TicketEvent.id))).scalars())
        attachments = list((await session.execute(select(Attachment).where(
            Attachment.ticket_id == ticket.id).order_by(Attachment.id))).scalars())
        outbox = list((await session.execute(select(SheetSyncOutbox).where(
            SheetSyncOutbox.entity_type == "ticket", SheetSyncOutbox.entity_id == str(ticket.id)
        ).order_by(SheetSyncOutbox.id))).scalars())
    return {"ticket": model_dict(ticket), "events": [model_dict(x) for x in events],
            "attachments": [model_dict(x) for x in attachments],
            "outbox": [model_dict(x) for x in outbox]}


@router.post("/tickets/{number}/archive")
async def archive(number: str, request: Request,
                  admin: AdminSession = Depends(require_admin_state_change)):
    async with request.app.state.db.session() as session:
        ticket = await archive_ticket(session, number, admin.id)
    return {"number": ticket.number, "archived_at": value(ticket.archived_at)}


@router.delete("/tickets/{number}")
async def permanent_delete(number: str, body: DeleteBody, request: Request,
                           admin: AdminSession = Depends(require_admin_state_change)):
    async with request.app.state.db.session() as session:
        tombstone = await delete_ticket(session, number, body.confirm_number, admin.id,
                                        request.app.state.settings.admin_media_retention_days)
    return {"number": tombstone.number, "deleted": True, "google_pending": tombstone.google_deleted_at is None}
