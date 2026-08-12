from __future__ import annotations

import json, random, string, math
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Attachment, Ticket, TicketEvent, utcnow
from ..attachments import attachment_url, discard, finalize, model_from_stage, stage_data_url
from .constants import DONE, FIXED, NEW, PAUSE, REJECTED, REVISION, TERMINAL, WORK
from .roles import UserError, raw_role, require_admin
from .intents import notify, notify_work, rate_limit, sheet

HISTORY_PAGE_SIZE = 15


def aware(value: datetime | None) -> datetime | None:
    if value and value.tzinfo is None: return value.replace(tzinfo=timezone.utc)
    return value


def running_elapsed(ticket: Ticket, now: datetime | None = None) -> int:
    elapsed = ticket.elapsed_seconds
    if ticket.status == WORK and ticket.work_started_at:
        elapsed += max(1, int(((now or utcnow()) - aware(ticket.work_started_at)).total_seconds()))
    return elapsed


def serialize(ticket: Ticket) -> dict:
    return {"number": ticket.number, "createdAt": aware(ticket.created_at).isoformat(), "type": ticket.type, "city": ticket.city,
            "office": ticket.office, "senderName": ticket.sender_name, "description": ticket.description, "status": ticket.status,
            "creatorId": ticket.creator_id, "adminId": ticket.admin_id or "", "adminName": ticket.admin_name or "",
            "isRunning": bool(ticket.status == WORK and ticket.work_started_at), "elapsedSeconds": running_elapsed(ticket),
            "idleSeconds": ticket.idle_seconds or 0, "fileUrl": ticket.file_url or "", "reason": ticket.reason or "",
            "resolvedAt": aware(ticket.resolved_at).isoformat() if ticket.resolved_at else "", "updatedAt": aware(ticket.updated_at).isoformat()}


async def event(session, ticket, actor, name, old=""):
    session.add(TicketEvent(ticket_id=ticket.id, actor_tg_id=actor, event=name, from_status=old, to_status=ticket.status, payload_json="{}"))

def format_min_sec(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def intents(session, ticket, operation, *, actor_name=""):
    key=f"ticket:{ticket.id}:v{ticket.version}:{operation}"
    payload={
        "kind":"ticket", "operation":operation, "ticket_id":ticket.id,
        "number":ticket.number, "status":ticket.status, "creator_id":ticket.creator_id,
        "admin_id":ticket.admin_id or "", "admin_name":ticket.admin_name or "",
        "reason":ticket.reason or "", "elapsed_seconds":ticket.elapsed_seconds,
        "type":ticket.type, "city":ticket.city, "office":ticket.office,
        "sender_name":ticket.sender_name, "description":ticket.description,
        "version":ticket.version,
    }
    sheet(session,key,"ticket",ticket.id,operation,payload)
    if operation == "create":
        text=(f"🔴 Новая заявка {ticket.number}\nТип: {ticket.type}\n"
              f"Город/офис: {ticket.city} / {ticket.office}\nОт: {ticket.sender_name}\n\n{ticket.description}")
        notify_work(session,key+":work",text,payload=payload)
    elif operation == "take":
        notify_work(session,key+":work",f"🟡 Заявка {ticket.number} взята в работу ({ticket.admin_name})",payload=payload)
    elif operation == "resume":
        notify_work(session,key+":work",f"🟡 Заявка {ticket.number} снова в работе",payload=payload)
    elif operation == "resubmit":
        text=(f"🔧 Заявка {ticket.number} исправлена и возвращена в работу\nТип: {ticket.type}\n"
              f"Город/офис: {ticket.city} / {ticket.office}\nОт: {ticket.sender_name}")
        notify_work(session,key+":work",text,payload=payload)
    elif operation == DONE:
        comment = ticket.reason or ""
        notify(session,key+":author",ticket.creator_id,
               f"✅ Заявка {ticket.number} решена."+(f"\nКомментарий: {comment}" if comment else ""),payload=payload)
        text=f"🟢 Заявка {ticket.number} решена\nЗатрачено: {format_min_sec(ticket.elapsed_seconds)}"
        if ticket.admin_name: text+=f"\nИсполнитель: {ticket.admin_name}"
        if comment: text+=f"\nКомментарий: {comment}"
        notify_work(session,key+":work",text,payload=payload)
    elif operation == REVISION:
        notify(session,key+":author",ticket.creator_id,
               f"✏️ Заявка {ticket.number} возвращена на доработку.\nОснование: {ticket.reason}"
               "\nОткройте приложение, исправьте данные и отправьте заявку снова.",payload=payload)
        notify_work(session,key+":work",
                    f"✏️ Заявка {ticket.number} отправлена на доработку ({ticket.admin_name or '—'})\nОснование: {ticket.reason}",payload=payload)
    elif operation == REJECTED:
        notify(session,key+":author",ticket.creator_id,
               f"🚫 Заявка {ticket.number} отклонена.\nОснование: {ticket.reason}",payload=payload)
        notify_work(session,key+":work",
                    f"🚫 Заявка {ticket.number} отклонена ({ticket.admin_name or '—'})\nОснование: {ticket.reason}",payload=payload)
    elif operation == "transfer":
        payload["from_admin_name"] = actor_name
        notify(session,key+":target",ticket.admin_id,
               f"🔁 Вам передали заявку {ticket.number}"+(f" (от {actor_name})" if actor_name else ""),payload=payload)
        notify_work(session,key+":work",
                    f"🔁 Заявка {ticket.number} передана: {actor_name or '—'} → {ticket.admin_name or ticket.admin_id}",payload=payload)


async def fetch(session, number):
    ticket = (await session.execute(select(Ticket).where(Ticket.number == str(number)))).scalar_one_or_none()
    if not ticket: raise UserError(f"Заявка {number} не найдена.")
    return ticket


async def create_ticket(session: AsyncSession, actor: dict, body: dict) -> dict:
    role = await raw_role(session, actor["tg_id"])
    if role["role"] not in ("сотрудник", "админ"): raise UserError("Нет доступа. Запросите доступ у администратора.")
    await rate_limit(session,actor["tg_id"],"create",30)
    for field in ("type", "city", "office", "name", "description"):
        if not str(body.get(field) or "").strip(): raise UserError(f"Не заполнено поле: {field}")
    typ = str(body["type"]).strip()
    if typ not in role["allowedTypes"]: raise UserError(f"Тип заявки «{typ}» недоступен для вашего аккаунта. Обратитесь к администратору.")
    staged = stage_data_url(session.info["settings"], body["image"], body.get("filename")) if body.get("image") else None
    for _ in range(220):
        number = random.choice(string.ascii_uppercase) + f"{random.randrange(1000):03d}"
        now = utcnow(); ticket = Ticket(number=number, created_at=now, type=typ, city=str(body["city"]).strip(), office=str(body["office"]).strip(), sender_name=str(body["name"]).strip(), description=str(body["description"]).strip(), status=NEW, creator_id=actor["tg_id"], updated_at=now)
        session.add(ticket)
        try:
            await session.flush()
            if staged:
                finalize(staged, session.info["settings"])
                session.add(model_from_stage(staged, ticket.id))
                ticket.file_url = attachment_url(session.info["settings"], staged.token)
            await event(session, ticket, actor["tg_id"], "create"); intents(session,ticket,"create"); await session.commit(); await session.refresh(ticket)
            return {"ticket": serialize(ticket)}
        except IntegrityError:
            await session.rollback()
            # A number collision happens before finalization; retry safely.
        except Exception:
            await session.rollback(); discard(staged)
            if staged: (session.info["settings"].media_dir / staged.stored_name).unlink(missing_ok=True)
            raise
    discard(staged)
    raise UserError("Пространство номеров заявок исчерпано — обратитесь к администратору.")


async def get_my_tickets(session, actor, body):
    rows = (await session.execute(select(Ticket).where(Ticket.creator_id == actor["tg_id"]).order_by(Ticket.created_at.desc()))).scalars()
    return {"tickets": [serialize(x) for x in rows]}


async def get_tickets(session, actor, body):
    await require_admin(session, actor["tg_id"])
    rows = (await session.execute(select(Ticket).where(Ticket.status.not_in(TERMINAL)).order_by(Ticket.created_at.desc()))).scalars()
    return {"tickets": [serialize(x) for x in rows]}


async def get_history(session, actor, body):
    await require_admin(session, actor["tg_id"])
    rows = list((await session.execute(select(Ticket).where(Ticket.status.in_(TERMINAL)).order_by(Ticket.resolved_at.desc()))).scalars())
    q = str(body.get("q") or "").strip().lower()
    if q: rows = [t for t in rows if any(q in str(v or "").lower() for v in (t.number,t.description,t.sender_name,t.type,t.city,t.office,t.admin_name,t.reason))]
    pages=max(1,(len(rows)+14)//15)
    try: page=int(float(body.get("page") or 1))
    except (ValueError,TypeError): page=1
    page=min(max(page,1),pages)
    return {"tickets":[serialize(x) for x in rows[(page-1)*15:page*15]],"page":page,"totalPages":pages,"total":len(rows)}


async def transition(session, actor, body, allowed, target, error, *, require_reason=False, finish=False):
    await require_admin(session, actor["tg_id"])
    reason=str(body.get("reason") or body.get("comment") or "").strip()
    if require_reason and not reason: raise UserError("Укажите основание доработки." if target==REVISION else "Укажите основание отклонения.")
    number=str(body.get("number") or ""); ticket=await fetch(session,number); old=ticket.status; now=utcnow()
    if old not in allowed: raise UserError(error)
    values={"status":target,"updated_at":now,"version":ticket.version+1}
    if old==WORK and ticket.work_started_at: values["elapsed_seconds"]=running_elapsed(ticket,now)
    if target in (PAUSE,REVISION,DONE,REJECTED): values["work_started_at"]=None
    if target==WORK: values["work_started_at"]=now
    if target in TERMINAL: values["resolved_at"]=now
    if finish: values["reason"]=reason[:1000]
    elif reason: values["reason"]=reason
    if target==REJECTED: values.update(admin_id=actor["tg_id"],admin_name=str(body.get("name") or (await raw_role(session,actor["tg_id"]))["name"] or ticket.admin_name))
    result=await session.execute(update(Ticket).where(Ticket.id==ticket.id,Ticket.version==ticket.version,Ticket.status==old).values(**values))
    if result.rowcount != 1: await session.rollback(); raise UserError("Заявка была изменена другим пользователем. Обновите список.")
    await session.refresh(ticket); await event(session,ticket,actor["tg_id"],target,old); intents(session,ticket,"resume" if target==WORK else target); await session.commit()
    return {"ticket":serialize(ticket)}


async def take_ticket(session, actor, body):
    await require_admin(session,actor["tg_id"]); ticket=await fetch(session,body.get("number")); now=utcnow()
    if ticket.status not in (NEW,FIXED): raise UserError("Заявку нельзя взять в работу в текущем статусе.")
    old=ticket.status
    role=await raw_role(session,actor["tg_id"]); values={"status":WORK,"admin_id":actor["tg_id"],"admin_name":str(body.get("name") or role["name"]),"work_started_at":now,"updated_at":now,"version":ticket.version+1}
    if ticket.idle_seconds is None: values["idle_seconds"]=max(0,int((now-aware(ticket.created_at)).total_seconds()))
    result=await session.execute(update(Ticket).where(Ticket.id==ticket.id,Ticket.version==ticket.version,Ticket.status.in_((NEW,FIXED))).values(**values))
    if result.rowcount!=1: await session.rollback(); raise UserError("Заявку нельзя взять в работу в текущем статусе.")
    await session.refresh(ticket); await event(session,ticket,actor["tg_id"],"take",old); intents(session,ticket,"take"); await session.commit(); return {"ticket":serialize(ticket)}


async def pause_ticket(s,a,b): return await transition(s,a,b,{WORK},PAUSE,"Заявка не в работе.")
async def resume_ticket(s,a,b): return await transition(s,a,b,{PAUSE},WORK,"Заявку нельзя возобновить.")
async def finish_ticket(s,a,b): return await transition(s,a,b,{WORK,PAUSE},DONE,"Завершить можно только заявку в работе или на паузе.",finish=True)
async def return_ticket(s,a,b): return await transition(s,a,b,{WORK,PAUSE},REVISION,"На доработку можно отправить только заявку в работе или на паузе.",require_reason=True)
async def reject_ticket(s,a,b): return await transition(s,a,b,{NEW,FIXED,WORK,PAUSE,REVISION},REJECTED,"Заявка уже закрыта.",require_reason=True)


async def resubmit_ticket(session,actor,body):
    role=await raw_role(session,actor["tg_id"])
    if role["role"] not in ("сотрудник","админ"): raise UserError("Нет доступа. Запросите доступ у администратора.")
    await rate_limit(session,actor["tg_id"],"resubmit",30)
    for f in ("type","city","office","name","description"):
        if not str(body.get(f) or "").strip(): raise UserError(f"Не заполнено поле: {f}")
    if str(body["type"]).strip() not in role["allowedTypes"]: raise UserError(f"Тип заявки «{body['type']}» недоступен для вашего аккаунта. Обратитесь к администратору.")
    t=await fetch(session,body.get("number"))
    if t.creator_id!=actor["tg_id"]: raise UserError("Дорабатывать заявку может только её автор.")
    if t.status!=REVISION: raise UserError("Эту заявку нельзя доработать (она не на доработке).")
    old_version=t.version; old_url=t.file_url
    staged=stage_data_url(session.info["settings"],body["image"],body.get("filename")) if body.get("image") else None
    values={"type":str(body["type"]).strip(),"city":str(body["city"]).strip(),"office":str(body["office"]).strip(),"sender_name":str(body["name"]).strip(),"description":str(body["description"]).strip(),"status":FIXED,"updated_at":utcnow(),"version":old_version+1}
    if staged: values["file_url"]=attachment_url(session.info["settings"],staged.token)
    result=await session.execute(update(Ticket).where(Ticket.id==t.id,Ticket.version==old_version,Ticket.status==REVISION,Ticket.creator_id==actor["tg_id"]).values(**values))
    if result.rowcount!=1: await session.rollback(); discard(staged); raise UserError("Заявка была изменена другим пользователем. Обновите список.")
    try:
        old_stored = None
        if staged:
            finalize(staged,session.info["settings"]); session.add(model_from_stage(staged,t.id)); old_stored = await _mark_old_attachment_stale(session,old_url,staged.token)
        await session.refresh(t); await event(session,t,actor["tg_id"],"resubmit",REVISION); intents(session,t,"resubmit"); await session.commit()
        if old_stored:
            try: (session.info["settings"].media_dir/old_stored).unlink(missing_ok=True)
            except OSError: pass
    except Exception:
        await session.rollback(); discard(staged)
        if staged: (session.info["settings"].media_dir/staged.stored_name).unlink(missing_ok=True)
        raise
    return {"ticket":serialize(t)}


async def transfer_ticket(session,actor,body):
    await require_admin(session,actor["tg_id"]); target=str(body.get("to_tg_id") or "")
    if not target: raise UserError("Не выбран администратор.")
    role=await raw_role(session,target)
    if role["role"]!="админ": raise UserError("Получатель не является администратором.")
    t=await fetch(session,body.get("number"))
    if t.status not in (WORK,PAUSE): raise UserError("Передать можно только заявку в работе или на паузе.")
    old_version=t.version; old=t.status
    actor_role=await raw_role(session,actor["tg_id"])
    from_name=str(body.get("name") or actor_role["name"] or t.admin_name or "")
    result=await session.execute(update(Ticket).where(Ticket.id==t.id,Ticket.version==old_version,Ticket.status.in_((WORK,PAUSE))).values(admin_id=target,admin_name=role["name"],updated_at=utcnow(),version=old_version+1))
    if result.rowcount!=1: await session.rollback(); raise UserError("Заявка была изменена другим пользователем. Обновите список.")
    await session.refresh(t); await event(session,t,actor["tg_id"],"transfer",old); intents(session,t,"transfer",actor_name=from_name); await session.commit(); return {"ticket":serialize(t)}


async def add_screenshot(session,actor,body):
    await require_admin(session,actor["tg_id"])
    if not body.get("image"): raise UserError("Нет файла.")
    await rate_limit(session,actor["tg_id"],"file",60)
    t=await fetch(session,body.get("number")); old_url=t.file_url
    staged=stage_data_url(session.info["settings"],body["image"],body.get("filename"))
    try:
        finalize(staged,session.info["settings"]); session.add(model_from_stage(staged,t.id))
        t.file_url=attachment_url(session.info["settings"],staged.token); t.updated_at=utcnow(); t.version+=1
        old_stored = await _mark_old_attachment_stale(session,old_url,staged.token)
        await event(session,t,actor["tg_id"],"attachment"); intents(session,t,"attachment"); await session.commit()
        if old_stored:
            try: (session.info["settings"].media_dir/old_stored).unlink(missing_ok=True)
            except OSError: pass
        return {"ticket":serialize(t)}
    except Exception:
        await session.rollback(); discard(staged); (session.info["settings"].media_dir/staged.stored_name).unlink(missing_ok=True); raise


async def _mark_old_attachment_stale(session, old_url, new_token):
    if not old_url: return None
    token=str(old_url).rstrip('/').split('/')[-1]
    if token == new_token: return None
    old=(await session.execute(select(Attachment).where(Attachment.token==token))).scalar_one_or_none()
    if old:
        old.stale_at=utcnow()
        return old.stored_name
    return None


async def leaderboard(session,actor,body):
    await require_admin(session,actor["tg_id"]); period="month" if body.get("period")=="month" else "all"
    tz=timezone(timedelta(hours=5),"Asia/Yekaterinburg"); rows=list((await session.execute(select(Ticket).where(Ticket.status.in_(TERMINAL)))).scalars()); now=utcnow().astimezone(tz)
    if period=="month": rows=[t for t in rows if t.resolved_at and aware(t.resolved_at).astimezone(tz).year==now.year and aware(t.resolved_at).astimezone(tz).month==now.month]
    stats={}
    for t in rows:
        if not t.admin_id: continue
        s=stats.setdefault(t.admin_id,{"tg_id":t.admin_id,"name":t.admin_name or t.admin_id,"count":0,"total":0,"types":{},"cities":{}}); s["count"]+=1;s["total"]+=t.elapsed_seconds;s["types"][t.type]=s["types"].get(t.type,0)+1;s["cities"][t.city]=s["cities"].get(t.city,0)+1
    out=[]
    for s in stats.values(): out.append({"tg_id":s["tg_id"],"name":s["name"],"count":s["count"],"avgSeconds":math.floor(s["total"]/s["count"]+0.5),"favoriteType":max(s["types"],key=s["types"].get,default=""),"favoriteCity":max(s["cities"],key=s["cities"].get,default="")})
    for key,rev,field in (("count",True,"countPoints"),("avgSeconds",False,"speedPoints")):
        for i,x in enumerate(sorted(out,key=lambda z:z[key],reverse=rev)): x[field]=[3,2,1][i] if i<3 else 0
    for x in out:x["score"]=x["countPoints"]+x["speedPoints"]
    out.sort(key=lambda x:(-x["score"],-x["count"],x["avgSeconds"])); return {"period":period,"leaders":out}
