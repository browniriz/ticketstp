from __future__ import annotations

import json

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AccessRequest, Role, utcnow
from .constants import DEFAULT_EMPLOYEE_TICKET_TYPES, TICKET_TYPES


class UserError(ValueError):
    pass


async def raw_role(session: AsyncSession, tg_id: str) -> dict:
    role = await session.get(Role, str(tg_id))
    if not role:
        return {"role": "гость", "name": "", "allowedTypes": []}
    if role.role == "админ":
        allowed = TICKET_TYPES.copy()
    elif role.allowed_types_json is None:
        allowed = DEFAULT_EMPLOYEE_TICKET_TYPES.copy()
    else:
        configured = json.loads(role.allowed_types_json)
        allowed = [x for x in TICKET_TYPES if x in configured and x != "Списание"]
    return {"role": role.role or "сотрудник", "name": role.name or "", "allowedTypes": allowed}


async def get_role(session: AsyncSession, actor: dict, body: dict | None = None) -> dict:
    role = await session.get(Role, actor["tg_id"])
    if not role:
        pending = await session.get(AccessRequest, actor["tg_id"])
        return {"role": "гость", "name": "", "allowedTypes": [], "pending": bool(pending)}
    changed = False
    if actor.get("tg_username") and actor["tg_username"] != role.username:
        role.username = actor["tg_username"]
        changed = True
    if actor.get("tg_photo") and actor["tg_photo"] != role.photo_url:
        role.photo_url = actor["tg_photo"]
        changed = True
    if changed:
        role.updated_at = utcnow()
        await session.commit()
    return await raw_role(session, actor["tg_id"])


async def require_admin(session: AsyncSession, tg_id: str) -> dict:
    role = await raw_role(session, tg_id)
    if role["role"] != "админ":
        raise UserError("Доступ только для администраторов.")
    return role


async def upsert_role(session: AsyncSession, tg_id: str, name: str, role_name: str, username: str = "", photo: str = "") -> Role:
    role = await session.get(Role, tg_id)
    if role is None:
        role = Role(tg_id=tg_id, allowed_types_json=None)
        session.add(role)
    role.name, role.role = name, role_name
    if username:
        role.username = username
    if photo:
        role.photo_url = photo
    return role


async def get_admins(session: AsyncSession, actor: dict, body: dict | None = None) -> dict:
    await require_admin(session, actor["tg_id"])
    rows = (await session.execute(select(Role).where(Role.role == "админ"))).scalars()
    return {"admins": [{"tg_id": r.tg_id, "name": r.name or r.tg_id} for r in rows]}
