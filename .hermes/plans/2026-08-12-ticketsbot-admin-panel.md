# Ticketsbot Admin Panel Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Создать отдельную защищённую браузерную панель для просмотра, поиска, аудита, архивирования и согласованного удаления заявок из серверной БД и Google-зеркала.

**Architecture:** Панель обслуживается тем же FastAPI на `/admin`, но использует отдельную cookie-сессию с HttpOnly/Secure/SameSite=Strict и отдельный случайный пароль, хранящийся только в `.env`. Административные операции выполняются транзакционно, фиксируются в append-only audit, а окончательное удаление допускается только для закрытой заявки после точного ввода номера. Google-строка удаляется через идемпотентную операцию durable outbox; до подтверждения удаления хранится tombstone, предотвращающий восстановление строки reconciliation/import.

**Tech Stack:** FastAPI, SQLAlchemy async, SQLite WAL, vanilla HTML/CSS/JS, pytest, Apps Script bridge.

---

### Task 1: Session authentication and audit schema

**Files:**
- Modify: `server/src/ticketsbot/config.py`
- Modify: `server/src/ticketsbot/models.py`
- Modify: `server/src/ticketsbot/db.py`
- Test: `server/tests/test_admin_panel.py`

**Steps:** Add password hash/session signing settings, schema revision with admin sessions, audit records and tombstones; test expiry, invalid cookie and migration.

### Task 2: Read-only admin API

**Files:**
- Create: `server/src/ticketsbot/admin.py`
- Modify: `server/src/ticketsbot/app.py`
- Test: `server/tests/test_admin_panel.py`

**Steps:** Add login/logout, paginated list with search/status/type/city filters, summary counts, ticket detail, events, attachments and outbox state. Verify unauthenticated requests fail.

### Task 3: Safe archive/delete service

**Files:**
- Create: `server/src/ticketsbot/services/admin.py`
- Modify: `server/src/ticketsbot/models.py`
- Test: `server/tests/test_admin_panel.py`

**Steps:** Allow archive without data loss; permanent delete only closed tickets with exact number confirmation. In one transaction create tombstone/audit, detach attachment, remove ticket/events/outboxes and enqueue Google delete. Physical media is quarantined until Google acknowledgement and backup retention.

### Task 4: Google bridge delete

**Files:**
- Modify: `Code.gs`
- Modify: `server/src/ticketsbot/workers.py`
- Test: `server/tests/test_admin_panel.py`
- Test: `server/tests/test_bridge_maintenance.py`

**Steps:** Add authenticated idempotent `bridgeDeleteTicket`, sequence/dedupe protection, and worker handling. Verify duplicate delete succeeds and unknown number is safe.

### Task 5: Responsive admin UI

**Files:**
- Create: `server/src/ticketsbot/static/admin.html`
- Modify: `server/src/ticketsbot/admin.py`

**Steps:** Build desktop/mobile dashboard with login, KPI cards, filters, list, detail drawer, timeline, sync indicators, archive and danger-zone confirmation. No secrets in localStorage.

### Task 6: Verification and deployment

**Files:**
- Modify: `server/OPERATIONS.md`
- Modify: `server/.env.example`

**Steps:** Run full tests, local real TCP browser flow, backup, deploy to isolated server, configure generated password hash/session secret, verify external HTTPS, login/list/detail/logout and workers. Take pre-delete backup, delete J265 through UI, verify absent in DB and Google, media quarantined, audit present, and reconciliation exact.
