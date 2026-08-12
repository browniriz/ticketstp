# Хранение, синхронизация и восстановление

## Вложения

Сервер принимает прежний `data:*;base64,...` в полях `image`/`filename`. Лимит — 20 МиБ. Исполняемые, HTML, SVG и скриптовые MIME/расширения запрещены. Имена проходят NFKC-нормализацию, удаление bidi/zero-width и разделителей пути. Файлы лежат отдельно от web-root в `TICKETSBOT_MEDIA_DIR` под случайными 256-битными именами.

`/media/{token}` требует подписанный Telegram `init_data` и проверяет автора/администратора; в production `PUBLIC_BASE_URL` обязан быть HTTPS. При замене старая запись отмечается `stale_at` и сразу перестаёт отдаваться. Физическое удаление рекомендуется после 7-дневного retention отдельной уборкой; резервная копия позволяет восстановить ошибочную замену.

## Workers и Sheets bridge

Включить `TICKETSBOT_WORKERS_ENABLED=true`. API только фиксирует outbox в одной транзакции и не ждёт Telegram/Sheets. Workers стартуют/останавливаются с FastAPI, сохраняют attempts, last_error, next_attempt_at и используют экспоненциальный backoff.

В Apps Script добавить Script Property `BRIDGE_SECRET` (минимум 32 случайных символа), тот же секрет указать серверу. Bridge маршрутизируется до Telegram auth, ограничивает batch 200, роли 5000 и размер ячейки. `bridgeUpsertTicket` идемпотентен по номеру. Локальный `Code.gs` изменён, но не развёрнут и production `API_URL` не менялся.

## Миграция, reconciliation, backup

Из каталога `server`:

```bash
.venv/Scripts/python scripts/maintenance.py import legacy.csv
.venv/Scripts/python scripts/maintenance.py reconcile          # dry-run
.venv/Scripts/python scripts/maintenance.py reconcile --apply  # batch upsert
.venv/Scripts/python scripts/maintenance.py backup backups/ticketsbot.db
```

CSV обязан содержать ровно 18 legacy-колонок. Повторный импорт сравнивает канонические 18 колонок: идентичная строка считается `unchanged`, не увеличивает `version` и не пишет в БД.

Reconciliation сначала вызывает server-side bridge action `bridgePullTickets`, затем сообщает totals, counts по статусам и списки `missing_in_sheet`, `extra_in_sheet`, `different` (включая поля и обе строки). Apps Script должен вернуть `{rows: [...]}` с каноническими 18-колоночными строками. `--apply` отправляет только отсутствующие/отличающиеся локальные строки; лишние строки в Sheets автоматически не удаляются.

### Coherent backup

Backup использует SQLite Online Backup API (включая committed WAL-страницы), проверяет `integrity_check` и обязательные таблицы, затем копирует media. Активная запись attachment обязана иметь файл совпадающего размера. Manifest содержит размер и SHA-256 каждого файла. Все артефакты сначала создаются в staging и публикуются атомарными rename; при ошибке частично опубликованный набор удаляется.

Встроенный maintenance lock не останавливает уже запущенный старый процесс сам по себе. Для согласованного с media snapshot API и workers должны быть quiesced на всё время backup. На Windows это делает `deploy/windows/backup.ps1`; ручной backup выполняйте только после остановки обоих процессов.

### Migrations

SQLite использует последовательные `PRAGMA user_version` revisions. Перед legacy upgrade автоматически создаётся `*.pre-migration-<UTC>.bak` и проверяется integrity. Preflight отклоняет пустые/дублирующиеся номера и неизвестные частичные схемы. Поддерживается только явно известный pilot fixture `id,number,status`; его обязательные поля заполняются явными legacy-значениями и текущим timestamp — epoch 1970 больше не используется. Ошибка оставляет revision неизменной и требует ручного export/repair.

## Windows deployment assets

Скрипты не запускаются автоматически:

```powershell
.\deploy\windows\prepare.ps1 -InstallRoot C:\Ticketsbot
.\deploy\windows\install-service.ps1 -InstallRoot C:\Ticketsbot
.\deploy\windows\install-tasks.ps1 -InstallRoot C:\Ticketsbot
```

`prepare.ps1` применяет pinned `uv.lock` через `uv sync --frozen`. NSSM service запускает только API на `127.0.0.1:8010` с absolute paths и пишет `logs/api*.log`. Отдельная startup task запускает `scripts/run_workers.py`; daily task вызывает quiescing `backup.ps1` и пишет в `backups/`/`logs/backup.log`. После review `.env` службы/задачи запускаются оператором вручную.
