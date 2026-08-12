from __future__ import annotations

import shutil
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import UniqueConstraint, event, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateIndex

from .config import Settings
from .models import Base

SCHEMA_REVISION = 4
SAFE_LEGACY_TICKET_COLUMNS = {"id", "number", "status"}


class MigrationError(RuntimeError):
    pass


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine: AsyncEngine = create_async_engine(settings.database_url)
        if settings.database_url.startswith("sqlite"):
            @event.listens_for(self.engine.sync_engine, "connect")
            def configure_sqlite(dbapi_connection, _record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()
        self.sessions = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession,
            info={"settings": settings},
        )

    def _safety_backup(self) -> str | None:
        """Create an automatic SQLite snapshot before upgrading any legacy DB."""
        path = self.settings.sqlite_path
        if not path or not path.exists() or path.stat().st_size == 0:
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = path.with_name(f"{path.name}.pre-migration-{stamp}.bak")
        source = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MigrationError("automatic pre-migration backup failed integrity check")
        except Exception:
            target.close()
            destination.unlink(missing_ok=True)
            raise
        finally:
            if target:
                try: target.close()
                except Exception: pass
            source.close()
        return str(destination)

    @staticmethod
    def _preflight(sync_connection) -> None:
        inspector = inspect(sync_connection)
        if "tickets" not in inspector.get_table_names():
            return
        columns = {column["name"] for column in inspector.get_columns("tickets")}
        if "number" not in columns:
            raise MigrationError("legacy tickets table has no required number column")
        duplicate = sync_connection.exec_driver_sql(
            "SELECT number, count(*) FROM tickets GROUP BY number HAVING count(*) > 1 LIMIT 1"
        ).first()
        if duplicate:
            raise MigrationError(f"duplicate ticket number prevents migration: {duplicate[0]!r}")
        missing = sync_connection.exec_driver_sql(
            "SELECT id FROM tickets WHERE number IS NULL OR trim(number)='' LIMIT 1"
        ).first()
        if missing:
            raise MigrationError(f"ticket {missing[0]} has no required number")
        model_columns = {column.name for column in Base.metadata.tables["tickets"].columns}
        if columns != model_columns and not columns <= SAFE_LEGACY_TICKET_COLUMNS:
            unknown = sorted(columns - model_columns)
            absent = sorted(model_columns - columns)
            raise MigrationError(
                f"unsupported partial tickets schema (unknown={unknown}, missing={absent}); "
                "repair/export it explicitly before startup"
            )

    @staticmethod
    def _revision_1(sync_connection) -> None:
        """Create the model schema and explicitly upgrade the known pilot ticket shape."""
        inspector = inspect(sync_connection)
        existing = set(inspector.get_table_names())
        legacy_ticket_columns = ({c["name"] for c in inspector.get_columns("tickets")}
                                 if "tickets" in existing else set())
        Base.metadata.create_all(sync_connection)
        if legacy_ticket_columns and legacy_ticket_columns != {
            c.name for c in Base.metadata.tables["tickets"].columns
        }:
            preparer = sync_connection.dialect.identifier_preparer
            table = Base.metadata.tables["tickets"]
            now = datetime.now(timezone.utc).isoformat(sep=" ")
            defaults = {
                "created_at": repr(now), "updated_at": repr(now), "type": "'legacy'",
                "city": "'legacy'", "office": "'legacy'", "sender_name": "'legacy'",
                "description": "'Imported legacy ticket'", "creator_id": "'legacy'",
                "admin_id": "''", "admin_name": "''", "elapsed_seconds": "0",
                "file_url": "''", "reason": "''", "version": "1",
                "work_started_at": "NULL", "resolved_at": "NULL", "idle_seconds": "NULL",
            }
            for column in table.columns:
                if column.name in legacy_ticket_columns:
                    continue
                default = defaults.get(column.name)
                if default is None:
                    raise MigrationError(f"no explicit legacy rule for tickets.{column.name}")
                column_type = column.type.compile(dialect=sync_connection.dialect)
                nullable = "" if column.nullable else " NOT NULL"
                sync_connection.exec_driver_sql(
                    f"ALTER TABLE {preparer.quote('tickets')} ADD COLUMN "
                    f"{preparer.quote(column.name)} {column_type}{nullable} DEFAULT {default}"
                )

    @staticmethod
    def _revision_2(sync_connection) -> None:
        """Restore indexes and uniqueness omitted by SQLite create_all on partial tables."""
        preparer = sync_connection.dialect.identifier_preparer
        for table in Base.metadata.sorted_tables:
            for index in table.indexes:
                sync_connection.execute(CreateIndex(index, if_not_exists=True))
            for constraint in table.constraints:
                if not isinstance(constraint, UniqueConstraint):
                    continue
                columns = list(constraint.columns)
                if columns:
                    name = constraint.name or f"uq_{table.name}_{'_'.join(c.name for c in columns)}"
                    cols = ", ".join(preparer.quote(c.name) for c in columns)
                    sync_connection.exec_driver_sql(
                        f"CREATE UNIQUE INDEX IF NOT EXISTS {preparer.quote(name)} "
                        f"ON {preparer.quote(table.name)} ({cols})"
                    )

    @staticmethod
    def _revision_3(sync_connection) -> None:
        """Validate required migrated ticket values; no silent epoch/string backfills."""
        if "tickets" not in inspect(sync_connection).get_table_names():
            return
        required = ("number", "created_at", "type", "city", "office", "sender_name",
                    "description", "status", "creator_id", "updated_at", "version")
        predicate = " OR ".join(f'"{name}" IS NULL' for name in required)
        invalid = sync_connection.exec_driver_sql(f"SELECT id FROM tickets WHERE {predicate} LIMIT 1").first()
        if invalid:
            raise MigrationError(f"ticket {invalid[0]} has incompatible missing required data")

    @staticmethod
    def _revision_4(sync_connection) -> None:
        """Add durable per-entity bridge ordering introduced after revision 3."""
        Base.metadata.tables["bridge_sequences"].create(sync_connection, checkfirst=True)
        inspector = inspect(sync_connection)
        if "bridge_sequences" not in inspector.get_table_names():
            raise MigrationError("revision 4 failed to create bridge_sequences")
        columns = {column["name"] for column in inspector.get_columns("bridge_sequences")}
        if columns != {"entity_key", "version"}:
            raise MigrationError("revision 4 created an incompatible bridge_sequences table")

    @staticmethod
    def _validate_current_schema(sync_connection) -> None:
        """Fail closed if user_version claims current but the physical schema is not."""
        inspector = inspect(sync_connection)
        actual_tables = set(inspector.get_table_names())
        missing_tables = sorted(set(Base.metadata.tables) - actual_tables)
        if missing_tables:
            raise MigrationError(f"current database is missing tables: {', '.join(missing_tables)}")
        for name, table in Base.metadata.tables.items():
            actual_columns = {column["name"] for column in inspector.get_columns(name)}
            expected_columns = {column.name for column in table.columns}
            if actual_columns != expected_columns:
                raise MigrationError(
                    f"current database has incompatible {name} columns "
                    f"(extra={sorted(actual_columns - expected_columns)}, "
                    f"missing={sorted(expected_columns - actual_columns)})"
                )

    async def initialize(self) -> None:
        if self.settings.database_url.startswith("sqlite"):
            path = self.settings.sqlite_path
            legacy = False
            if path and path.exists() and path.stat().st_size:
                probe = sqlite3.connect(path)
                try:
                    version = int(probe.execute("PRAGMA user_version").fetchone()[0])
                    has_tables = bool(probe.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
                    ).fetchone())
                    legacy = has_tables and version < SCHEMA_REVISION
                finally:
                    probe.close()
            if legacy:
                self._safety_backup()
        async with self.engine.begin() as connection:
            if not self.settings.database_url.startswith("sqlite"):
                await connection.run_sync(Base.metadata.create_all)
                return
            version = int((await connection.execute(text("PRAGMA user_version"))).scalar_one())
            if version > SCHEMA_REVISION:
                raise MigrationError(f"database revision {version} is newer than supported {SCHEMA_REVISION}")
            if version < SCHEMA_REVISION:
                await connection.run_sync(self._preflight)
                revisions = (self._revision_1, self._revision_2, self._revision_3, self._revision_4)
                for revision in range(version + 1, SCHEMA_REVISION + 1):
                    await connection.run_sync(revisions[revision - 1])
                    await connection.execute(text(f"PRAGMA user_version={revision}"))
            await connection.run_sync(self._validate_current_schema)

    async def close(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self):
        async with self.sessions() as session:
            yield session

    async def integrity_check(self) -> str:
        async with self.engine.connect() as connection:
            return str((await connection.execute(text("PRAGMA integrity_check"))).scalar_one())
