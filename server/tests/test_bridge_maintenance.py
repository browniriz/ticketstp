import importlib.util
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from ticketsbot.config import Settings
from ticketsbot.models import Role, Ticket, TicketTombstone
from ticketsbot.workers import ROLE_HEADERS, WorkerManager, ticket_row

SPEC = importlib.util.spec_from_file_location("maintenance", Path(__file__).parents[1] / "scripts" / "maintenance.py")
maintenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(maintenance)


class SnapshotBridge:
    def __init__(self, rows, revision=1): self.rows, self.revision = rows, revision
    async def call(self, action, **payload):
        assert action == "bridgePullRoles"
        canonical=json.dumps({"headers":ROLE_HEADERS,"rows":self.rows},ensure_ascii=False,separators=(",",":"))
        return {"headers": ROLE_HEADERS, "rows": self.rows, "revision":self.revision,
                "count":len(self.rows),"hash":hashlib.sha256(canonical.encode()).hexdigest()}


@pytest.mark.asyncio
async def test_empty_roles_snapshot_cannot_erase_authority(app):
    await app.state.db.initialize()
    async with app.state.db.session() as session:
        session.add(Role(tg_id="1", name="Admin", role="админ"))
        await session.commit()
    worker = WorkerManager(app.state.db, app.state.settings, bridge=SnapshotBridge([]))
    with pytest.raises(ValueError, match="empty"):
        await worker.pull_roles_once()
    async with app.state.db.session() as session:
        assert (await session.get(Role, "1")).role == "админ"


@pytest.mark.asyncio
async def test_roles_snapshot_rejects_wrong_headers_and_duplicates(app):
    bridge = SnapshotBridge([["1", "A", "админ", "", ""] + [True] * 9] * 2)
    worker = WorkerManager(app.state.db, app.state.settings, bridge=bridge)
    with pytest.raises(ValueError, match="duplicate"):
        await worker.pull_roles_once()


def test_ticket_row_exact_local_format_and_durations():
    ticket = Ticket(number="A001", created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
                    type="Касса", city="Пермь", office="1", sender_name="N", description="D",
                    status="создана", creator_id="1", elapsed_seconds=65,
                    updated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc), idle_seconds=3601)
    row = ticket_row(ticket)
    assert len(row) == 18
    assert row[1] == "02.01.2026 08:04:05" and row[14] == row[1]
    assert row[12] == "01:05" and row[15] == "60:01"


def test_import_parser_treats_sheet_text_as_yekaterinburg():
    assert maintenance.date("02.01.2026 08:04:05") == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert maintenance.duration("60:01") == 3601
    with pytest.raises(ValueError): maintenance.duration("1:99")


@pytest.mark.asyncio
async def test_import_is_idempotent_by_ticket_number(tmp_path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    source = tmp_path / "tickets.csv"
    row = ["A001", "02.01.2026 08:04:05", "Касса", "Пермь", "1", "N", "D", "создана",
           "1", "", "", "", "01:05", "", "02.01.2026 08:04:05", "", "", ""]
    source.write_text(",".join(row), encoding="utf-8")
    assert (await maintenance.import_csv(settings, source))["inserted"] == 1
    result = await maintenance.import_csv(settings, source)
    assert result == {"inserted": 0, "updated": 0, "unchanged": 1, "total": 1}
    db = maintenance.Database(settings)
    async with db.session() as session:
        assert (await session.execute(select(Ticket))).scalar_one().version == 1
    await db.close()


def test_backup_verified_atomic_and_includes_media(tmp_path):
    source = tmp_path / "source.sqlite"
    connection = sqlite3.connect(source)
    try:
        for table in maintenance.REQUIRED_TABLES:
            connection.execute(f'CREATE TABLE "{table}" (id INTEGER)')
        connection.execute("INSERT INTO tickets VALUES (1)")
        connection.commit()
    finally: connection.close()
    media = tmp_path / "media"; media.mkdir(); (media / "file.bin").write_bytes(b"x")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{source}", media_dir=media)
    destination = tmp_path / "backup.sqlite"
    result = maintenance.backup(settings, destination)
    assert result["integrity_check"] == "ok" and result["tables"]["tickets"] == 1
    assert result["media_files"] == 1 and (Path(str(destination) + ".media") / "file.bin").read_bytes() == b"x"
    assert result["files"][0]["size"] == 1
    assert len(result["files"][0]["sha256"]) == 64
    assert json.loads(Path(str(destination) + ".json").read_text(encoding="utf-8"))["integrity_check"] == "ok"
    with pytest.raises(FileExistsError): maintenance.backup(settings, destination)


class TicketBridge:
    def __init__(self, rows): self.rows, self.calls = rows, []
    async def call(self, action, **payload):
        self.calls.append((action, payload))
        if action == "bridgePullTickets": return {"rows": self.rows}
        assert len(payload["rows"]) == len(payload["dedupe_keys"])
        return {"upserted": len(payload["rows"]), "acknowledgments": [
            {"number": row[0], "sequence": sequence, "dedupe_key": key}
            for row, sequence, key in zip(payload["rows"], payload["sequences"], payload["dedupe_keys"])
        ]}


@pytest.mark.asyncio
async def test_reconcile_canonicalizes_formula_escape_but_preserves_user_apostrophe(tmp_path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'formula.db'}")
    db = maintenance.Database(settings); await db.initialize()
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    async with db.session() as session:
        session.add_all([
            Ticket(number="A001", created_at=now, type="Касса", city="Пермь", office="1",
                   sender_name="N", description="=safe text", status="создана", creator_id="1", updated_at=now),
            Ticket(number="A002", created_at=now, type="Касса", city="Пермь", office="1",
                   sender_name="N", description="'=literal quote", status="создана", creator_id="1", updated_at=now),
        ]); await session.commit()
        tickets = list((await session.execute(select(Ticket).order_by(Ticket.number))).scalars())
        rows = [ticket_row(ticket) for ticket in tickets]
    await db.close()
    rows[0][6] = "'=safe text"       # one bridge-added escape
    rows[1][6] = "''=literal quote"  # escape plus legitimate apostrophe
    report = await maintenance.reconcile(settings, bridge=TicketBridge(rows))
    assert report["summary"]["matching"] == 2
    assert report["summary"]["different"] == 0


@pytest.mark.asyncio
async def test_reconcile_reports_both_sides_and_applies_only_differences(tmp_path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'reconcile.db'}")
    db = maintenance.Database(settings); await db.initialize()
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    async with db.session() as session:
        session.add_all([
            Ticket(number="A001", created_at=now, type="Касса", city="Пермь", office="1",
                   sender_name="N", description="D", status="создана", creator_id="1", updated_at=now),
            Ticket(number="A002", created_at=now, type="Касса", city="Пермь", office="1",
                   sender_name="N", description="D", status="в работе", creator_id="1", updated_at=now),
        ]); await session.commit()
    async with db.session() as session:
        rows = [ticket_row((await session.execute(
            select(Ticket).where(Ticket.number == "A001")
        )).scalar_one())]
    rows[0][7] = "решена"
    rows.append(["A003", "02.01.2026 08:04:05", "Касса", "Пермь", "1", "N", "D", "создана",
                 "1", "", "", "", "00:00", "", "02.01.2026 08:04:05", "", "", ""])
    await db.close()
    bridge = TicketBridge(rows)
    report = await maintenance.reconcile(settings, apply=True, bridge=bridge)
    assert report["summary"] == {"local_total": 2, "sheet_total": 2, "missing_in_sheet": 1,
                                  "extra_in_sheet": 1, "different": 1, "matching": 0}
    assert report["details"]["missing_in_sheet"] == ["A002"]
    assert report["details"]["extra_in_sheet"] == ["A003"]
    assert report["status_counts"]["local"] == {"в работе": 1, "создана": 1}
    assert report["applied"] and report["upserted"] == 2
    batch_payload = bridge.calls[-1][1]
    assert len(batch_payload["rows"]) == len(batch_payload["dedupe_keys"]) == 2
    assert all(key.startswith("reconcile:") for key in batch_payload["dedupe_keys"])


@pytest.mark.asyncio
async def test_reconcile_rejects_mismatched_batch_ack(tmp_path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'bad-ack.db'}")
    db = maintenance.Database(settings); await db.initialize()
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    async with db.session() as session:
        session.add(Ticket(number="A001", created_at=now, type="Касса", city="Пермь", office="1",
                           sender_name="N", description="D", status="создана", creator_id="1", updated_at=now))
        await session.commit()
    await db.close()

    class BadAckBridge(TicketBridge):
        async def call(self, action, **payload):
            if action == "bridgePullTickets": return {"rows": []}
            return {"upserted": 1, "acknowledgments": [
                {"number": "Z999", "sequence": payload["sequences"][0],
                 "dedupe_key": payload["dedupe_keys"][0]}
            ]}

    with pytest.raises(RuntimeError, match="mismatched batch acknowledgment"):
        await maintenance.reconcile(settings, apply=True, bridge=BadAckBridge([]))


@pytest.mark.parametrize("bad_field", ["dedupe_key", "current_sequence"])
@pytest.mark.asyncio
async def test_reconcile_delete_rejects_wrong_ack_identity(tmp_path, bad_field):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / ('delete-' + bad_field + '.db')}")
    db = maintenance.Database(settings); await db.initialize()
    async with db.session() as session:
        session.add(TicketTombstone(number="A001", ticket_id=1, snapshot_json="{}", sequence=7))
        await session.commit()
    await db.close()
    sheet_row = ["A001", "02.01.2026 08:04:05", "Касса", "Пермь", "1", "N", "D", "решена",
                 "1", "", "", "", "00:00", "", "02.01.2026 08:04:05", "", "", ""]

    class BadDeleteAckBridge:
        async def call(self, action, **payload):
            if action == "bridgePullTickets":
                return {"rows": [sheet_row]}
            assert action == "bridgeDeleteTicket"
            ack = {"number": payload["number"], "sequence": payload["sequence"],
                   "current_sequence": payload["sequence"], "dedupe_key": payload["dedupe_key"],
                   "absent": True}
            ack[bad_field] = "wrong" if bad_field == "dedupe_key" else payload["sequence"] + 1
            return ack

    with pytest.raises(RuntimeError, match="tombstoned ticket absence"):
        await maintenance.reconcile(settings, apply=True, bridge=BadDeleteAckBridge())


def test_apps_script_bridge_and_role_revision_static_contract():
    source = (Path(__file__).parents[2] / "Code.gs").read_text(encoding="utf-8")
    assert "if (action === 'bridgePullTickets') return bridgeTicketsSnapshot_();" in source
    pull = source[source.index("function bridgeTicketsSnapshot_"):source.index("function bridgeRolesSnapshot_")]
    assert "TICKETS_HEADERS" in pull and "getRange(2, 1, lastRow - 1, 18)" in pull
    assert "count: rows.length" in pull and "hash: hash" in pull
    assert "canonicalBridgeTicketRow_" in pull
    delete = source[source.index("function bridgeDeleteTicket_"):source.index("function bridgeBatchUpsertTickets_")]
    assert delete.index("invalidateTicketCache_(); invalidateRowMap_();") < delete.index("absent: !buildRowMap_(sh)[number]")
    mirror = source[source.index("function bridgeMirrorAccess_"):source.index("// Прогон проверки")]
    assert "bumpRolesRevision_();" not in mirror
    boundaries = (("upsertRole_", "function updateRoleContact_"),
                  ("updateRoleContact_", "// ============================ TICKETS"),
                  ("removeRole_", "// Ручные изменения"))
    for name, following in boundaries:
        start = source.index("function " + name)
        block = source[start:source.index(following, start)]
        assert block.count("bumpRolesRevision_();") == 1
    revoke = source[source.index("function revokeAccess_"):source.index("function removeRole_")]
    assert "target.role === 'админ'" in revoke and "length <= 1" in revoke


def test_apps_script_formula_round_trip_and_dedupe_repair_contract():
    source = (Path(__file__).parents[2] / "Code.gs").read_text(encoding="utf-8")
    formula = source[source.index("function plainText_"):source.index("function bridgeStateKey_")]
    assert "BRIDGE_TICKET_TEXT_COLUMNS" in formula
    assert "canonicalBridgeTicketRow_" in formula
    assert '"=text" -> "\'=text", while "\'=text" -> "\'\'=text"' in formula
    batch = source[source.index("function bridgeBatchUpsertTickets_"):source.index("function bridgeMirrorAccess_")]
    assert "currentMatches" in batch
    assert "JSON.stringify(current) === JSON.stringify(desired)" in batch


def test_apps_script_init_data_freshness_static_contract():
    source = (Path(__file__).parents[2] / "Code.gs").read_text(encoding="utf-8")
    auth = source[source.index("function verifyInitData_"):source.index("function bytesToHex_")]
    assert "INIT_DATA_MAX_AGE_SECONDS" in auth and "INIT_DATA_FUTURE_SKEW_SECONDS" in auth
    assert "authDate < nowSeconds - maxAge" in auth
    assert "authDate > nowSeconds + futureSkew" in auth
    setup = source[source.index("function setupSecrets"):source.index("// ============================ SHEETS")]
    assert "INIT_DATA_MAX_AGE_SECONDS: ''" in setup
    assert "INIT_DATA_FUTURE_SKEW_SECONDS: ''" in setup
