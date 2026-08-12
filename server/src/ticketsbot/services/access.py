from __future__ import annotations

from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AccessRequest, BridgeSequence, Role, utcnow
from .roles import UserError, raw_role, require_admin, upsert_role
from .intents import notify, notify_work, rate_limit, sheet

PAGE_SIZE = 10


def _event_key(entity: str, target: str, operation: str) -> str:
    """Return a durable key unique to one access/role mutation."""
    return f"{entity}:{target}:{operation}:{uuid4().hex}"


async def _sheet_access(session: AsyncSession, event_key: str, entity: str,
                        target: str, operation: str, payload: dict) -> None:
    """Persist monotonic entity ordering with the mutation and outbox event."""
    key = f"access:{target}"
    stmt = insert(BridgeSequence).values(entity_key=key, version=1).on_conflict_do_update(
        index_elements=[BridgeSequence.entity_key],
        set_={"version": BridgeSequence.version + 1},
    ).returning(BridgeSequence.version)
    sequence = int((await session.execute(stmt)).scalar_one())
    payload = dict(payload)
    payload["sequence"] = sequence
    sheet(session, event_key, entity, target, operation, payload)


async def request_access(session: AsyncSession, actor: dict, body: dict) -> dict:
    tg_id = actor["tg_id"]
    if (await raw_role(session, tg_id))["role"] != "гость":
        return {"ok": True, "already": True}
    await rate_limit(session,tg_id,"access",5)
    values={"name":str(body.get("name") or actor.get("tg_name") or ""),"username":actor.get("tg_username", "") or "","photo_url":actor.get("tg_photo", "") or "","requested_at":utcnow()}
    stmt=insert(AccessRequest).values(tg_id=tg_id,**values).on_conflict_do_update(index_elements=[AccessRequest.tg_id],set_=values)
    await session.execute(stmt)
    event_key=_event_key("access",tg_id,"request")
    await _sheet_access(session,event_key,"access",tg_id,"request",{"tg_id":tg_id,"name":values["name"],
          "username":values["username"],"photo_url":values["photo_url"]})
    payload={"kind":"access_request","operation":"requestAccess","tg_id":tg_id,
             "name":values["name"],"username":values["username"]}
    text="🔑 Запрос доступа к боту: "
    if values["name"]: text+=values["name"]+" "
    if values["username"]: text+="@"+values["username"]+" "
    text+=f"(id {tg_id})"
    notify_work(session,event_key,text,payload=payload)
    await session.commit()
    return {"ok": True}


async def get_access(session: AsyncSession, actor: dict, body: dict) -> dict:
    await require_admin(session, actor["tg_id"])
    requests = (await session.execute(select(AccessRequest).order_by(AccessRequest.requested_at))).scalars().all()
    roles_ids = set((await session.execute(select(Role.tg_id))).scalars())
    pending = [{"tg_id": r.tg_id, "name": r.name, "date": r.requested_at.isoformat(), "username": r.username, "photo_url": r.photo_url} for r in requests if r.tg_id not in roles_ids]
    stmt = select(Role).where(Role.role == "сотрудник")
    q = str(body.get("q") or "").strip().lower()
    employees = (await session.execute(stmt)).scalars().all()
    out = [{"tg_id": r.tg_id, "name": r.name or r.tg_id, "username": r.username, "photo_url": r.photo_url} for r in employees]
    if q:
        out = [e for e in out if q in e["name"].lower() or q in e["username"].lower()]
    out.sort(key=lambda e: e["name"].lower())
    total_pages = max(1, (len(out) + PAGE_SIZE - 1) // PAGE_SIZE)
    try: page = int(float(body.get("page") or 1))
    except (TypeError, ValueError): page = 1
    page = min(max(1, page), total_pages)
    return {"requests": pending, "employees": out[(page-1)*PAGE_SIZE:page*PAGE_SIZE], "page": page, "totalPages": total_pages, "total": len(out)}


async def approve_access(session: AsyncSession, actor: dict, body: dict) -> dict:
    await require_admin(session, actor["tg_id"])
    target = str(body.get("target_tg_id") or "")
    if not target: raise UserError("Не выбран сотрудник.")
    req = await session.get(AccessRequest, target)
    name = str(body.get("target_name") or (req.name if req else ""))
    await upsert_role(session, target, name, "сотрудник", req.username if req else "", req.photo_url if req else "")
    if req: await session.delete(req)
    event_key=_event_key("role",target,"approve")
    await _sheet_access(session,event_key,"role",target,"approve",{"tg_id":target,"name":name,
          "role":"сотрудник","username":req.username if req else "",
          "photo_url":req.photo_url if req else ""})
    notify(session,event_key,target,"Доступ одобрен")
    await session.commit()
    return {"ok": True}


async def reject_access(session: AsyncSession, actor: dict, body: dict) -> dict:
    await require_admin(session, actor["tg_id"])
    target = str(body.get("target_tg_id") or "")
    if not target: raise UserError("Не выбран запрос.")
    result=await session.execute(delete(AccessRequest).where(AccessRequest.tg_id == target))
    if result.rowcount!=1: await session.rollback(); raise UserError("Запрос уже обработан другим администратором.")
    event_key=_event_key("access",target,"reject")
    await _sheet_access(session,event_key,"access",target,"reject",{"tg_id":target}); notify(session,event_key,target,"Запрос доступа отклонён"); await session.commit()
    return {"ok": True}


async def rename_role(session: AsyncSession, actor: dict, body: dict) -> dict:
    await require_admin(session, actor["tg_id"])
    target, name = str(body.get("target_tg_id") or ""), str(body.get("target_name") or "").strip()
    if not target: raise UserError("Не выбран сотрудник.")
    if not name: raise UserError("Укажите новое имя.")
    role = await session.get(Role, target)
    if not role: raise UserError("Сотрудник не найден.")
    old=role.name
    result=await session.execute(update(Role).where(Role.tg_id==target,Role.name==old).values(name=name[:80]))
    if result.rowcount!=1: await session.rollback(); raise UserError("Профиль был изменён другим пользователем.")
    await _sheet_access(session,_event_key("role",target,"rename"),"role",target,"rename",{"tg_id":target,"name":name[:80]}); await session.commit()
    return {"ok": True, "name": name[:80]}


async def revoke_access(session: AsyncSession, actor: dict, body: dict) -> dict:
    await require_admin(session, actor["tg_id"])
    target = str(body.get("target_tg_id") or "")
    if not target: raise UserError("Не выбран сотрудник.")
    target_role = await session.get(Role, target)
    if target_role and target_role.role == "админ":
        admin_count = await session.scalar(select(func.count()).select_from(Role).where(Role.role == "админ"))
        if int(admin_count or 0) <= 1:
            raise UserError("Нельзя удалить последнего администратора.")
    result=await session.execute(delete(Role).where(Role.tg_id == target))
    if result.rowcount!=1: await session.rollback(); raise UserError("Сотрудник уже удалён.")
    await session.execute(delete(AccessRequest).where(AccessRequest.tg_id == target))
    event_key=_event_key("role",target,"revoke")
    await _sheet_access(session,event_key,"role",target,"revoke",{"tg_id":target}); notify(session,event_key,target,"Доступ отозван"); await session.commit()
    return {"ok": True}


async def refresh_contacts(session: AsyncSession, actor: dict, body: dict) -> dict:
    await require_admin(session, actor["tg_id"])
    client=body.get("_telegram_client")
    roles=(await session.execute(select(Role).where(Role.role=="сотрудник",Role.username==""))).scalars().all()
    updated=failed=0
    for role in roles:
        try:
            if client is None: raise RuntimeError("Telegram client unavailable")
            chat=await client.get_chat(role.tg_id)
            username=str(chat.get("username") if isinstance(chat,dict) else getattr(chat,"username","") or "")
            if not username: raise RuntimeError("username unavailable")
            role.username=username; updated+=1
            await _sheet_access(session,_event_key("role",role.tg_id,"refresh-contact"),"role",role.tg_id,"refresh_contact",{"tg_id":role.tg_id,"username":username})
        except Exception: failed+=1
    await session.commit()
    return {"updated":updated,"failed":failed}
