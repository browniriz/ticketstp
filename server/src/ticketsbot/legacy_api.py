from __future__ import annotations

import json
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .auth import AuthError, verify_init_data
from .services.access import approve_access,get_access,refresh_contacts,reject_access,rename_role,request_access,revoke_access
from .services.roles import UserError,get_admins,get_role
from .services.tickets import add_screenshot,create_ticket,finish_ticket,get_history,get_my_tickets,get_tickets,leaderboard,pause_ticket,reject_ticket,resume_ticket,resubmit_ticket,return_ticket,take_ticket,transfer_ticket

router=APIRouter()
ACTIONS={"getRole":get_role,"createTicket":create_ticket,"getMyTickets":get_my_tickets,"getTickets":get_tickets,"getHistory":get_history,"takeTicket":take_ticket,"pauseTicket":pause_ticket,"resumeTicket":resume_ticket,"finishTicket":finish_ticket,"returnTicket":return_ticket,"rejectTicket":reject_ticket,"resubmitTicket":resubmit_ticket,"transferTicket":transfer_ticket,"addScreenshot":add_screenshot,"getAdmins":get_admins,"getLeaderboard":leaderboard,"requestAccess":request_access,"getAccess":get_access,"approveAccess":approve_access,"rejectAccess":reject_access,"renameRole":rename_role,"refreshContacts":refresh_contacts,"revokeAccess":revoke_access}

@router.get("/")
async def pong(): return {"success":True,"data":{"pong":True}}

@router.post("/")
async def legacy(request:Request):
    try:
        raw=await request.body(); body=json.loads(raw or b"{}")
        if not isinstance(body,dict): raise UserError("Некорректный запрос.")
        action=body.get("action")
        if action=="ping": return {"success":True,"data":{"pong":True}}
        if action not in ACTIONS: return {"success":False,"error":"Неизвестное действие: "+str(action)}
        settings=request.app.state.settings
        _validate_text_fields(body, settings)
        user=verify_init_data(body.get("init_data"),settings.bot_token,
                              settings.init_data_max_age_seconds,
                              settings.init_data_future_skew_seconds)
        actor={"tg_id":user.id,"tg_name":user.name,"tg_username":user.username,"tg_photo":user.photo_url}
        # Never trust identity supplied by the client.
        body.update(actor)
        body["_telegram_client"] = request.app.state.telegram_client
        async with request.app.state.db.session() as session:
            data=await ACTIONS[action](session,actor,body)
        return {"success":True,"data":data}
    except (UserError,AuthError,json.JSONDecodeError) as exc:
        return JSONResponse({"success":False,"error":str(exc)})
    except Exception:
        return JSONResponse({"success":False,"error":"Внутренняя ошибка сервера. Попробуйте позже."})


def _validate_text_fields(body: dict, settings) -> None:
    """Bound strings before service/database processing (uploads have separate limits)."""
    for key, value in body.items():
        if not isinstance(value, str):
            continue
        limit = settings.max_text_field_length
        if key == "init_data":
            limit = settings.max_init_data_length
        elif key == "image":
            limit = ((settings.max_attachment_bytes + 2) // 3) * 4 + 256
        if len(value) > limit:
            raise UserError("Поле запроса превышает допустимую длину.")
