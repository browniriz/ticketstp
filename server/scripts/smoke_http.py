from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlencode

import httpx
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime-http"
DB_PATH = RUNTIME / "ticketsbot.db"
MEDIA = RUNTIME / "media"
TOKEN = "local-smoke-bot-token"
BASE_URL = "http://127.0.0.1:18010"


def signed(user_id: int, first_name: str, username: str) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": f"smoke-{user_id}",
        "user": json.dumps({"id": user_id, "first_name": first_name, "username": username}, separators=(",", ":"), ensure_ascii=False),
    }
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def env() -> dict[str, str]:
    result = os.environ.copy()
    result.update({
        "TICKETSBOT_BOT_TOKEN": TOKEN,
        "TICKETSBOT_DATABASE_URL": f"sqlite+aiosqlite:///{DB_PATH.as_posix()}",
        "TICKETSBOT_MEDIA_DIR": str(MEDIA),
        "TICKETSBOT_PUBLIC_BASE_URL": BASE_URL,
        "TICKETSBOT_HOST": "127.0.0.1",
        "TICKETSBOT_PORT": "18010",
        "TICKETSBOT_WORKERS_ENABLED": "false",
        "TICKETSBOT_CORS_ORIGINS": "https://browniriz.github.io",
    })
    return result


async def seed() -> None:
    os.environ.update(env())
    from ticketsbot.config import Settings
    from ticketsbot.db import Database
    from ticketsbot.models import Role

    settings = Settings()
    db = Database(settings)
    await db.initialize()
    async with db.session() as session:
        session.add_all([
            Role(tg_id="1001", name="Сотрудник HTTP", role="сотрудник", username="http_user", allowed_types_json='["Касса"]'),
            Role(tg_id="2001", name="Администратор HTTP", role="админ", username="http_admin", allowed_types_json="[]"),
        ])
        await session.commit()
    await db.close()


async def verify_db(number: str) -> dict:
    os.environ.update(env())
    from ticketsbot.config import Settings
    from ticketsbot.db import Database
    from ticketsbot.models import NotificationOutbox, SheetSyncOutbox, Ticket, TicketEvent

    db = Database(Settings())
    await db.initialize()
    async with db.session() as session:
        ticket = (await session.execute(select(Ticket).where(Ticket.number == number))).scalar_one()
        event_count = (await session.execute(select(func.count()).select_from(TicketEvent).where(TicketEvent.ticket_id == ticket.id))).scalar_one()
        notification_count = (await session.execute(select(func.count()).select_from(NotificationOutbox))).scalar_one()
        sheet_count = (await session.execute(select(func.count()).select_from(SheetSyncOutbox))).scalar_one()
        result = {"status": ticket.status, "events": event_count, "notification_outbox": notification_count, "sheet_outbox": sheet_count}
    await db.close()
    return result


def main() -> None:
    if RUNTIME.exists():
        import shutil
        shutil.rmtree(RUNTIME)
    MEDIA.mkdir(parents=True)
    asyncio.run(seed())

    log_path = RUNTIME / "uvicorn.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "ticketsbot.runtime"], cwd=ROOT, env=env(), stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(timeout=10) as client:
                for _ in range(60):
                    try:
                        if client.get(BASE_URL + "/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("uvicorn did not become ready")

                employee = signed(1001, "Сотрудник HTTP", "http_user")
                admin = signed(2001, "Администратор HTTP", "http_admin")

                def call(action: str, init_data: str, **payload):
                    response = client.post(BASE_URL + "/", json={"action": action, "init_data": init_data, **payload})
                    response.raise_for_status()
                    body = response.json()
                    if not body.get("success"):
                        raise RuntimeError(f"{action}: {body}")
                    return body["data"]

                health = client.get(BASE_URL + "/health").json()
                role = call("getRole", employee)
                image = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nsmoke").decode()
                created = call("createTicket", employee, type="Касса", city="Пермь", office="1", name="Сотрудник HTTP", description="Реальный TCP HTTP smoke", image=image, filename="smoke.png")
                number = created["ticket"]["number"]
                call("takeTicket", admin, number=number)
                call("pauseTicket", admin, number=number)
                call("resumeTicket", admin, number=number)
                call("returnTicket", admin, number=number, reason="Проверить ещё раз")
                call("resubmitTicket", employee, number=number, type="Касса", city="Пермь", office="1", name="Сотрудник HTTP", description="Исправлено")
                call("takeTicket", admin, number=number)
                finished = call("finishTicket", admin, number=number, comment="Готово через настоящий HTTP")
                history = call("getHistory", admin, q=number)

                file_url = created["ticket"].get("fileUrl", "")
                token = file_url.rsplit("/", 1)[-1]
                session_response = client.post(BASE_URL + "/media/session", json={"token": token}, headers={"X-Telegram-Init-Data": employee})
                session_response.raise_for_status()
                signed_url = session_response.json()["url"]
                media_response = client.get(signed_url)
                media_response.raise_for_status()

                db_result = asyncio.run(verify_db(number))
                output = {
                    "health": health,
                    "role": role.get("role"),
                    "ticket": number,
                    "final_status": finished["ticket"]["status"],
                    "history_total": history["total"],
                    "media_status": media_response.status_code,
                    "media_bytes": len(media_response.content),
                    "db": db_result,
                }
                print(json.dumps(output, ensure_ascii=False, indent=2))
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.returncode not in (0, -15, 1):
                raise RuntimeError(f"uvicorn exited unexpectedly: {process.returncode}")


if __name__ == "__main__":
    main()
