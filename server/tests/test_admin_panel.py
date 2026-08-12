import base64
import hashlib
import json
import secrets
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ticketsbot.app import create_app
from ticketsbot.config import Settings
from ticketsbot.models import (AdminAudit, AdminSession, Attachment, BridgeSequence,
                               NotificationOutbox, Role, SheetSyncOutbox, Ticket, TicketEvent,
                               TicketTombstone, utcnow)
from ticketsbot.workers import WorkerManager


def password_hash(password: str) -> str:
    salt = b"admin-test-salt"
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.urlsafe_b64encode(salt).decode().rstrip("=") + "$" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


@pytest.fixture
def admin_app(tmp_path):
    return create_app(Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'admin.db'}",
        media_dir=tmp_path / "media",
        admin_password_hash=password_hash("correct horse"),
        admin_session_secret="s" * 48,
        admin_allowed_origin="https://testserver",
        admin_session_ttl_seconds=60,
        admin_login_max_attempts=2,
    ))


@pytest.fixture
def admin_client(admin_app):
    with TestClient(admin_app, base_url="https://testserver") as client:
        yield client


def login(client, password="correct horse"):
    return client.post("/admin/api/login", json={"password": password},
                       headers={"Origin": "https://testserver"})


def csrf_headers(token):
    return {"Origin": "https://testserver", "X-CSRF-Token": token}


def test_admin_panel_static_route_is_hardened(admin_client):
    redirect = admin_client.get("/admin", follow_redirects=False)
    assert redirect.status_code == 308 and redirect.headers["location"] == "/admin/"
    response = admin_client.get("/admin/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "script-src 'sha256-" in response.headers["content-security-policy"]
    assert "script-src 'unsafe-inline'" not in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert '<html lang="ru">' in response.text
    assert 'const API = "/admin/api"' in response.text and 'API+"/session"' in response.text
    assert 'id="accessTab"' in response.text and 'id="addAccessForm"' in response.text
    assert 'api("/access"' in response.text and 'loadAccess()' in response.text
    assert "localStorage" not in response.text


async def seed_ticket(app, number="A001", status="решена"):
    await app.state.db.initialize()
    async with app.state.db.session() as session:
        ticket = Ticket(number=number, type="Касса", city="Пермь", office="1", sender_name="N",
                        description="D", status=status, creator_id="1")
        session.add(ticket); await session.flush()
        session.add(TicketEvent(ticket_id=ticket.id, actor_tg_id="1", event="created"))
        await session.commit()
        return ticket.id


def test_login_cookie_is_hardened_and_invalid_cookie_fails(admin_client):
    assert admin_client.get("/admin/api/tickets").status_code == 401
    response = login(admin_client)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    admin_client.cookies.set("ticketsbot_admin", "forged", domain="testserver.local")
    assert admin_client.get("/admin/api/tickets").status_code == 401


def test_state_changes_require_same_origin(admin_client):
    assert admin_client.post("/admin/api/login", json={"password": "correct horse"}).status_code == 403
    assert admin_client.post("/admin/api/login", json={"password": "correct horse"},
                             headers={"Origin": "https://evil.example"}).status_code == 403
    response = admin_client.post("/admin/api/login", json={"password": "correct horse"},
                                 headers={"Referer": "https://testserver/admin/"})
    assert response.status_code == 200


def test_configured_admin_origin_is_allowed(tmp_path):
    app = create_app(Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'origin.db'}",
        media_dir=tmp_path / "media", admin_password_hash=password_hash("correct horse"),
        admin_session_secret="s" * 48, admin_allowed_origin="https://admin.example",
    ))
    with TestClient(app, base_url="https://api.example") as client:
        assert client.post("/admin/api/login", json={"password": "correct horse"},
                           headers={"Origin": "https://admin.example"}).status_code == 200


@pytest.mark.parametrize("origin", ["", "http://admin.example", "https://admin.example/path",
                                    "https://user@admin.example", "not-a-url"])
def test_admin_configuration_requires_exact_https_origin(tmp_path, origin):
    app = create_app(Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'bad-origin.db'}",
        admin_password_hash=password_hash("correct horse"), admin_session_secret="s" * 48,
        admin_allowed_origin=origin,
    ))
    with pytest.raises(ValueError, match="admin_allowed_origin"):
        with TestClient(app):
            pass


def test_session_endpoint_and_csrf_are_per_session(admin_app):
    with TestClient(admin_app, base_url="https://testserver") as first, \
         TestClient(admin_app, base_url="https://testserver") as second:
        first_token = login(first).json()["csrf_token"]
        second_token = login(second).json()["csrf_token"]
        assert first_token != second_token
        status = first.get("/admin/api/session")
        assert status.status_code == 200
        assert status.json()["csrf_token"] == first_token
        assert first.post("/admin/api/logout", headers={"Origin": "https://testserver"}).status_code == 403
        assert first.post("/admin/api/logout", headers=csrf_headers(second_token)).status_code == 403
        assert first.post("/admin/api/logout", headers=csrf_headers(first_token)).status_code == 200
        assert first.get("/admin/api/session").status_code == 401


def test_login_rate_limit(admin_client):
    assert login(admin_client, "bad").status_code == 401
    assert login(admin_client, "bad").status_code == 401
    assert login(admin_client, "bad").status_code == 429


@pytest.mark.asyncio
async def test_expired_session_rejected(admin_app):
    with TestClient(admin_app, base_url="https://testserver") as client:
        assert login(client).status_code == 200
        async with admin_app.state.db.session() as session:
            row = (await session.execute(select(AdminSession))).scalar_one()
            row.expires_at = utcnow() - timedelta(seconds=1); await session.commit()
        assert client.get("/admin/api/tickets").status_code == 401


def test_revision_migrates_admin_schema(tmp_path):
    path = tmp_path / "legacy.db"
    settings = Settings(database_url=f"sqlite+aiosqlite:///{path}")
    import asyncio
    from ticketsbot.db import Database
    initial = Database(settings); asyncio.run(initial.initialize()); asyncio.run(initial.close())
    db = sqlite3.connect(path)
    db.execute("DROP TABLE admin_sessions"); db.execute("DROP TABLE admin_audit")
    db.execute("DROP TABLE ticket_tombstones")
    db.execute("DROP INDEX ix_tickets_archived_at")
    db.execute("ALTER TABLE tickets DROP COLUMN archived_at")
    db.execute("DROP INDEX ix_attachments_quarantined_at")
    db.execute("DROP INDEX ix_attachments_retain_until")
    db.execute("ALTER TABLE attachments DROP COLUMN quarantined_at")
    db.execute("ALTER TABLE attachments DROP COLUMN retain_until")
    db.execute("PRAGMA user_version=4"); db.commit(); db.close()
    app = create_app(settings)
    with TestClient(app):
        pass
    db = sqlite3.connect(path)
    names = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
    assert {"admin_sessions", "admin_audit", "ticket_tombstones"} <= names
    assert "csrf_secret" in {r[1] for r in db.execute("PRAGMA table_info(admin_sessions)")}
    assert db.execute("PRAGMA user_version").fetchone()[0] >= 6
    db.close()


@pytest.mark.asyncio
async def test_read_only_list_detail_events_and_outbox(admin_app):
    ticket_id = await seed_ticket(admin_app)
    async with admin_app.state.db.session() as session:
        session.add(SheetSyncOutbox(dedupe_key="ticket:1:1", entity_type="ticket", entity_id=str(ticket_id),
                                    operation="upsert", payload_json="{}")); await session.commit()
    with TestClient(admin_app, base_url="https://testserver") as client:
        login(client)
        listed = client.get("/admin/api/tickets", params={"q": "A001", "status": "решена"})
        assert listed.status_code == 200 and listed.json()["total"] == 1
        assert listed.json()["summary"]["решена"] == 1
        detail = client.get("/admin/api/tickets/A001").json()
        assert detail["ticket"]["number"] == "A001"
        assert detail["events"][0]["event"] == "created"
        assert detail["outbox"][0]["operation"] == "upsert"


@pytest.mark.asyncio
async def test_access_list_search_and_add_employee_are_audited(admin_app):
    await admin_app.state.db.initialize()
    async with admin_app.state.db.session() as session:
        session.add_all([
            Role(tg_id="100", name="Анна Админ", role="админ", username="anna"),
            Role(tg_id="200", name="Борис", role="сотрудник", username="boris"),
        ])
        await session.commit()
    with TestClient(admin_app, base_url="https://testserver") as client:
        csrf = login(client).json()["csrf_token"]
        listed = client.get("/admin/api/access")
        assert listed.status_code == 200
        assert listed.json()["total"] == 2
        assert {item["role"] for item in listed.json()["items"]} == {"админ", "сотрудник"}
        assert client.get("/admin/api/access", params={"q": "200"}).json()["items"][0]["name"] == "Борис"
        assert client.get("/admin/api/access", params={"q": "ANNA"}).json()["items"][0]["tg_id"] == "100"
        assert client.get("/admin/api/access", params={"q": "%"}).json()["total"] == 0
        created = client.post("/admin/api/access", json={"tg_id": "300", "name": "Светлана"},
                              headers=csrf_headers(csrf))
        assert created.status_code == 201
        assert created.json()["employee"] == {"tg_id": "300", "name": "Светлана",
                                                "role": "сотрудник", "username": ""}
        assert created.json()["google_pending"] is True
        assert client.post("/admin/api/access", json={"tg_id": "300", "name": "Дубликат"},
                           headers=csrf_headers(csrf)).status_code == 409
    async with admin_app.state.db.session() as session:
        role = await session.get(Role, "300")
        assert role is not None and role.role == "сотрудник" and role.name == "Светлана"
        outbox = (await session.execute(select(SheetSyncOutbox).where(
            SheetSyncOutbox.entity_type == "role", SheetSyncOutbox.entity_id == "300"))).scalar_one()
        assert outbox.operation == "approve"
        assert json.loads(outbox.payload_json)["role"] == "сотрудник"
        audit = (await session.execute(select(AdminAudit).where(
            AdminAudit.action == "add_employee_access"))).scalar_one()
        assert json.loads(audit.payload_json) == {"tg_id": "300", "name": "Светлана",
                                                  "role": "сотрудник"}


def test_add_employee_access_requires_csrf_and_valid_fields(admin_client):
    csrf = login(admin_client).json()["csrf_token"]
    assert admin_client.post("/admin/api/access", json={"tg_id": "123", "name": "Иван"}).status_code == 403
    for body in ({"tg_id": "abc", "name": "Иван"}, {"tg_id": "0", "name": "Иван"},
                 {"tg_id": "123", "name": ""}, {"tg_id": str(2 ** 52), "name": "Иван"},
                 {"tg_id": "123", "name": "Иван\u202e"}, {"tg_id": "123", "name": "Иван\nПетров"}):
        assert admin_client.post("/admin/api/access", json=body,
                                 headers=csrf_headers(csrf)).status_code == 422
    assert admin_client.post("/admin/api/access", json={"tg_id": "123", "name": "ﷺ" * 80},
                             headers=csrf_headers(csrf)).status_code == 422


@pytest.mark.asyncio
async def test_archive_and_safe_delete_are_audited_and_transactional(admin_app):
    ticket_id = await seed_ticket(admin_app)
    admin_app.state.settings.media_dir.mkdir(parents=True, exist_ok=True)
    (admin_app.state.settings.media_dir / "safe.bin").write_bytes(b"media")
    async with admin_app.state.db.session() as session:
        session.add(Attachment(token="t", ticket_id=ticket_id, stored_name="safe.bin", original_name="a.bin",
                               mime_type="application/octet-stream", size=5)); await session.commit()
    with TestClient(admin_app, base_url="https://testserver") as client:
        csrf = login(client).json()["csrf_token"]
        assert client.post("/admin/api/tickets/A001/archive", headers=csrf_headers(csrf)).status_code == 200
        assert client.get("/admin/api/tickets").json()["total"] == 0
        included = client.get("/admin/api/tickets", params={"include_archived": "true"}).json()
        assert included["total"] == 1 and included["summary"] == {"решена": 1}
        assert client.request("DELETE", "/admin/api/tickets/A001", json={"confirm_number": "A002"},
                              headers=csrf_headers(csrf)).status_code == 400
        assert client.request("DELETE", "/admin/api/tickets/A001", json={"confirm_number": "A001"},
                              headers=csrf_headers(csrf)).status_code == 200
    async with admin_app.state.db.session() as session:
        assert await session.get(Ticket, ticket_id) is None
        tomb = (await session.execute(select(TicketTombstone))).scalar_one()
        assert tomb.number == "A001" and tomb.google_deleted_at is None
        outbox = (await session.execute(select(SheetSyncOutbox))).scalar_one()
        assert outbox.operation == "delete_ticket" and json.loads(outbox.payload_json)["number"] == "A001"
        attachment = (await session.execute(select(Attachment))).scalar_one()
        assert attachment.ticket_id is None and attachment.quarantined_at is not None
        actions = list((await session.execute(select(AdminAudit.action))).scalars())
        assert actions == ["archive_ticket", "delete_ticket"]


@pytest.mark.asyncio
async def test_delete_sequence_is_newer_than_imported_ticket_version(admin_app):
    ticket_id = await seed_ticket(admin_app)
    async with admin_app.state.db.session() as session:
        ticket = await session.get(Ticket, ticket_id)
        ticket.version = 5
        await session.commit()
    with TestClient(admin_app, base_url="https://testserver") as client:
        csrf = login(client).json()["csrf_token"]
        response = client.request("DELETE", "/admin/api/tickets/A001",
                                  json={"confirm_number": "A001"}, headers=csrf_headers(csrf))
        assert response.status_code == 200
    async with admin_app.state.db.session() as session:
        tombstone = (await session.execute(select(TicketTombstone))).scalar_one()
        outbox = (await session.execute(select(SheetSyncOutbox))).scalar_one()
        assert tombstone.sequence == 6
        assert json.loads(outbox.payload_json)["sequence"] == 6


@pytest.mark.asyncio
async def test_delete_sequence_continues_higher_local_counter(admin_app):
    ticket_id = await seed_ticket(admin_app)
    async with admin_app.state.db.session() as session:
        session.add(BridgeSequence(entity_key="ticket:A001", version=12))
        await session.commit()
    with TestClient(admin_app, base_url="https://testserver") as client:
        csrf = login(client).json()["csrf_token"]
        response = client.request("DELETE", "/admin/api/tickets/A001",
                                  json={"confirm_number": "A001"}, headers=csrf_headers(csrf))
        assert response.status_code == 200
    async with admin_app.state.db.session() as session:
        tombstone = (await session.execute(select(TicketTombstone))).scalar_one()
        assert tombstone.sequence == 13


@pytest.mark.asyncio
async def test_delete_rejects_open_ticket(admin_app):
    await seed_ticket(admin_app, "A002", "в работе")
    with TestClient(admin_app, base_url="https://testserver") as client:
        csrf = login(client).json()["csrf_token"]
        assert client.request("DELETE", "/admin/api/tickets/A002", json={"confirm_number": "A002"},
                              headers=csrf_headers(csrf)).status_code == 409


class DeleteBridge:
    def __init__(self): self.calls = []
    async def call(self, action, **payload):
        self.calls.append((action, payload))
        return {"number": payload["number"], "sequence": payload["sequence"],
                "current_sequence": payload["sequence"], "dedupe_key": payload["dedupe_key"],
                "deleted": False, "absent": True}


@pytest.mark.asyncio
async def test_worker_handles_delete_and_acknowledges_tombstone(admin_app):
    await seed_ticket(admin_app)
    with TestClient(admin_app, base_url="https://testserver") as client:
        csrf = login(client).json()["csrf_token"]
        client.request("DELETE", "/admin/api/tickets/A001", json={"confirm_number": "A001"},
                       headers=csrf_headers(csrf))
    bridge = DeleteBridge(); worker = WorkerManager(admin_app.state.db, admin_app.state.settings, bridge=bridge)
    await worker.sheets_once()
    assert bridge.calls[0][0] == "bridgeDeleteTicket"
    async with admin_app.state.db.session() as session:
        assert (await session.execute(select(SheetSyncOutbox))).scalar_one().delivered
        assert (await session.execute(select(TicketTombstone))).scalar_one().google_deleted_at is not None


class WrongDeleteSequenceBridge(DeleteBridge):
    async def call(self, action, **payload):
        ack = await super().call(action, **payload)
        ack["current_sequence"] = payload["sequence"] + 1
        return ack


@pytest.mark.asyncio
async def test_delete_worker_rejects_mismatched_current_sequence(admin_app):
    await seed_ticket(admin_app)
    with TestClient(admin_app, base_url="https://testserver") as client:
        csrf = login(client).json()["csrf_token"]
        client.request("DELETE", "/admin/api/tickets/A001", json={"confirm_number": "A001"},
                       headers=csrf_headers(csrf))
    await WorkerManager(admin_app.state.db, admin_app.state.settings,
                        bridge=WrongDeleteSequenceBridge()).sheets_once()
    async with admin_app.state.db.session() as session:
        outbox = (await session.execute(select(SheetSyncOutbox))).scalar_one()
        tombstone = (await session.execute(select(TicketTombstone))).scalar_one()
        assert not outbox.delivered and "mismatched ticket delete acknowledgment" in outbox.last_error
        assert tombstone.google_deleted_at is None


@pytest.mark.parametrize("invalid_state", ["missing_tombstone", "sequence_mismatch", "local_ticket"])
@pytest.mark.asyncio
async def test_delete_worker_requires_local_postconditions(admin_app, invalid_state):
    await seed_ticket(admin_app)
    with TestClient(admin_app, base_url="https://testserver") as client:
        csrf = login(client).json()["csrf_token"]
        client.request("DELETE", "/admin/api/tickets/A001", json={"confirm_number": "A001"},
                       headers=csrf_headers(csrf))
    async with admin_app.state.db.session() as session:
        tombstone = (await session.execute(select(TicketTombstone))).scalar_one()
        if invalid_state == "missing_tombstone":
            await session.delete(tombstone)
        elif invalid_state == "sequence_mismatch":
            tombstone.sequence += 1
        else:
            session.add(Ticket(number="A001", type="Касса", city="Пермь", office="1",
                               sender_name="N", description="D", status="решена", creator_id="1"))
        await session.commit()
    await WorkerManager(admin_app.state.db, admin_app.state.settings, bridge=DeleteBridge()).sheets_once()
    async with admin_app.state.db.session() as session:
        outbox = (await session.execute(select(SheetSyncOutbox))).scalar_one()
        tombstone = (await session.execute(select(TicketTombstone))).scalar_one_or_none()
        assert not outbox.delivered and "delete postcondition" in outbox.last_error
        assert tombstone is None or tombstone.google_deleted_at is None
