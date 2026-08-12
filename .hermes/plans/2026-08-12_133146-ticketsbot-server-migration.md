# Ticketsbot Server Migration Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Перенести рабочие операции Ticketsbot с Google Apps Script/Sheets на отдельный FastAPI + SQLite сервис на сервере Avito, сохранив Google-листы `роли`, `заявки`, `запросы` и текущий `Отчет`.

**Architecture:** Mini App вызывает совместимый FastAPI endpoint. SQLite является источником истины по заявкам и запросам доступа. Google `роли` остаётся управленческим источником и зеркалируется в локальную БД фоновым защищённым обменом с Apps Script; Google `заявки` является отчётным зеркалом серверных заявок. Telegram-уведомления и синхронизация выполняются после фиксации основной транзакции с повторными попытками.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy async, SQLite WAL, httpx, pydantic-settings, pytest, существующий HTML Mini App, Google Apps Script bridge.

---

## Ограничения и безопасность

- Не изменять и не выключать текущий Apps Script deployment до завершения пилота.
- Новый сервис использует отдельные порт, процесс, `.env`, БД, логи и backup.
- Не смешивать таблицы с `avito_bot.db`.
- Не переключать Telegram Menu Button/API_URL до живой проверки тестового URL.
- Существующие неотслеживаемые файлы репозитория не менять.
- Google OAuth сейчас отозван; облачный deployment является отдельным gate после локальной реализации.

## Task 1: Scaffold backend and configuration

**Files:**
- Create: `server/pyproject.toml`
- Create: `server/src/ticketsbot/config.py`
- Create: `server/src/ticketsbot/app.py`
- Create: `server/src/ticketsbot/runtime.py`
- Test: `server/tests/test_health.py`

**Acceptance:** отдельный `/health`, legacy GET pong и POST `{action:"ping"}`; запуск на отдельном порту 8010; секреты только из `.env`.

## Task 2: Database schema and migrations

**Files:**
- Create: `server/src/ticketsbot/db.py`
- Create: `server/src/ticketsbot/models.py`
- Test: `server/tests/test_database.py`

**Tables:** roles, access_requests, tickets, ticket_events, notification_outbox, sheet_sync_outbox, sync_state.

**Acceptance:** WAL, foreign keys, busy timeout, unique ticket number, transaction-safe state transitions, integrity check.

## Task 3: Telegram initData authentication and legacy envelope

**Files:**
- Create: `server/src/ticketsbot/auth.py`
- Create: `server/src/ticketsbot/legacy_api.py`
- Test: `server/tests/test_auth.py`

**Acceptance:** server validates HMAC, never trusts client `tg_id`, preserves `{success,data,error}` contract and accepts JSON POST without requiring content-type.

## Task 4: Roles, access and ticket operations

**Files:**
- Create: `server/src/ticketsbot/services/*.py`
- Test: `server/tests/test_roles.py`
- Test: `server/tests/test_ticket_workflow.py`
- Test: `server/tests/test_access.py`

**Acceptance:** all existing actions and response fields supported; exact status machine, timers, type matrix, pagination, leaderboard, admin checks and concurrency behavior.

## Task 5: Attachments and Telegram outbox

**Files:**
- Create: `server/src/ticketsbot/files.py`
- Create: `server/src/ticketsbot/workers.py`
- Test: `server/tests/test_files.py`
- Test: `server/tests/test_outbox.py`

**Acceptance:** 20 MiB bound, blocked MIME/extensions, safe filenames, HTTPS file URLs, operation commits before notification delivery, retries do not duplicate durable intent.

## Task 6: Google Sheets bridge

**Files:**
- Modify: `Code.gs`
- Create: `server/src/ticketsbot/sheets_bridge.py`
- Test: `server/tests/test_sheets_bridge.py`

**Protocol:** shared high-entropy secret in Script Properties/server env; pull complete role snapshot with revision/hash; idempotent upsert of ticket rows by unique number; access request mirror; batch reconciliation endpoint.

**Acceptance:** `роли` remains authoritative; `заявки` keeps exact 18 columns/date/duration formats; `Отчет` code remains unchanged; sync failure never fails a ticket action; pending jobs retry.

## Task 7: Migration and reconciliation tools

**Files:**
- Create: `server/scripts/import_sheet_export.py`
- Create: `server/scripts/reconcile_sheet.py`
- Create: `server/scripts/backup_database.py`
- Test: `server/tests/test_migration.py`

**Acceptance:** idempotent import preserving IDs, timestamps, statuses, durations and file URLs; comparison report by totals/statuses; SQLite online backup with integrity check.

## Task 8: Frontend compatibility and deployment assets

**Files:**
- Modify: `index.html` only as needed for configurable API URL/request IDs.
- Create: `server/scripts/windows/run_service.cmd`
- Create: `server/scripts/windows/install_tasks.ps1`
- Create: `server/.env.example`
- Update: `README.md`

**Acceptance:** old interface works unchanged except endpoint; no production URL switch; service uses `127.0.0.1:8010`; separate task names/logs/backups.

## Task 9: Verification gates

1. Full pytest/static checks.
2. Real local TCP HTTP scenario: role → create → take → pause/resume → finish → history.
3. Concurrent take test: exactly one winner.
4. Sync bridge contract test and report parity fixtures.
5. Destination preflight without starting production polling: Python, disk, port 8010, DB integrity.
6. Deploy disabled test service and public test route.
7. Signed Telegram Mini App test with separate URL.
8. Live Google mirror check: new row and status changes appear; existing `Report` rebuilds with matching aggregates.
9. Only then update production `API_URL`/Menu Button, keep Apps Script as rollback.

## Rollback

- Switch frontend API URL/Menu Button back to current Apps Script deployment.
- Stop/disable only Ticketsbot server tasks.
- Keep server DB for reconciliation; never overwrite Google history during rollback.
