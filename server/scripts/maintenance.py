from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from ticketsbot.config import Settings
from ticketsbot.db import Database
from ticketsbot.models import Ticket
from ticketsbot.workers import BridgeClient, ticket_row

LOCAL_TZ = timezone(timedelta(hours=5), "Asia/Yekaterinburg")
REQUIRED_TABLES = {
    "roles", "access_requests", "tickets", "ticket_events", "notification_outbox",
    "sheet_sync_outbox", "rate_limits", "sync_state", "bridge_sequences", "attachments",
}
TICKET_COLUMNS = (
    "number", "created_at", "type", "city", "office", "sender_name", "description",
    "status", "creator_id", "admin_id", "admin_name", "work_started_at",
    "elapsed_seconds", "resolved_at", "updated_at", "idle_seconds", "file_url", "reason",
)
TICKET_TEXT_COLUMNS = (2, 3, 4, 5, 6, 7, 8, 9, 10, 16, 17)
SHEETS_DANGEROUS_PREFIX = re.compile(r"^'[']*[\s\x00-\x1f]*[=+\-@]")


def canonical_sheet_ticket_row(row):
    """Remove one bridge-added Sheets formula escape, retaining user apostrophes."""
    result = list(row)
    for index in TICKET_TEXT_COLUMNS:
        value = str(result[index] if result[index] is not None else "")
        if SHEETS_DANGEROUS_PREFIX.match(value):
            value = value[1:]
        result[index] = value
    return result


@contextmanager
def maintenance_lock(path: Path):
    """Non-blocking cross-process maintenance lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except OSError as exc:
        raise RuntimeError(f"maintenance lock is held: {path}") from exc
    finally:
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def duration(value):
    text = str(value or "0").strip()
    parts = text.split(":")
    if len(parts) == 2:
        minutes, seconds = (int(p or 0) for p in parts)
        if minutes < 0 or not 0 <= seconds < 60:
            raise ValueError("invalid MM:SS duration")
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours, minutes, seconds = (int(p or 0) for p in parts)
        if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
            raise ValueError("invalid HH:MM:SS duration")
        return hours * 3600 + minutes * 60 + seconds
    result = int(float(text))
    if result < 0:
        raise ValueError("negative duration")
    return result


def date(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return datetime.strptime(text, "%d.%m.%Y %H:%M:%S").replace(
            tzinfo=LOCAL_TZ
        ).astimezone(timezone.utc)


def from_row(r):
    if len(r) != 18:
        raise ValueError("expected exactly 18 columns")
    number = str(r[0]).strip()
    if not re.fullmatch(r"[A-Z][0-9]{3}", number):
        raise ValueError("invalid ticket number")
    now = datetime.now(timezone.utc)
    return Ticket(
        number=number, created_at=date(r[1]) or now, type=r[2], city=r[3], office=r[4],
        sender_name=r[5], description=r[6], status=r[7], creator_id=str(r[8]),
        admin_id=str(r[9] or ""), admin_name=r[10], work_started_at=date(r[11]),
        elapsed_seconds=duration(r[12]), resolved_at=date(r[13]), updated_at=date(r[14]) or now,
        idle_seconds=duration(r[15]) if r[15] else None, file_url=r[16], reason=r[17],
    )


def _ticket_values(ticket):
    return tuple(getattr(ticket, name) for name in TICKET_COLUMNS)


async def import_csv(settings, source):
    db = Database(settings)
    await db.initialize()
    inserted = updated = unchanged = 0
    try:
        with Path(source).open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        if rows and rows[0] and rows[0][0] in ("номер", "number"):
            rows = rows[1:]
        parsed = [from_row(row) for row in rows]
        numbers = [ticket.number for ticket in parsed]
        if len(numbers) != len(set(numbers)):
            raise ValueError("duplicate ticket number in import")
        async with db.session() as session:
            for incoming in parsed:
                current = (await session.execute(
                    select(Ticket).where(Ticket.number == incoming.number)
                )).scalar_one_or_none()
                if current:
                    # Compare the canonical external representation. SQLite may
                    # return naive datetimes even though the logical UTC values
                    # are equal, which must not turn a no-op into an update.
                    if ticket_row(current) == ticket_row(incoming):
                        unchanged += 1
                    else:
                        for column in TICKET_COLUMNS:
                            setattr(current, column, getattr(incoming, column))
                        current.version += 1
                        updated += 1
                else:
                    session.add(incoming)
                    inserted += 1
            await session.commit()
        return {"inserted": inserted, "updated": updated, "unchanged": unchanged,
                "total": len(parsed)}
    finally:
        await db.close()


def _status_counts(rows):
    result = {}
    for row in rows:
        status = str(row[7] or "")
        result[status] = result.get(status, 0) + 1
    return dict(sorted(result.items()))


async def reconcile(settings, apply=False, bridge=None):
    if bridge is None and (not settings.sheet_bridge_url or not settings.sheet_bridge_secret):
        raise RuntimeError("sheet bridge URL and secret are required")
    db = Database(settings)
    await db.initialize()
    bridge = bridge or BridgeClient(settings.sheet_bridge_url, settings.sheet_bridge_secret)
    try:
        async with db.session() as session:
            tickets = list((await session.execute(select(Ticket).order_by(Ticket.number))).scalars())
        local_rows = [ticket_row(ticket) for ticket in tickets]
        snapshot = await bridge.call("bridgePullTickets")
        sheet_rows = snapshot.get("rows") if isinstance(snapshot, dict) else None
        if not isinstance(sheet_rows, list):
            raise RuntimeError("bridgePullTickets returned no rows array")
        normalized_sheet = [ticket_row(from_row(canonical_sheet_ticket_row(row))) for row in sheet_rows]
        local = {row[0]: row for row in local_rows}
        sheet = {row[0]: row for row in normalized_sheet}
        if len(sheet) != len(normalized_sheet):
            raise ValueError("duplicate ticket number in sheet snapshot")
        missing = sorted(set(local) - set(sheet))
        extra = sorted(set(sheet) - set(local))
        different = []
        for number in sorted(set(local) & set(sheet)):
            fields = [TICKET_COLUMNS[i] for i, pair in enumerate(zip(local[number], sheet[number]))
                      if pair[0] != pair[1]]
            if fields:
                different.append({"number": number, "fields": fields,
                                  "local": local[number], "sheet": sheet[number]})
        result = {
            "summary": {"local_total": len(local_rows), "sheet_total": len(normalized_sheet),
                        "missing_in_sheet": len(missing), "extra_in_sheet": len(extra),
                        "different": len(different),
                        "matching": len(local_rows) - len(missing) - len(different)},
            "status_counts": {"local": _status_counts(local_rows),
                              "sheet": _status_counts(normalized_sheet)},
            "details": {"missing_in_sheet": missing, "extra_in_sheet": extra,
                        "different": different},
            "applied": False,
        }
        if apply:
            changed = [local[n] for n in missing] + [local[item["number"]] for item in different]
            applied = 0
            for start in range(0, len(changed), 200):
                batch = changed[start:start + 200]
                dedupe_keys = [
                    "reconcile:" + str(row[0]) + ":" + hashlib.sha256(
                        json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    ).hexdigest()
                    for row in batch
                ]
                response = await bridge.call("bridgeBatchUpsertTickets", rows=batch,
                                             dedupe_keys=dedupe_keys)
                acknowledgments = response.get("acknowledgments") if isinstance(response, dict) else None
                if not isinstance(acknowledgments, list) or len(acknowledgments) != len(batch):
                    raise RuntimeError("bridge acknowledged incomplete batch")
                expected = {(str(row[0]), key) for row, key in zip(batch, dedupe_keys)}
                actual = set()
                for ack in acknowledgments:
                    if not isinstance(ack, dict):
                        raise RuntimeError("bridge returned invalid batch acknowledgment")
                    actual.add((str(ack.get("number", "")), str(ack.get("dedupe_key", ""))))
                if len(actual) != len(acknowledgments) or actual != expected:
                    raise RuntimeError("bridge returned mismatched batch acknowledgment")
                count = response.get("upserted")
                if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= len(batch):
                    raise RuntimeError("bridge returned invalid upsert count")
                applied += len(acknowledgments)
            result.update(applied=True, upserted=applied)
        return result
    finally:
        await db.close()


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup(settings, destination):
    src = settings.sqlite_path
    if not src or not src.exists():
        raise RuntimeError("backup supports an existing SQLite database")
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    media_dest = dest.with_suffix(dest.suffix + ".media")
    manifest_dest = dest.with_suffix(dest.suffix + ".json")
    if any(path.exists() for path in (dest, media_dest, manifest_dest)):
        raise FileExistsError("backup destination already exists")

    stage = Path(tempfile.mkdtemp(prefix=f".{dest.name}.", dir=dest.parent))
    staged_db, staged_media = stage / "database.sqlite", stage / "media"
    published = []
    try:
        with maintenance_lock(src.with_suffix(src.suffix + ".maintenance.lock")):
            source = sqlite3.connect(f"file:{src.resolve().as_posix()}?mode=ro", uri=True)
            target = sqlite3.connect(staged_db)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            staged_media.mkdir()
            if settings.media_dir.exists():
                shutil.copytree(settings.media_dir, staged_media, dirs_exist_ok=True)

        verify = sqlite3.connect(f"file:{staged_db.resolve().as_posix()}?mode=ro", uri=True)
        try:
            integrity = verify.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {row[0] for row in verify.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            missing_tables = sorted(REQUIRED_TABLES - tables)
            if integrity != "ok":
                raise RuntimeError(f"backup integrity check failed: {integrity}")
            if missing_tables:
                raise RuntimeError(f"backup missing required tables: {', '.join(missing_tables)}")
            counts = {name: verify.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                      for name in sorted(REQUIRED_TABLES)}
            attachment_rows = []
            columns = {row[1] for row in verify.execute("PRAGMA table_info(attachments)")}
            if {"stored_name", "size", "stale_at"} <= columns:
                attachment_rows = verify.execute(
                    "SELECT stored_name,size FROM attachments WHERE stale_at IS NULL"
                ).fetchall()
        finally:
            verify.close()

        files = []
        for path in sorted(p for p in staged_media.rglob("*") if p.is_file()):
            files.append({"path": path.relative_to(staged_media).as_posix(),
                          "size": path.stat().st_size, "sha256": _sha256(path)})
        file_by_name = {item["path"]: item for item in files}
        errors = []
        for stored_name, expected_size in attachment_rows:
            item = file_by_name.get(stored_name)
            if item is None:
                errors.append({"stored_name": stored_name, "error": "missing"})
            elif item["size"] != expected_size:
                errors.append({"stored_name": stored_name, "error": "size_mismatch",
                               "database_size": expected_size, "file_size": item["size"]})
        if errors:
            raise RuntimeError(f"active attachment validation failed: {errors}")
        manifest = {
            "database": str(dest), "media": str(media_dest),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "integrity_check": integrity, "tables": counts, "media_files": len(files),
            "files": files, "active_attachments": len(attachment_rows),
            "strategy": "service-quiesced maintenance lock + SQLite online snapshot + media copy",
        }
        staged_manifest = stage / "manifest.json"
        staged_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        # Every artifact is staged and verified first. Each publish operation is atomic;
        # rollback removes earlier artifacts if a later rename fails.
        for source_path, final_path in (
            (staged_db, dest), (staged_media, media_dest), (staged_manifest, manifest_dest)
        ):
            os.replace(source_path, final_path)
            published.append(final_path)
        return manifest
    except Exception:
        for path in reversed(published):
            shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    importer = sub.add_parser("import")
    importer.add_argument("csv")
    reconciler = sub.add_parser("reconcile")
    reconciler.add_argument("--apply", action="store_true")
    backup_parser = sub.add_parser("backup")
    backup_parser.add_argument("destination")
    args = parser.parse_args()
    settings = Settings()
    result = backup(settings, args.destination) if args.command == "backup" else asyncio.run(
        import_csv(settings, args.csv) if args.command == "import" else reconcile(settings, args.apply)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
