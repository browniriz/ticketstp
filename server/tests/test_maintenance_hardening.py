import asyncio
import sqlite3

import pytest

from ticketsbot.config import Settings
from ticketsbot.db import Database, MigrationError


@pytest.mark.parametrize("name,ddl,rows", [
    ("duplicate.db", "id INTEGER PRIMARY KEY, number VARCHAR(4), status VARCHAR(30)",
     [(1, "L001", "создана"), (2, "L001", "создана")]),
    ("partial.db", "id INTEGER PRIMARY KEY, number VARCHAR(4), status VARCHAR(30), city TEXT",
     [(1, "L001", "создана", "Пермь")]),
])
def test_migration_preflight_fails_closed_and_keeps_safety_backup(tmp_path, name, ddl, rows):
    path = tmp_path / name
    connection = sqlite3.connect(path)
    connection.execute(f"CREATE TABLE tickets ({ddl})")
    placeholders = ",".join("?" for _ in rows[0])
    connection.executemany(f"INSERT INTO tickets VALUES ({placeholders})", rows)
    connection.commit()
    connection.close()

    database = Database(Settings(database_url=f"sqlite+aiosqlite:///{path}"))
    with pytest.raises(MigrationError):
        asyncio.run(database.initialize())
    asyncio.run(database.close())

    assert list(tmp_path.glob(f"{name}.pre-migration-*.bak"))
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        connection.close()


def test_revision_three_database_upgrades_bridge_sequences(tmp_path):
    path = tmp_path / "revision3.db"
    database = Database(Settings(database_url=f"sqlite+aiosqlite:///{path}"))
    asyncio.run(database.initialize())
    asyncio.run(database.close())
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE bridge_sequences")
    connection.execute("PRAGMA user_version=3")
    connection.commit()
    connection.close()

    database = Database(Settings(database_url=f"sqlite+aiosqlite:///{path}"))
    asyncio.run(database.initialize())
    asyncio.run(database.close())
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        columns = {row[1] for row in connection.execute("PRAGMA table_info(bridge_sequences)")}
        assert columns == {"entity_key", "version"}
    finally:
        connection.close()


def test_current_revision_still_validates_physical_schema(tmp_path):
    path = tmp_path / "lying-current.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE tickets (id INTEGER PRIMARY KEY)")
    connection.execute("PRAGMA user_version=6")
    connection.commit()
    connection.close()

    database = Database(Settings(database_url=f"sqlite+aiosqlite:///{path}"))
    with pytest.raises(MigrationError, match="missing tables"):
        asyncio.run(database.initialize())
    asyncio.run(database.close())
