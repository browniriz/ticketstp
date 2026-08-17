/**
 * Ticketsbot — backend (Google Apps Script web app)
 * =================================================
 * Single POST endpoint. The Mini App (index.html) sends JSON { action, ... }
 * and gets back JSON { success, data, error }.
 *
 * Deploy:
 *   1. Создай Google Таблицу, открой Расширения → Apps Script.
 *   2. Вставь этот файл целиком (имя файла Code.gs).
 *   3. Заполни блок CONFIG ниже.
 *   4. Запусти один раз функцию setup() (создаст листы и добавит тебя админом).
 *   5. Deploy → New deployment → Web app:
 *        Execute as: Me   |   Who has access: Anyone
 *      Скопируй URL и вставь его в index.html в константу API_URL.
 *   6. После ЛЮБЫХ изменений Code.gs делай Deploy → New deployment (новый!),
 *      иначе изменения не публикуются.
 */

// ============================ CONFIG ============================
//
// СЕКРЕТЫ И КОНФИГ ХРАНЯТСЯ В SCRIPT PROPERTIES, НЕ В КОДЕ.
// Заполнить один раз одним из способов:
//   A) Apps Script → ⚙ Project Settings → Script properties → добавить ключи; ИЛИ
//   B) временно вписать значения в setupSecrets() ниже, запустить его один раз,
//      затем СТЕРЕТЬ значения обратно (чтобы секреты не оставались в коде).
//
// Ключи:
//   BOT_TOKEN           — токен бота от BotFather (СЕКРЕТ; при утечке — /revoke и заменить).
//   SPREADSHEET_ID      — ID Google-таблицы (пусто = активная таблица контейнерного скрипта).
//   NOTIFY_CHAT_ID      — чат уведомлений (группа: -100...). Пусто = не слать.
//   NOTIFY_THREAD_ID    — тема форума (только супергруппа-форум). Пусто = общий поток.
//   BOOTSTRAP_ADMIN_IDS — tg_id бутстрап-админов через запятую (для setup()).
//   BOOTSTRAP_ADMIN_NAME— имя бутстрап-админа (необязательно).
//   INIT_DATA_MAX_AGE_SECONDS — максимальный возраст Telegram initData (по умолчанию 86400 = 24ч).
//   INIT_DATA_FUTURE_SKEW_SECONDS — допустимое опережение часов Telegram (по умолчанию 60с).

function scriptProp_(key) {
  return PropertiesService.getScriptProperties().getProperty(key) || '';
}

var BOT_TOKEN = scriptProp_('BOT_TOKEN');
var SPREADSHEET_ID = scriptProp_('SPREADSHEET_ID');
var NOTIFY_CHAT_ID = scriptProp_('NOTIFY_CHAT_ID');
var NOTIFY_THREAD_ID = scriptProp_('NOTIFY_THREAD_ID');
var BOOTSTRAP_ADMIN_IDS = scriptProp_('BOOTSTRAP_ADMIN_IDS');
var BOOTSTRAP_ADMIN_NAME = scriptProp_('BOOTSTRAP_ADMIN_NAME') || 'Администратор';
var BRIDGE_SECRET = scriptProp_('BRIDGE_SECRET');
var INIT_DATA_MAX_AGE_SECONDS = Number(scriptProp_('INIT_DATA_MAX_AGE_SECONDS') || 86400);
var INIT_DATA_FUTURE_SKEW_SECONDS = Number(scriptProp_('INIT_DATA_FUTURE_SKEW_SECONDS') || 60);

// Разовая настройка секретов. ВПИШИ значения, запусти один раз, затем ОЧИСТИ обратно
// до пустых строк (Script properties уже сохранены и читаются из хранилища).
// Пустые строки не перезаписывают уже существующие свойства.
function setupSecrets() {
  var values = {
    BOT_TOKEN: '',            // токен бота от BotFather
    SPREADSHEET_ID: '',       // ID таблицы
    NOTIFY_CHAT_ID: '',       // напр. -1002009555068
    NOTIFY_THREAD_ID: '',     // напр. 43695 (или пусто)
    BOOTSTRAP_ADMIN_IDS: '',  // напр. 339860192
    BOOTSTRAP_ADMIN_NAME: '', // напр. Администратор
    BRIDGE_SECRET: '',        // случайная строка минимум 32 символа; только server↔Sheets
    INIT_DATA_MAX_AGE_SECONDS: '',    // напр. 86400 (24 часа); пусто = значение по умолчанию
    INIT_DATA_FUTURE_SKEW_SECONDS: '' // напр. 60; пусто = значение по умолчанию
  };
  var props = PropertiesService.getScriptProperties();
  var written = [];
  Object.keys(values).forEach(function (k) {
    if (values[k] !== '') { props.setProperty(k, String(values[k])); written.push(k); }
  });
  return written.length ? ('OK: записаны ключи — ' + written.join(', ') + '. Теперь очисти значения в setupSecrets().')
                        : 'Ничего не записано: впиши значения в setupSecrets().';
}

// ============================ SHEETS ============================

var SHEET_ROLES = 'роли';
var SHEET_TICKETS = 'заявки';
var SHEET_REQUESTS = 'запросы';

var TICKET_TYPES = ['Ломбард', 'Скупка', 'Касса', 'Ошибка', 'Перемещение', 'Оприходование', 'Изъятие', 'Списание', 'Возвраты клиентам'];
var ADMIN_ONLY_TICKET_TYPES = ['Списание'];
var DEFAULT_EMPLOYEE_TICKET_TYPES = ['Ломбард', 'Скупка', 'Касса', 'Ошибка', 'Изъятие'];
var ROLE_BASE_COLUMNS = 5;
var ROLE_TYPE_START_COLUMN = ROLE_BASE_COLUMNS + 1;
var ROLES_HEADERS = ['tg_id', 'имя', 'роль', 'username', 'photo_url'].concat(TICKET_TYPES); // роль: сотрудник | админ
var REQUESTS_HEADERS = ['tg_id', 'имя', 'дата_запроса', 'username', 'photo_url']; // ожидающие одобрения доступа

// Используется только при первой миграции старой таблицы: эти сотрудники раньше
// получали «Перемещение» и «Оприходование» из списка в index.html.
var LEGACY_RESTRICTED_TYPE_IDS = [
  '2116352369', '6385304772', '1398694890', '975893746', '1369791792',
  '512953513', '1764776025', '2108473175', '1295198922', '1231866253'
];

var TICKETS_HEADERS = [
  'номер',              // 0  A047
  'дата_создания',      // 1  ISO
  'тип',                // 2  Ломбард | Скупка | Касса | Ошибка
  'город',              // 3
  'офис',               // 4
  'имя_отправителя',    // 5
  'описание',           // 6
  'статус',             // 7  создана | в работе | на паузе | решена | на доработке | исправлена | отклонена
  'tg_id_создателя',    // 8
  'админ_tg_id',        // 9  кто взял в работу
  'админ_имя',          // 10
  'work_started_at',    // 11 ISO или '' — момент запуска таймера
  'Затраченное время',  // 12 суммарное время в работе, формат ММ:СС
  'дата_решения',       // 13 ISO
  'последнее_изменение',// 14 ISO
  'Время не в работе',  // 15 от создания до 1-го взятия в работу, ММ:СС
  'Файл',               // 16 ссылка на вложение в Google Drive (любой тип)
  'основание'           // 17 текст основания доработки/отклонения
];

var STATUS = {
  NEW: 'создана',
  WORK: 'в работе',
  PAUSE: 'на паузе',
  DONE: 'решена',
  REVISION: 'на доработке',  // админ вернул сотруднику с основанием; таймер стоит, не обнулён
  FIXED: 'исправлена',       // сотрудник поправил и вернул в работу; ждёт повторного взятия
  REJECTED: 'отклонена'      // админ отклонил с основанием; терминальный статус
};

// ============================ CACHE =============================
// Опросы (getTickets/getMyTickets/getHistory/getRole) читаются из кэша Google
// (~10-50 мс) вместо медленного обращения к таблице (~0.5-3 с).
// Любая запись сбрасывает кэш, поэтому данные всегда актуальны; TTL — лишь
// потолок на случай, если изменений не было. Таймер пересчитывается на лету.
// Активные и завершённые заявки кэшируются РАЗДЕЛЬНО: getTickets/getHistory
// читают только свою половину, а не всю историю заявок — иначе по мере роста
// листа общий кэш упирается в лимит CacheService (100 КБ на значение) и
// опрос (каждые 4с) начинает бить по таблице напрямую на КАЖДЫЙ запрос.
// Все записи через приложение явно сбрасывают кэш. Длинный TTL нужен, чтобы
// большая таблица не перечитывалась каждые 20 секунд при обычном просмотре.
var CACHE_TTL_SECONDS = 300;
// Лимит CacheService считается в байтах; 20 тыс. UTF-16 символов безопасны
// даже для четырёхбайтового Unicode и остаются ниже 100 КБ на ключ.
var CACHE_CHUNK_SIZE = 20000;
var CACHE_KEY_TICKETS_ACTIVE = 'ticket_rows_active_v2';
var CACHE_KEY_TICKETS_DONE = 'ticket_rows_done_v2';
var CACHE_KEY_ROLES = 'role_rows_v2';
// Лидерборд хранит уже посчитанные агрегаты (не сырые строки), поэтому не
// упирается в лимит CacheService даже при большой истории заявок.
var CACHE_KEY_LEADERBOARD_ALL = 'leaderboard_all_v1';
var CACHE_KEY_LEADERBOARD_MONTH = 'leaderboard_month_v1';

// ============================ ROUTING ============================

function doGet(e) {
  return json_({ success: true, data: { pong: true } });
}

function doPost(e) {
  try {
    var body = (e && e.postData && e.postData.contents) ? JSON.parse(e.postData.contents) : {};
    var action = body.action;
    var data;

    // Server-to-Sheets bridge is routed before Telegram auth because it uses a
    // separate high-entropy Script Property secret. Never send BRIDGE_SECRET to browsers.
    if (String(action || '').indexOf('bridge') === 0) {
      if (!bridgeSecretValid_(body.bridge_secret)) return json_({ success: false, error: 'unauthorized' });
      bridgeRateLimit_();
      data = bridgeRoute_(action, body);
      return json_({ success: true, data: data });
    }

    // Личность подтверждается подписью Telegram initData, а НЕ полем tg_id от клиента.
    // Любой запрос (кроме ping) обязан принести валидный init_data; доверенный id
    // перезаписывает body.tg_id, поэтому подделать чужой tg_id нельзя.
    if (action !== 'ping') {
      var verified = verifyInitData_(body.init_data);
      body.tg_id = String(verified.id);
      body.tg_username = verified.username || '';
      body.tg_photo = verified.photoUrl || '';
    }

    switch (action) {
      case 'ping':          data = { pong: true }; break;
      case 'getRole':       data = getRole_(body); break;
      case 'createTicket':  data = createTicket_(body); break;
      case 'getMyTickets':  data = getMyTickets_(body); break;
      case 'getTickets':    data = getTickets_(body); break;
      case 'getHistory':    data = getHistory_(body); break;
      case 'takeTicket':    data = takeTicket_(body); break;
      case 'pauseTicket':   data = pauseTicket_(body); break;
      case 'resumeTicket':  data = resumeTicket_(body); break;
      case 'finishTicket':  data = finishTicket_(body); break;
      case 'returnTicket':  data = returnTicket_(body); break;
      case 'rejectTicket':  data = rejectTicket_(body); break;
      case 'resubmitTicket': data = resubmitTicket_(body); break;
      case 'transferTicket': data = transferTicket_(body); break;
      case 'addScreenshot': data = addScreenshot_(body); break;
      case 'getAdmins':     data = getAdmins_(body); break;
      case 'getLeaderboard': data = getLeaderboard_(body); break;
      case 'requestAccess': data = requestAccess_(body); break;
      case 'getAccess':     data = getAccess_(body); break;
      case 'approveAccess': data = approveAccess_(body); break;
      case 'rejectAccess':  data = rejectAccess_(body); break;
      case 'renameRole':    data = renameRole_(body); break;
      case 'refreshContacts': data = refreshContacts_(body); break;
      case 'revokeAccess':  data = revokeAccess_(body); break;
      default:
        return json_({ success: false, error: 'Неизвестное действие: ' + action });
    }
    return json_({ success: true, data: data });
  } catch (err) {
    // Только намеренные (userFacing) ошибки показываем клиенту дословно;
    // системные логируем и отдаём generic, чтобы не утекали детали реализации.
    if (err && err.userFacing) {
      return json_({ success: false, error: String(err.message) });
    }
    Logger.log('Внутренняя ошибка: ' + (err && err.stack ? err.stack : err));
    return json_({ success: false, error: 'Внутренняя ошибка сервера. Попробуйте позже.' });
  }
}

function json_(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// Намеренная пользовательская ошибка — её текст безопасно показывать клиенту.
// Непомеченные (системные) ошибки в doPost заменяются на generic-сообщение.
function userError_(msg) {
  var e = new Error(msg);
  e.userFacing = true;
  return e;
}

// Простой rate-limit на основе CacheService: не более maxPerHour действий в час
// на пользователя (фиксированное часовое окно по ключу). Защита от спама/DoS.
function rateLimit_(tgId, bucket, maxPerHour) {
  if (!tgId) return;
  var hour = Math.floor(Date.now() / 3600000);
  var key = 'rl_' + bucket + '_' + tgId + '_' + hour;
  var cache = CacheService.getScriptCache();
  var n = Number(cache.get(key) || 0);
  if (n >= maxPerHour) throw userError_('Слишком много запросов, попробуйте позже.');
  cache.put(key, String(n + 1), 3700);
}

// ============================ AUTH (Telegram initData) ============================
// Проверяет подпись initData из Telegram WebApp и возвращает доверенный профиль.
// Алгоритм: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
//   secret_key   = HMAC_SHA256(key='WebAppData', msg=BOT_TOKEN)
//   check_hash   = HMAC_SHA256(key=secret_key, msg=data_check_string)  → hex
// data_check_string — все поля кроме hash, отсортированы по ключу, склеены '\n' как key=value.
// Подписанный auth_date также ограничен настраиваемым окном свежести (по умолчанию 24ч).
function verifyInitData_(initData) {
  if (!BOT_TOKEN) throw userError_('Сервер не настроен: отсутствует BOT_TOKEN.');
  if (!initData) throw userError_('Откройте бота в официальном приложении Telegram.');

  var pairs = String(initData).split('&');
  var data = {}, hash = '';
  for (var i = 0; i < pairs.length; i++) {
    var idx = pairs[i].indexOf('=');
    if (idx === -1) continue;
    var key = decodeURIComponent(pairs[i].slice(0, idx));
    var val = decodeURIComponent(pairs[i].slice(idx + 1));
    if (key === 'hash') hash = val; else data[key] = val;
  }
  if (!hash) throw userError_('Некорректные данные авторизации.');

  var keys = Object.keys(data).sort();
  var dcs = keys.map(function (k) { return k + '=' + data[k]; }).join('\n');

  var secret = Utilities.computeHmacSha256Signature(BOT_TOKEN, 'WebAppData');
  var computed = Utilities.computeHmacSha256Signature(Utilities.newBlob(dcs).getBytes(), secret);
  if (bytesToHex_(computed) !== String(hash).toLowerCase()) {
    throw userError_('Проверка подписи Telegram не пройдена.');
  }

  var authDate = Number(data.auth_date);
  var maxAge = INIT_DATA_MAX_AGE_SECONDS;
  var futureSkew = INIT_DATA_FUTURE_SKEW_SECONDS;
  if (!Number.isFinite(maxAge) || maxAge <= 0 || !Number.isFinite(futureSkew) || futureSkew < 0) {
    throw userError_('Сервер неверно настроен: окно свежести Telegram.');
  }
  if (!Number.isInteger(authDate) || authDate <= 0) throw userError_('Некорректная дата авторизации Telegram.');
  var nowSeconds = Math.floor(Date.now() / 1000);
  if (authDate < nowSeconds - maxAge) throw userError_('Данные авторизации Telegram устарели. Откройте приложение заново.');
  if (authDate > nowSeconds + futureSkew) throw userError_('Дата авторизации Telegram находится в будущем.');

  var user = {};
  try { user = JSON.parse(data.user || '{}'); } catch (e) {}
  if (!user || !user.id) throw userError_('В данных авторизации нет пользователя.');
  return {
    id: user.id,
    name: [user.first_name, user.last_name].filter(Boolean).join(' '),
    username: user.username || '',
    photoUrl: user.photo_url || '',
    raw: data
  };
}

function bytesToHex_(bytes) {
  var out = '';
  for (var i = 0; i < bytes.length; i++) {
    var b = bytes[i] < 0 ? bytes[i] + 256 : bytes[i];
    out += (b < 16 ? '0' : '') + b.toString(16);
  }
  return out;
}

// Constant-time-ish comparison: always scans the maximum length.
function bridgeSecretValid_(candidate) {
  var a = String(BRIDGE_SECRET || ''), b = String(candidate || '');
  if (!a || a.length < 32) return false;
  var diff = a.length ^ b.length, n = Math.max(a.length, b.length);
  for (var i = 0; i < n; i++) diff |= (a.charCodeAt(i % a.length) ^ b.charCodeAt(i % Math.max(1, b.length)));
  return diff === 0;
}

function bridgeRateLimit_() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) throw new Error('bridge busy');
  try {
    var key = 'bridge_rl_' + Math.floor(Date.now() / 60000);
    var props = PropertiesService.getScriptProperties();
    var count = Number(props.getProperty(key) || 0);
    if (count >= 300) throw new Error('bridge rate limit');
    props.setProperty(key, String(count + 1));
    props.deleteProperty('bridge_rl_' + (Math.floor(Date.now() / 60000) - 2));
  } finally { lock.releaseLock(); }
}

function bridgeRoute_(action, body) {
  if (action === 'bridgePullRoles') return bridgeRolesSnapshot_();
  if (action === 'bridgePullTickets') return bridgeTicketsSnapshot_();
  if (action === 'bridgeUpsertTicket') return bridgeUpsertTicket_(body.row, body.sequence, body.dedupe_key);
  if (action === 'bridgeDeleteTicket') return bridgeDeleteTicket_(body.number, body.sequence, body.dedupe_key);
  if (action === 'bridgeBatchUpsertTickets') {
    if (!Array.isArray(body.rows) || body.rows.length > 200) throw new Error('invalid batch');
    return bridgeBatchUpsertTickets_(body.rows, body.sequences || [], body.dedupe_keys || []);
  }
  if (action === 'bridgeMirrorAccess') return bridgeMirrorAccess_(body);
  throw new Error('unknown bridge action');
}

function bridgeTicketsSnapshot_() {
  var sh = ensureSheet_(getSpreadsheet_(), SHEET_TICKETS, TICKETS_HEADERS);
  var lastRow = sh.getLastRow();
  // Never silently reconcile against a partial snapshot: reject oversized sheets.
  if (lastRow > 5001) throw new Error('ticket snapshot too large');
  var rows = lastRow < 2 ? [] : sh.getRange(2, 1, lastRow - 1, 18).getValues();
  rows = rows.filter(function (row) { return String(row[0] || '').trim() !== ''; })
    .map(function (row) { return canonicalBridgeTicketRow_(normalizeTicketDateCells_(row.slice())); });
  var canonical = JSON.stringify({ headers: TICKETS_HEADERS, rows: rows });
  var hash = bytesToHex_(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, canonical, Utilities.Charset.UTF_8));
  return { headers: TICKETS_HEADERS, rows: rows, count: rows.length, hash: hash };
}

function bridgeRolesSnapshot_() {
  var rows = readRoleRows_();
  var canonical = JSON.stringify({ headers: ROLES_HEADERS, rows: rows });
  var hash = bytesToHex_(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, canonical, Utilities.Charset.UTF_8));
  var revision = Number(PropertiesService.getScriptProperties().getProperty('BRIDGE_ROLES_REVISION') || 1);
  return { headers: ROLES_HEADERS, rows: rows, revision: revision, hash: hash, count: rows.length };
}

function bumpRolesRevision_() {
  var props = PropertiesService.getScriptProperties();
  props.setProperty('BRIDGE_ROLES_REVISION', String(Number(props.getProperty('BRIDGE_ROLES_REVISION') || 1) + 1));
}

function plainText_(value) {
  if (value == null) return '';
  var text = String(value);
  // One reversible Sheets escape also preserves legitimate leading apostrophes:
  // "=text" -> "'=text", while "'=text" -> "''=text".
  return /^[']*[\s\u0000-\u001f]*[=+\-@]/.test(text) ? "'" + text : text;
}

var BRIDGE_TICKET_TEXT_COLUMNS = [2, 3, 4, 5, 6, 7, 8, 9, 10, 16, 17];

function sanitizeBridgeTicketRow_(row) {
  BRIDGE_TICKET_TEXT_COLUMNS.forEach(function (i) { row[i] = plainText_(row[i]); });
  return row;
}

function canonicalBridgeTicketRow_(row) {
  BRIDGE_TICKET_TEXT_COLUMNS.forEach(function (i) {
    var text = String(row[i] == null ? '' : row[i]);
    // Decode exactly one escape inserted by plainText_; further quotes are data.
    if (/^'[']*[\s\u0000-\u001f]*[=+\-@]/.test(text)) row[i] = text.slice(1);
  });
  return row;
}

function bridgeStateKey_(prefix, value) {
  if (!/^[A-Za-z0-9:._-]{1,160}$/.test(String(value || ''))) throw new Error('invalid bridge key');
  var digest = bytesToHex_(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, String(value), Utilities.Charset.UTF_8));
  return prefix + digest;
}

function validateBridgeTicketRow_(row) {
  if (!Array.isArray(row) || row.length !== 18 || !/^[A-Z][0-9]{3}$/.test(String(row[0] || ''))) throw new Error('invalid ticket row');
  for (var i = 0; i < row.length; i++) if (String(row[i] == null ? '' : row[i]).length > 20000) throw new Error('cell too large');
  if (row[12] && !/^\d+:[0-5]\d$/.test(String(row[12]))) throw new Error('invalid elapsed duration');
  if (row[15] && !/^\d+:[0-5]\d$/.test(String(row[15]))) throw new Error('invalid idle duration');
}

function bridgeUpsertTicket_(row, sequence, dedupeKey) {
  validateBridgeTicketRow_(row);
  sequence = Number(sequence);
  if (!Number.isInteger(sequence) || sequence < 1) throw new Error('invalid ticket sequence');
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) throw new Error('bridge busy');
  try {
    var desired = normalizeTicketDateCells_(row.slice());
    var props = PropertiesService.getScriptProperties(), doneKey = bridgeStateKey_('bridge_done_', dedupeKey);
    var previous = props.getProperty(doneKey);
    var sequenceKey = bridgeStateKey_('bridge_ticket_seq_', String(desired[0]));
    var previousSequence = Number(props.getProperty(sequenceKey) || 0);
    var sh = ensureSheet_(getSpreadsheet_(), SHEET_TICKETS, TICKETS_HEADERS);
    var map = buildRowMap_(sh), existingIndex = map[String(desired[0])];
    if (sequence < previousSequence) throw new Error('stale ticket sequence');
    if (previous && sequence === previousSequence && existingIndex) {
      var existing = canonicalBridgeTicketRow_(normalizeTicketDateCells_(sh.getRange(existingIndex, 1, 1, 18).getValues()[0]));
      if (JSON.stringify(existing) === JSON.stringify(desired)) return JSON.parse(previous);
    }
    if (sequence === previousSequence) throw new Error('conflicting ticket sequence');
    row = sanitizeBridgeTicketRow_(desired.slice());
    var index = existingIndex || nextTicketRow_(sh);
    sh.getRange(index, 1, 1, 18).setValues([row]);
    invalidateTicketCache_(); invalidateRowMap_();
    props.setProperty(sequenceKey, String(sequence));
    var ack = { number: String(row[0]), row: index, sequence: sequence, dedupe_key: String(dedupeKey) };
    props.setProperty(doneKey, JSON.stringify(ack));
    return ack;
  } finally { lock.releaseLock(); }
}

function bridgeDeleteTicket_(number, sequence, dedupeKey) {
  number = String(number || ''); sequence = Number(sequence);
  if (!/^[A-Z][0-9]{3}$/.test(number) || !Number.isInteger(sequence) || sequence < 1) throw new Error('invalid ticket delete');
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) throw new Error('bridge busy');
  try {
    var props = PropertiesService.getScriptProperties();
    var doneKey = bridgeStateKey_('bridge_done_', dedupeKey), prior = props.getProperty(doneKey);
    var sequenceKey = bridgeStateKey_('bridge_ticket_seq_', number);
    var previousSequence = Number(props.getProperty(sequenceKey) || 0);
    if (sequence < previousSequence) throw new Error('stale ticket sequence');
    var sh = ensureSheet_(getSpreadsheet_(), SHEET_TICKETS, TICKETS_HEADERS);
    var map = buildRowMap_(sh), row = map[number], deleted = false;
    if (row) { sh.deleteRow(row); deleted = true; }
    if (sequence > previousSequence) props.setProperty(sequenceKey, String(sequence));
    // The row map is cached for an hour. Invalidate it before verifying the
    // delete postcondition, otherwise a successfully deleted row looks present.
    invalidateTicketCache_(); invalidateRowMap_();
    var ack = { number: number, sequence: sequence, dedupe_key: String(dedupeKey), deleted: deleted,
      absent: !buildRowMap_(sh)[number], current_sequence: Math.max(sequence, previousSequence) };
    props.setProperty(doneKey, JSON.stringify(ack));
    return ack;
  } finally { lock.releaseLock(); }
}

function bridgeBatchUpsertTickets_(rows, sequences, dedupeKeys) {

  var seen = {};
  rows.forEach(function (row) {
    validateBridgeTicketRow_(row);
    if (seen[String(row[0])]) throw new Error('duplicate ticket in batch');
    seen[String(row[0])] = true;
  });
  if (dedupeKeys.length !== rows.length || sequences.length !== rows.length) throw new Error('invalid batch metadata');
  if (!rows.length) return { upserted: 0, acknowledgments: [] };
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(20000)) throw new Error('bridge busy');
  try {
    var props = PropertiesService.getScriptProperties(), pending = [], acknowledgments = [];
    var sh = ensureSheet_(getSpreadsheet_(), SHEET_TICKETS, TICKETS_HEADERS);
    var map = buildRowMap_(sh);
    rows.forEach(function (row, i) {
      var key = bridgeStateKey_('bridge_done_', dedupeKeys[i]), prior = props.getProperty(key);
      var desired = normalizeTicketDateCells_(row.slice());
      var sequence = Number(sequences[i]);
      if (!Number.isInteger(sequence) || sequence < 1) throw new Error('invalid ticket sequence');
      var sequenceKey = bridgeStateKey_('bridge_ticket_seq_', String(desired[0]));
      var previousSequence = Number(props.getProperty(sequenceKey) || 0);
      if (sequence < previousSequence) throw new Error('stale ticket sequence');
      var existingIndex = map[String(desired[0])], currentMatches = false;
      if (prior && existingIndex) {
        var current = canonicalBridgeTicketRow_(normalizeTicketDateCells_(sh.getRange(existingIndex, 1, 1, 18).getValues()[0]));
        currentMatches = JSON.stringify(current) === JSON.stringify(desired);
      }
      if (prior && sequence === previousSequence && currentMatches) acknowledgments.push(JSON.parse(prior));
      else {
        if (sequence === previousSequence) throw new Error('conflicting ticket sequence');
        pending.push({ row: sanitizeBridgeTicketRow_(desired.slice()), key: key, sequenceKey: sequenceKey,
          sequence: sequence, dedupe: String(dedupeKeys[i]) });
      }
    });
    var next = nextTicketRow_(sh);
    pending.forEach(function (item) {
      item.index = map[String(item.row[0])] || next++;
      map[String(item.row[0])] = item.index;
    });
    pending.sort(function (a, b) { return a.index - b.index; });
    for (var start = 0; start < pending.length;) {
      var end = start + 1;
      while (end < pending.length && pending[end].index === pending[end - 1].index + 1) end++;
      sh.getRange(pending[start].index, 1, end - start, 18).setValues(pending.slice(start, end).map(function (x) { return x.row; }));
      start = end;
    }
    pending.forEach(function (item) {
      props.setProperty(item.sequenceKey, String(item.sequence));
      var ack = { number: String(item.row[0]), row: item.index, sequence: item.sequence, dedupe_key: item.dedupe };
      props.setProperty(item.key, JSON.stringify(ack)); acknowledgments.push(ack);
    });
    invalidateTicketCache_(); invalidateRowMap_();
    acknowledgments.sort(function (a, b) { return String(a.dedupe_key).localeCompare(String(b.dedupe_key)); });
    return { upserted: pending.length, acknowledgments: acknowledgments };
  } finally { lock.releaseLock(); }
}

function bridgeMirrorAccess_(body) {
  var payload = body.payload || {}, id = String(payload.tg_id || payload.creator_id || '');
  if (!/^\d{1,32}$/.test(id)) throw new Error('invalid access payload');
  var sequence = Number(payload.sequence), operation = String(body.operation || '');
  if (!Number.isInteger(sequence) || sequence < 1) throw new Error('invalid access sequence');
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) throw new Error('bridge busy');
  try {
    var props = PropertiesService.getScriptProperties();
    var doneKey = bridgeStateKey_('bridge_done_', body.dedupe_key);
    var priorAck = props.getProperty(doneKey);
    if (priorAck) return JSON.parse(priorAck);
    var sequenceKey = bridgeStateKey_('bridge_seq_', id), previousSequence = Number(props.getProperty(sequenceKey) || 0);
    if (sequence <= previousSequence) throw new Error('stale access sequence');
    if (operation === 'request') {
      upsertRequest_(id, plainText_(String(payload.name || '').slice(0, 80)), plainText_(String(payload.username || '').slice(0, 64)), plainText_(String(payload.photo_url || '').slice(0, 1000)));
    } else if (operation === 'reject') {
      removeRequest_(id);
    } else if (operation === 'approve') {
      upsertRole_(id, plainText_(String(payload.name || '').slice(0, 80)), 'сотрудник', plainText_(String(payload.username || '').slice(0, 64)), plainText_(String(payload.photo_url || '').slice(0, 1000)));
      removeRequest_(id);
    } else if (operation === 'update') {
      var updatedRole = String(payload.role || '');
      if (updatedRole !== 'сотрудник' && updatedRole !== 'админ') throw new Error('invalid role');
      upsertRole_(id, plainText_(String(payload.name || '').slice(0, 80)), updatedRole,
        plainText_(String(payload.username || '').slice(0, 64)),
        plainText_(String(payload.photo_url || '').slice(0, 1000)), true, payload.allowed_types);
      removeRequest_(id);
    } else if (operation === 'rename') {
      var current = getRoleRaw_(id);
      if (current.role === 'гость') throw new Error('role not found');
      upsertRole_(id, plainText_(String(payload.name || '').slice(0, 80)), current.role);
    } else if (operation === 'refresh_contact') {
      updateRoleContact_(id, plainText_(String(payload.username || '').slice(0, 64)), plainText_(String(payload.photo_url || '').slice(0, 1000)));
    } else if (operation === 'revoke' || operation === 'delete') {
      var target = getRoleRaw_(id);
      if (target.role === 'админ' && readRoleRows_().filter(function (r) { return String(r[2]) === 'админ'; }).length <= 1) throw new Error('cannot delete last admin');
      removeRole_(id); removeRequest_(id);
    } else {
      throw new Error('invalid access operation');
    }
    props.setProperty(sequenceKey, String(sequence));
    var ack = { tg_id: id, operation: operation, sequence: sequence, dedupe_key: String(body.dedupe_key) };
    props.setProperty(doneKey, JSON.stringify(ack));
    return ack;
  } finally { lock.releaseLock(); }
}

// Прогон проверки на реальном initData. Вставь строку из window.Telegram.WebApp.initData
// (в приложении: открой консоль или временно выведи tg.initData) и запусти в редакторе.
function testVerifyInitData_() {
  var sample = ''; // ← вставь сюда реальный initData
  if (!sample) return 'Вставь реальный initData в sample (window.Telegram.WebApp.initData).';
  try { return verifyInitData_(sample); }
  catch (e) { return 'FAIL: ' + e.message; }
}

// ============================ SETUP =============================

function setup() {
  if (!BOT_TOKEN) {
    throw userError_('Не задан BOT_TOKEN в Script Properties. Заполни секреты (см. CONFIG / setupSecrets()) и запусти setup() снова.');
  }
  var ss = getSpreadsheet_();
  try { ss.setSpreadsheetTimeZone(DISPLAY_TZ); SpreadsheetApp.flush(); } catch (e) { Logger.log('tz: ' + e); } // местное время (UTC+5)
  var rs = ensureSheet_(ss, SHEET_ROLES, ROLES_HEADERS);
  setupRoleTypeAccess_(rs);
  var ts = ensureSheet_(ss, SHEET_TICKETS, TICKETS_HEADERS);
  formatTicketColumns_(ts);
  migrateTicketDates_(ts); // существующие Date/ISO → читаемый локальный текст
  invalidateTicketCache_();
  invalidateRowMap_();
  ensureSheet_(ss, SHEET_REQUESTS, REQUESTS_HEADERS);
  getAttachmentFolder_(); // создаёт папку в Drive и запрашивает доступ при первом запуске

  if (BOOTSTRAP_ADMIN_IDS) {
    var ids = String(BOOTSTRAP_ADMIN_IDS).split(',');
    for (var i = 0; i < ids.length; i++) {
      var id = ids[i].trim();
      if (id) upsertRole_(id, BOOTSTRAP_ADMIN_NAME, 'админ');
    }
  }
  return 'OK: листы созданы, доступ к типам настроен, админ(ы) добавлены.';
}

function getSpreadsheet_() {
  if (SPREADSHEET_ID) return SpreadsheetApp.openById(SPREADSHEET_ID);
  var active = SpreadsheetApp.getActiveSpreadsheet();
  if (!active) throw userError_('Нет привязанной таблицы. Заполни SPREADSHEET_ID в CONFIG.');
  return active;
}

function ensureSheet_(ss, name, headers) {
  var sh = ss.getSheetByName(name);
  if (!sh) sh = ss.insertSheet(name);
  if (sh.getMaxColumns() < headers.length) {
    sh.insertColumnsAfter(sh.getMaxColumns(), headers.length - sh.getMaxColumns());
  }
  // Всегда синхронизируем строку заголовков: это и создаёт их с нуля, и
  // мигрирует при переименовании/добавлении столбцов (данные строк не трогаются).
  sh.getRange(1, 1, 1, headers.length).setValues([headers]);
  sh.setFrozenRows(1);
  sh.getRange(1, 1, 1, headers.length).setFontWeight('bold');
  return sh;
}

// Разовая настройка матрицы типов в листе «роли». Можно безопасно запускать
// повторно: уже заполненные флажки не перезаписываются.
function setupRoleTypeAccess() {
  var sh = ensureSheet_(getSpreadsheet_(), SHEET_ROLES, ROLES_HEADERS);
  var migrated = setupRoleTypeAccess_(sh);
  CacheService.getScriptCache().remove(CACHE_KEY_ROLES);
  return 'OK: матрица типов настроена; перенесено строк: ' + migrated + '.';
}

function setupRoleTypeAccess_(sh) {
  var lastRow = sh.getLastRow();
  var typeCount = TICKET_TYPES.length;
  var migrated = 0;

  if (lastRow >= 2) {
    var rows = sh.getRange(2, 1, lastRow - 1, ROLES_HEADERS.length).getValues();
    var flags = [];
    for (var i = 0; i < rows.length; i++) {
      var existing = rows[i].slice(ROLE_BASE_COLUMNS, ROLE_BASE_COLUMNS + typeCount);
      if (hasRoleTypeConfiguration_(existing)) {
        flags.push(existing.map(roleTypeCellChecked_));
        continue;
      }
      flags.push(defaultRoleTypeFlags_(rows[i][2], rows[i][0], true));
      migrated++;
    }
    sh.getRange(2, ROLE_TYPE_START_COLUMN, flags.length, typeCount).setValues(flags);
  }

  if (sh.getMaxRows() >= 2) {
    var checkboxRule = SpreadsheetApp.newDataValidation()
      .requireCheckbox()
      .setAllowInvalid(false)
      .build();
    sh.getRange(2, ROLE_TYPE_START_COLUMN, sh.getMaxRows() - 1, typeCount)
      .setDataValidation(checkboxRule);
  }

  var notes = TICKET_TYPES.map(function (type) {
    if (ADMIN_ONLY_TICKET_TYPES.indexOf(type) !== -1) {
      return 'Тип «' + type + '» доступен только администраторам; флажок для сотрудников не применяется.';
    }
    return 'Флажок включён — сотрудник может создавать заявки типа «' + type + '». Для администраторов ограничения не применяются.';
  });
  sh.getRange(1, ROLE_TYPE_START_COLUMN, 1, typeCount)
    .setNotes([notes])
    .setBackground('#d9ead3');
  sh.autoResizeColumns(ROLE_TYPE_START_COLUMN, typeCount);
  return migrated;
}

// Часовой пояс и формат отображения дат в таблице.
var DISPLAY_TZ = 'Asia/Yekaterinburg'; // UTC+5 (Пермь/Екатеринбург/Челябинск/Магнитогорск)
var SHEET_DATE_TEXT_FORMAT = 'dd.MM.yyyy HH:mm:ss';

// Служебные даты храним как стабильный читаемый текст в часовом поясе таблицы.
// API по-прежнему получает ISO через toIso_(), а таймер разбирает текст через
// parseTicketDate_(). Так Google Sheets не меняет тип ячейки самопроизвольно,
// но вместо сырого 2026-07-26T05:38:29.319Z видно 26.07.2026 10:38:29.
function formatTicketColumns_(sh) {
  var rows = sh.getMaxRows();
  var textCols = [2, 12, 13, 14, 15, 16, 17, 18];
  for (var i = 0; i < textCols.length; i++) {
    sh.getRange(1, textCols[i], rows, 1).setNumberFormat('@');
  }
  var dateCols = [2, 12, 14, 15];
  for (var d = 0; d < dateCols.length; d++) {
    sh.setColumnWidth(dateCols[d], 165);
  }
  sh.setColumnWidth(13, 115);
  sh.setColumnWidth(16, 125);
}

// Нормализация старых Date, ISO и "ДД.ММ.ГГГГ ЧЧ:ММ" в единый читаемый текст.
function migrateTicketDates_(sh) {
  var dateCols = [2, 12, 14, 15];
  var last = sh.getLastRow();
  if (last < 2) return;
  for (var c = 0; c < dateCols.length; c++) {
    var rng = sh.getRange(2, dateCols[c], last - 1, 1);
    var vals = rng.getValues();
    var changed = false;
    for (var i = 0; i < vals.length; i++) {
      var v = vals[i][0];
      if (v !== '' && v != null) {
        var normalized = formatTicketDate_(v);
        if (normalized && normalized !== String(v)) {
          vals[i][0] = normalized;
          changed = true;
        } else if (v instanceof Date && normalized) {
          vals[i][0] = normalized;
          changed = true;
        }
      }
    }
    if (changed) rng.setValues(vals);
  }
}

// Ручной безопасный ремонт исторических строк. Сначала создаёт резервную копию
// листа, затем нормализует даты и восстанавливает только однозначные значения.
function repairTicketDataAndFormatting() {
  var ss = getSpreadsheet_();
  var sh = ss.getSheetByName(SHEET_TICKETS);
  if (!sh) throw userError_('Лист «' + SHEET_TICKETS + '» не найден.');

  var stamp = Utilities.formatDate(new Date(), DISPLAY_TZ, 'yyyyMMdd_HHmmss');
  var backupName = SHEET_TICKETS + '_backup_' + stamp;
  var suffix = 2;
  while (ss.getSheetByName(backupName)) {
    backupName = SHEET_TICKETS + '_backup_' + stamp + '_' + suffix++;
  }
  sh.copyTo(ss).setName(backupName);

  var result = repairTicketDataAndFormatting_(sh);
  result.backupSheet = backupName;
  return result;
}

function repairTicketDataAndFormatting_(sh) {
  var last = sh.getLastRow();
  var result = {
    rowsChecked: Math.max(0, last - 1),
    normalizedDateCells: 0,
    recoveredResolvedDates: 0,
    recoveredUpdatedDates: 0,
    filledSpentDurations: 0,
    missingCreated: [],
    missingResolvedForClosed: [],
    missingUpdated: [],
    missingIdleForSolved: []
  };
  if (last < 2) {
    formatTicketColumns_(sh);
    return result;
  }

  var rows = sh.getRange(2, 1, last - 1, TICKETS_HEADERS.length).getValues();
  var dateIndexes = [1, 11, 13, 14];
  var terminal = {};
  terminal[STATUS.DONE] = true;
  terminal[STATUS.REJECTED] = true;

  for (var r = 0; r < rows.length; r++) {
    var row = rows[r];
    if (!row[0]) continue;

    for (var c = 0; c < dateIndexes.length; c++) {
      var idx = dateIndexes[c];
      if (row[idx] === '' || row[idx] == null) continue;
      var formatted = formatTicketDate_(row[idx]);
      if (formatted && (row[idx] instanceof Date || formatted !== String(row[idx]))) {
        row[idx] = formatted;
        result.normalizedDateCells++;
      }
    }

    if (!row[12]) {
      row[12] = '00:00';
      result.filledSpentDurations++;
    }

    // Для закрытой заявки последнее изменение совпадает с моментом закрытия.
    // Копируем только существующую точную дату, ничего не рассчитываем на глаз.
    if (terminal[row[7]] && !row[13] && row[14]) {
      row[13] = row[14];
      result.recoveredResolvedDates++;
    }
    if (!row[14] && row[13]) {
      row[14] = row[13];
      result.recoveredUpdatedDates++;
    }

    if (!row[1]) result.missingCreated.push(String(row[0]));
    if (terminal[row[7]] && !row[13]) result.missingResolvedForClosed.push(String(row[0]));
    if (!row[14]) result.missingUpdated.push(String(row[0]));
    if (row[7] === STATUS.DONE && !row[15]) result.missingIdleForSolved.push(String(row[0]));
  }

  for (var dc = 0; dc < dateIndexes.length; dc++) {
    var dateIdx = dateIndexes[dc];
    sh.getRange(2, dateIdx + 1, rows.length, 1)
      .setValues(rows.map(function (row) { return [row[dateIdx]]; }));
  }
  if (result.filledSpentDurations) {
    sh.getRange(2, 13, rows.length, 1)
      .setValues(rows.map(function (row) { return [row[12]]; }));
  }

  formatTicketColumns_(sh);
  SpreadsheetApp.flush();
  invalidateTicketCache_();
  invalidateRowMap_();
  return result;
}

// Разовое восстановление исторических заявок по встроенной истории правок
// Google Sheets. Временные точки в истории отображаются с точностью до минуты,
// поэтому восстановленные секунды равны 00, кроме Z051, где 05:10 подтверждается
// уже сохранённым временем вне работы.
function restoreTicketsFromCellHistory() {
  var repairs = [
    { number: 'S574', createdAt: '25.07.2026 16:12:00', spent: '01:00', resolvedAt: '25.07.2026 16:34:00', updatedAt: '25.07.2026 16:34:00', idle: '21:00' },
    { number: 'J710', createdAt: '25.07.2026 16:24:00', spent: '00:00', resolvedAt: '25.07.2026 16:31:00', updatedAt: '25.07.2026 16:31:00', idle: '07:00' },
    { number: 'Z051', resolvedAt: '25.07.2026 17:05:10', updatedAt: '25.07.2026 17:05:10' },
    { number: 'M004', createdAt: '25.07.2026 18:02:00', spent: '00:00', resolvedAt: '25.07.2026 18:15:00', updatedAt: '25.07.2026 18:15:00', idle: '13:00' },
    { number: 'S605', createdAt: '25.07.2026 18:07:00', spent: '00:00', resolvedAt: '25.07.2026 18:20:00', updatedAt: '25.07.2026 18:20:00', idle: '13:00' },
    { number: 'O712', createdAt: '25.07.2026 19:12:00', spent: '00:00', resolvedAt: '25.07.2026 19:20:00', updatedAt: '25.07.2026 19:20:00', idle: '08:00' },
    { number: 'K709', createdAt: '25.07.2026 22:03:00', spent: '00:00', resolvedAt: '26.07.2026 09:16:00', updatedAt: '26.07.2026 09:16:00', idle: '673:00' },
    { number: 'B430', createdAt: '25.07.2026 22:21:00', spent: '03:00', resolvedAt: '26.07.2026 09:39:00', updatedAt: '26.07.2026 09:39:00', idle: '655:00' },
    { number: 'C310', createdAt: '26.07.2026 07:30:00', spent: '01:00', resolvedAt: '26.07.2026 09:40:00', updatedAt: '26.07.2026 09:40:00', idle: '129:00' },
    { number: 'L992', createdAt: '26.07.2026 09:28:00', idle: '31:00' },
    { number: 'H373', createdAt: '26.07.2026 09:41:00', idle: '20:00' }
  ];

  var ss = getSpreadsheet_();
  var sh = ss.getSheetByName(SHEET_TICKETS);
  if (!sh) throw userError_('Лист «' + SHEET_TICKETS + '» не найден.');

  var stamp = Utilities.formatDate(new Date(), DISPLAY_TZ, 'yyyyMMdd_HHmmss');
  var backupName = SHEET_TICKETS + '_backup_history_' + stamp;
  var suffix = 2;
  while (ss.getSheetByName(backupName)) {
    backupName = SHEET_TICKETS + '_backup_history_' + stamp + '_' + suffix++;
  }
  sh.copyTo(ss).setName(backupName);

  var rowMap = getRowMap_(sh);
  var updated = [];
  for (var i = 0; i < repairs.length; i++) {
    var repair = repairs[i];
    var rowIdx = rowMap[repair.number];
    if (!rowIdx) throw userError_('Заявка ' + repair.number + ' не найдена.');

    var row = readTicketRow_(sh, rowIdx);
    if (String(row[0]) !== repair.number) {
      throw userError_('Строка заявки ' + repair.number + ' изменилась; восстановление остановлено.');
    }
    if (repair.createdAt != null) row[1] = repair.createdAt;
    if (repair.spent != null) row[12] = repair.spent;
    if (repair.resolvedAt != null) row[13] = repair.resolvedAt;
    if (repair.updatedAt != null) row[14] = repair.updatedAt;
    if (repair.idle != null) row[15] = repair.idle;
    writeRow_(sh, rowIdx, row);
    updated.push(repair.number);
  }

  formatTicketColumns_(sh);
  SpreadsheetApp.flush();
  invalidateTicketCache_();
  invalidateRowMap_();
  return {
    backupSheet: backupName,
    updatedTickets: updated,
    precision: 'minute',
    source: 'Google Sheets cell edit history'
  };
}

// ============================ ROLES =============================

// Роль по tg_id без побочных эффектов (бэкфилла контактов/подмешивания списка
// заявок) — для внутренних проверок доступа (requireAdmin_, isAuthorized_ и
// т.п.), чтобы не зациклиться на getRole_, который при роли «админ» сам
// вызывает getTickets_ (а тот снова проверяет админа).
function getRoleRaw_(tgId) {
  tgId = String(tgId || '');
  if (!tgId) return { role: 'гость', name: '', allowedTypes: [] };
  var rows = readRoleRows_();
  for (var i = 0; i < rows.length; i++) {
    if (String(rows[i][0]) === tgId) {
      return {
        role: rows[i][2] || 'сотрудник',
        name: rows[i][1] || '',
        allowedTypes: allowedTicketTypesForRoleRow_(rows[i])
      };
    }
  }
  return { role: 'гость', name: '', allowedTypes: [] };
}

// Обработчик действия getRole: роль и бэкфилл контактов. Список заявок здесь
// намеренно не читаем: на большой таблице это может занять десятки секунд и
// заблокировать саму проверку доступа. Клиент загружает заявки отдельным запросом.
function getRole_(body) {
  var tgId = String(body.tg_id || '');
  if (!tgId) return { role: 'гость', name: '' };
  var rows = readRoleRows_();
  for (var i = 0; i < rows.length; i++) {
    if (String(rows[i][0]) === tgId) {
      // Бэкфилл контактов: при открытии приложения подтягиваем актуальные
      // username/фото из подписанного initData (есть только в верхнем getRole-вызове).
      if (body.tg_username && (String(rows[i][3] || '') !== String(body.tg_username) ||
          (body.tg_photo && String(rows[i][4] || '') !== String(body.tg_photo)))) {
        try { updateRoleContact_(tgId, body.tg_username, body.tg_photo); } catch (e) { Logger.log('backfill: ' + e); }
      }
      var role = rows[i][2] || 'сотрудник';
      var result = {
        role: role,
        name: rows[i][1] || '',
        allowedTypes: allowedTicketTypesForRoleRow_(rows[i])
      };
      return result;
    }
  }
  // не внесён в «роли» → доступа нет; сообщаем, отправлял ли уже запрос
  return { role: 'гость', name: '', allowedTypes: [], pending: hasPendingRequest_(tgId) };
}

function isAuthorized_(tgId) {
  var role = getRoleRaw_(tgId).role;
  return role === 'сотрудник' || role === 'админ';
}

// Строки листа "роли" (без шапки) с кэшем. tg_id приводится к строке.
function readRoleRows_() {
  var cache = CacheService.getScriptCache();
  var cached = cache.get(CACHE_KEY_ROLES);
  if (cached) { try { return JSON.parse(cached); } catch (e) {} }
  var sh = sheet_(SHEET_ROLES);
  var rows = sh.getDataRange().getValues();
  var data = [];
  for (var i = 1; i < rows.length; i++) {
    if (rows[i][0] !== '' && rows[i][0] != null) {
      // [tg_id, имя, роль, username, photo_url, ...флажки типов]
      var roleRow = [String(rows[i][0]), rows[i][1], rows[i][2], rows[i][3] || '', rows[i][4] || ''];
      for (var c = ROLE_BASE_COLUMNS; c < ROLES_HEADERS.length; c++) {
        roleRow.push(rows[i][c] == null ? '' : rows[i][c]);
      }
      data.push(roleRow);
    }
  }
  cache.put(CACHE_KEY_ROLES, JSON.stringify(data), CACHE_TTL_SECONDS);
  return data;
}

function isAdmin_(tgId) {
  return getRoleRaw_(tgId).role === 'админ';
}

function hasRoleTypeConfiguration_(cells) {
  for (var i = 0; i < cells.length; i++) {
    if (cells[i] !== '' && cells[i] != null) return true;
  }
  return false;
}

function roleTypeCellChecked_(value) {
  if (value === true) return true;
  var normalized = String(value == null ? '' : value).trim().toLowerCase();
  return normalized === 'true' || normalized === 'да' || normalized === '1' ||
    normalized === 'yes' || normalized === 'x' || normalized === '✓';
}

function defaultRoleTypeFlags_(role, tgId, includeLegacyAccess) {
  if (String(role || '') === 'админ') {
    return TICKET_TYPES.map(function () { return true; });
  }
  var legacyFullAccess = includeLegacyAccess &&
    LEGACY_RESTRICTED_TYPE_IDS.indexOf(String(tgId || '')) !== -1;
  return TICKET_TYPES.map(function (type) {
    return legacyFullAccess || DEFAULT_EMPLOYEE_TICKET_TYPES.indexOf(type) !== -1;
  });
}

function allowedTicketTypesForRoleRow_(row) {
  if (String(row[2] || '') === 'админ') return TICKET_TYPES.slice();
  var cells = row.slice(ROLE_BASE_COLUMNS, ROLE_BASE_COLUMNS + TICKET_TYPES.length);
  if (!hasRoleTypeConfiguration_(cells)) {
    return TICKET_TYPES.filter(function (type) {
      return DEFAULT_EMPLOYEE_TICKET_TYPES.indexOf(type) !== -1;
    });
  }
  return TICKET_TYPES.filter(function (type, index) {
    return ADMIN_ONLY_TICKET_TYPES.indexOf(type) === -1 && roleTypeCellChecked_(cells[index]);
  });
}

function assertTicketTypeAllowed_(tgId, type) {
  type = String(type || '').trim();
  var access = getRoleRaw_(tgId);
  if (access.allowedTypes.indexOf(type) === -1) {
    throw userError_('Тип заявки «' + type + '» недоступен для вашего аккаунта. Обратитесь к администратору.');
  }
}

function upsertRole_(tgId, name, role, username, photo, replaceContacts, allowedTypes) {
  name = plainText_(name); username = plainText_(username); photo = plainText_(photo);
  var sh = sheet_(SHEET_ROLES);
  var rows = sh.getDataRange().getValues();
  var typeFlags = Array.isArray(allowedTypes) ? TICKET_TYPES.map(function (type) {
    return allowedTypes.indexOf(type) !== -1;
  }) : null;
  var updated = false;
  for (var i = 1; i < rows.length; i++) {
    if (String(rows[i][0]) === String(tgId)) {
      // Контакты не затираем пустыми — сохраняем прежние, если новые не переданы.
      var u = replaceContacts ? username : ((username != null && username !== '') ? username : (rows[i][3] || ''));
      var p = replaceContacts ? photo : ((photo != null && photo !== '') ? photo : (rows[i][4] || ''));
      sh.getRange(i + 1, 1, 1, 5).setValues([[tgId, name, role, u, p]]);
      if (typeFlags) sh.getRange(i + 1, 6, 1, typeFlags.length).setValues([typeFlags]);
      updated = true;
      break;
    }
  }
  if (!updated) {
    var newRow = [tgId, name, role, username || '', photo || '']
      .concat(typeFlags || defaultRoleTypeFlags_(role, tgId, false));
    sh.appendRow(newRow);
  }
  CacheService.getScriptCache().remove(CACHE_KEY_ROLES);
  bumpRolesRevision_();
}

// Обновить только контакты сотрудника (username/photo) — бэкфилл при открытии приложения.
function updateRoleContact_(tgId, username, photo) {
  var sh = sheet_(SHEET_ROLES);
  var rows = sh.getDataRange().getValues();
  for (var i = 1; i < rows.length; i++) {
    if (String(rows[i][0]) === String(tgId)) {
      var u = username || rows[i][3] || '';
      var p = photo || rows[i][4] || '';
      if (String(rows[i][3] || '') === String(u) && String(rows[i][4] || '') === String(p)) return false;
      sh.getRange(i + 1, 4, 1, 2).setValues([[u, p]]);
      CacheService.getScriptCache().remove(CACHE_KEY_ROLES);
      bumpRolesRevision_();
      return true;
    }
  }
  return false;
}

// ============================ TICKETS ===========================

function createTicket_(body) {
  if (!isAuthorized_(body.tg_id)) {
    throw userError_('Нет доступа. Запросите доступ у администратора.');
  }
  rateLimit_(body.tg_id, 'create', 30);
  var required = ['type', 'city', 'office', 'name', 'description'];
  for (var i = 0; i < required.length; i++) {
    if (!body[required[i]] || !String(body[required[i]]).trim()) {
      throw userError_('Не заполнено поле: ' + required[i]);
    }
  }
  assertTicketTypeAllowed_(body.tg_id, body.type);
  var sh = sheet_(SHEET_TICKETS);
  var number = generateNumber_(sh);
  var now = nowTicketDate_();
  var fileUrl = body.image ? saveFile_(body.image, number, body.filename) : '';

  var row = [
    number,
    now,
    String(body.type).trim(),
    String(body.city).trim(),
    String(body.office).trim(),
    String(body.name).trim(),
    String(body.description).trim(),
    STATUS.NEW,
    String(body.tg_id || ''),
    '', '', '',      // 9-11: админ_tg_id, админ_имя, work_started_at
    '00:00',         // 12 Затраченное время
    '',              // 13 дата_решения
    now,             // 14 последнее_изменение
    '',              // 15 Время не в работе (заполнится при взятии)
    fileUrl,         // 16 Файл
    ''               // 17 основание
  ];
  sanitizeBridgeTicketRow_(row);
  // Пишем сразу
  // getLastRow() — иначе посторонний контент в дальних ячейках уводит запись вниз.
  sh.getRange(nextTicketRow_(sh), 1, 1, row.length).setValues([row]);
  updateTicketCachesAfterWrite_(row);
  invalidateRowMap_();

  notify_('🔴 Новая заявка ' + number +
    '\nТип: ' + row[2] +
    '\nГород/офис: ' + row[3] + ' / ' + row[4] +
    '\nОт: ' + row[5] +
    '\n\n' + row[6]);

  return { ticket: rowToTicket_(row) };
}

function getMyTickets_(body) {
  var tgId = String(body.tg_id || '');
  var all = readTickets_();
  var mine = all.filter(function (t) { return String(t.creatorId) === tgId; });
  mine.sort(byCreatedDesc_);
  return { tickets: mine };
}

function getTickets_(body) {
  requireAdmin_(body);
  // Активные = всё, кроме терминальных (решена/отклонена). «На доработке» видна
  // админам (read-only, ждёт сотрудника), «исправлена» — снова берётся в работу.
  var all = readActiveTicketRows_().map(rowToTicket_);
  all.sort(byCreatedDesc_);
  return { tickets: all };
}

// Страница истории фиксированного размера (не берём из body — иначе клиент
// мог бы запросить всю историю разом одним большим page size).
var HISTORY_PAGE_SIZE = 15;

function getHistory_(body) {
  requireAdmin_(body);
  // История = терминальные статусы: решена и отклонена.
  var done = readDoneTicketRows_().map(rowToTicket_);

  var q = searchText_(body.q);
  if (q) {
    done = done.filter(function (t) {
      return searchText_(t.number).indexOf(q) !== -1 ||
             searchText_(t.description).indexOf(q) !== -1 ||
             searchText_(t.senderName).indexOf(q) !== -1 ||
             searchText_(t.type).indexOf(q) !== -1 ||
             searchText_(t.city).indexOf(q) !== -1 ||
             searchText_(t.office).indexOf(q) !== -1 ||
             searchText_(t.adminName).indexOf(q) !== -1 ||
             searchText_(t.reason).indexOf(q) !== -1;
    });
  }

  done.sort(function (a, b) {
    return String(b.resolvedAt).localeCompare(String(a.resolvedAt));
  });
  var totalPages = Math.max(1, Math.ceil(done.length / HISTORY_PAGE_SIZE));
  var page = Math.floor(Number(body.page)) || 1;
  page = Math.min(Math.max(1, page), totalPages);
  var start = (page - 1) * HISTORY_PAGE_SIZE;
  return {
    tickets: done.slice(start, start + HISTORY_PAGE_SIZE),
    page: page,
    totalPages: totalPages,
    total: done.length
  };
}

function takeTicket_(body) {
  requireAdmin_(body);
  return withTicket_(body.number, function (sh, rowIdx, row) {
    // Взять можно только новую («создана») или возвращённую сотрудником («исправлена»).
    // Для «исправлена» накопленное время в кол.12 сохраняется → таймер продолжит с него.
    if (row[7] !== STATUS.NEW && row[7] !== STATUS.FIXED) {
      throw userError_('Заявку нельзя взять в работу в текущем статусе.');
    }
    var now = nowTicketDate_();
    // «Время не в работе»: от создания до первого взятия (заполняется один раз).
    if (!row[15] && row[1]) {
      var createdDate = parseTicketDate_(row[1]);
      var takenDate = parseTicketDate_(now);
      if (createdDate && takenDate) {
        var idleSec = Math.max(0, Math.floor((takenDate.getTime() - createdDate.getTime()) / 1000));
        row[15] = formatMinSec_(idleSec);
      }
    }
    row[7] = STATUS.WORK;
    row[9] = String(body.tg_id || '');
    row[10] = String(body.name || getRoleRaw_(body.tg_id).name || '');
    row[11] = now;            // work_started_at
    row[14] = now;
    writeRow_(sh, rowIdx, row);
    notify_('🟡 Заявка ' + row[0] + ' взята в работу (' + row[10] + ')');
    return { ticket: rowToTicket_(row) };
  });
}

function pauseTicket_(body) {
  requireAdmin_(body);
  return withTicket_(body.number, function (sh, rowIdx, row) {
    if (row[7] !== STATUS.WORK) throw userError_('Заявка не в работе.');
    var acc = parseDuration_(row[12]) + elapsedSinceStart_(row[11]);
    row[7] = STATUS.PAUSE;
    row[11] = '';             // таймер остановлен
    row[12] = formatMinSec_(acc);
    row[14] = nowTicketDate_();
    writeRow_(sh, rowIdx, row);
    return { ticket: rowToTicket_(row) };
  });
}

function resumeTicket_(body) {
  requireAdmin_(body);
  return withTicket_(body.number, function (sh, rowIdx, row) {
    if (row[7] !== STATUS.PAUSE) throw userError_('Заявку нельзя возобновить.');
    row[7] = STATUS.WORK;
    row[11] = nowTicketDate_();
    row[14] = row[11];
    writeRow_(sh, rowIdx, row);
    notify_('🟡 Заявка ' + row[0] + ' снова в работе');
    return { ticket: rowToTicket_(row) };
  });
}

function finishTicket_(body) {
  requireAdmin_(body);
  var comment = String(body.comment || '').trim().slice(0, 1000);
  return withTicket_(body.number, function (sh, rowIdx, row) {
    if (row[7] !== STATUS.WORK && row[7] !== STATUS.PAUSE) {
      throw userError_('Завершить можно только заявку в работе или на паузе.');
    }
    var acc = parseDuration_(row[12]) + elapsedSinceStart_(row[11]);
    var now = nowTicketDate_();
    row[7] = STATUS.DONE;
    row[11] = '';
    row[12] = formatMinSec_(acc);
    row[13] = now;            // дата_решения
    row[14] = now;
    row[17] = comment;        // комментарий администратора при завершении
    writeRow_(sh, rowIdx, row);
    var userMsg = '✅ Заявка ' + row[0] + ' решена.' +
      (comment ? '\nКомментарий: ' + comment : '');
    notifyBatch_([
      { chatId: row[8], text: userMsg },
      { chatId: NOTIFY_CHAT_ID, threadId: NOTIFY_THREAD_ID, text: '🟢 Заявка ' + row[0] + ' решена\nЗатрачено: ' + formatMinSec_(acc) +
        (row[10] ? '\nИсполнитель: ' + row[10] : '') +
        (comment ? '\nКомментарий: ' + comment : '') }
    ]);
    return { ticket: rowToTicket_(row) };
  });
}

// Отправить заявку обратно сотруднику на доработку (с основанием).
// Таймер останавливается (накопленное в кол.12 сохраняется, НЕ обнуляется),
// чтобы при повторном взятии в работу он продолжился с того же места.
function returnTicket_(body) {
  requireAdmin_(body);
  var reason = String(body.reason || '').trim();
  if (!reason) throw userError_('Укажите основание доработки.');
  return withTicket_(body.number, function (sh, rowIdx, row) {
    if (row[7] !== STATUS.WORK && row[7] !== STATUS.PAUSE) {
      throw userError_('На доработку можно отправить только заявку в работе или на паузе.');
    }
    var acc = parseDuration_(row[12]) + elapsedSinceStart_(row[11]);
    row[7] = STATUS.REVISION;
    row[11] = '';                 // таймер остановлен
    row[12] = formatMinSec_(acc); // накопленное сохранено
    row[14] = nowTicketDate_();
    row[17] = reason;
    writeRow_(sh, rowIdx, row);
    notifyBatch_([
      { chatId: row[8], text: '✏️ Заявка ' + row[0] + ' возвращена на доработку.\nОснование: ' + reason +
        '\nОткройте приложение, исправьте данные и отправьте заявку снова.' },
      { chatId: NOTIFY_CHAT_ID, threadId: NOTIFY_THREAD_ID,
        text: '✏️ Заявка ' + row[0] + ' отправлена на доработку (' + (row[10] || '—') + ')\nОснование: ' + reason }
    ]);
    return { ticket: rowToTicket_(row) };
  });
}

// Отклонить заявку (с основанием). Терминальный статус, как «решена».
// Доступно для поступающих (создана/исправлена) и взятых в работу (в работе/на паузе).
function rejectTicket_(body) {
  requireAdmin_(body);
  var reason = String(body.reason || '').trim();
  if (!reason) throw userError_('Укажите основание отклонения.');
  return withTicket_(body.number, function (sh, rowIdx, row) {
    if (row[7] === STATUS.DONE || row[7] === STATUS.REJECTED) {
      throw userError_('Заявка уже закрыта.');
    }
    var acc = parseDuration_(row[12]) + elapsedSinceStart_(row[11]);
    var now = nowTicketDate_();
    row[7] = STATUS.REJECTED;
    row[9] = String(body.tg_id || row[9] || '');
    row[10] = String(body.name || getRoleRaw_(body.tg_id).name || row[10] || '');
    row[11] = '';
    row[12] = formatMinSec_(acc);
    row[13] = now;                // дата_решения (для истории)
    row[14] = now;
    row[17] = reason;
    writeRow_(sh, rowIdx, row);
    notifyBatch_([
      { chatId: row[8], text: '🚫 Заявка ' + row[0] + ' отклонена.\nОснование: ' + reason },
      { chatId: NOTIFY_CHAT_ID, threadId: NOTIFY_THREAD_ID,
        text: '🚫 Заявка ' + row[0] + ' отклонена (' + (row[10] || '—') + ')\nОснование: ' + reason }
    ]);
    return { ticket: rowToTicket_(row) };
  });
}

// Сотрудник исправил заявку, возвращённую на доработку, и отправляет снова.
// Может править все поля. Накопленное время сохраняется; статус → «исправлена»,
// после чего админ снова берёт её в работу (таймер продолжается).
function resubmitTicket_(body) {
  if (!isAuthorized_(body.tg_id)) {
    throw userError_('Нет доступа. Запросите доступ у администратора.');
  }
  rateLimit_(body.tg_id, 'resubmit', 30);
  var required = ['type', 'city', 'office', 'name', 'description'];
  for (var i = 0; i < required.length; i++) {
    if (!body[required[i]] || !String(body[required[i]]).trim()) {
      throw userError_('Не заполнено поле: ' + required[i]);
    }
  }
  assertTicketTypeAllowed_(body.tg_id, body.type);
  return withTicket_(body.number, function (sh, rowIdx, row) {
    if (String(row[8]) !== String(body.tg_id || '')) {
      throw userError_('Дорабатывать заявку может только её автор.');
    }
    if (row[7] !== STATUS.REVISION) {
      throw userError_('Эту заявку нельзя доработать (она не на доработке).');
    }
    row[2] = String(body.type).trim();
    row[3] = String(body.city).trim();
    row[4] = String(body.office).trim();
    row[5] = String(body.name).trim();
    row[6] = String(body.description).trim();
    if (body.image) row[16] = saveFile_(body.image, row[0], body.filename); // замена вложения по желанию
    row[7] = STATUS.FIXED;
    row[14] = nowTicketDate_();
    writeRow_(sh, rowIdx, row);
    notify_('🔧 Заявка ' + row[0] + ' исправлена и возвращена в работу\nТип: ' + row[2] +
      '\nГород/офис: ' + row[3] + ' / ' + row[4] + '\nОт: ' + row[5]);
    return { ticket: rowToTicket_(row) };
  });
}

// Передать заявку другому админу (таймер продолжает идти у нового исполнителя).
function transferTicket_(body) {
  requireAdmin_(body);
  var toId = String(body.to_tg_id || '');
  if (!toId) throw userError_('Не выбран администратор.');
  var to = getRoleRaw_(toId);
  if (to.role !== 'админ') throw userError_('Получатель не является администратором.');
  return withTicket_(body.number, function (sh, rowIdx, row) {
    if (row[7] !== STATUS.WORK && row[7] !== STATUS.PAUSE) {
      throw userError_('Передать можно только заявку в работе или на паузе.');
    }
    var fromName = String(body.name || getRoleRaw_(body.tg_id).name || row[10] || '');
    row[9] = toId;
    row[10] = to.name || '';
    row[14] = nowTicketDate_();
    writeRow_(sh, rowIdx, row);
    // Здесь групповое сообщение зависит от результата личного (доставлено ли),
    // поэтому вызовы остаются последовательными — параллелить нечего.
    var delivered = notifyUser_(toId, '🔁 Вам передали заявку ' + row[0] +
      (fromName ? ' (от ' + fromName + ')' : ''));
    notify_('🔁 Заявка ' + row[0] + ' передана: ' + (fromName || '—') + ' → ' + (to.name || toId) +
      (delivered ? '' : '\n(личное уведомление не дошло — получатель не запускал бота в личке)'));
    return { ticket: rowToTicket_(row) };
  });
}

// Прикрепить/заменить файл у заявки (админ). Имя действия историческое.
function addScreenshot_(body) {
  requireAdmin_(body);
  if (!body.image) throw userError_('Нет файла.');
  rateLimit_(body.tg_id, 'file', 60);
  return withTicket_(body.number, function (sh, rowIdx, row) {
    row[16] = saveFile_(body.image, row[0], body.filename);
    row[14] = nowTicketDate_();
    writeRow_(sh, rowIdx, row);
    return { ticket: rowToTicket_(row) };
  });
}

// Список админов для передачи заявок.
function getAdmins_(body) {
  requireAdmin_(body);
  var rows = readRoleRows_();
  var admins = [];
  for (var i = 0; i < rows.length; i++) {
    if (String(rows[i][2]) === 'админ') {
      admins.push({ tg_id: String(rows[i][0]), name: rows[i][1] || String(rows[i][0]) });
    }
  }
  return { admins: admins };
}

// ============================ LEADERBOARD (рейтинг админов) ============================
// Место = сумма очков за 2 ранга: по количеству закрытых заявок и по средней
// скорости обработки (меньше среднее время — лучше). За 1/2/3 место в каждом
// ранге начисляется 3/2/1 очко — устойчивее к перекосам, чем один общий балл
// из разных по природе метрик (штуки vs минуты).
function getLeaderboard_(body) {
  requireAdmin_(body);
  var period = (body.period === 'month') ? 'month' : 'all';
  var cacheKey = period === 'month' ? CACHE_KEY_LEADERBOARD_MONTH : CACHE_KEY_LEADERBOARD_ALL;
  var cache = CacheService.getScriptCache();
  var cached = cache.get(cacheKey);
  if (cached) { try { return JSON.parse(cached); } catch (e) {} }

  // История = терминальные статусы (решена/отклонена), как и getHistory_.
  var done = readDoneTicketRows_().map(rowToTicket_);
  if (period === 'month') {
    var now = new Date();
    var y = now.getFullYear(), m = now.getMonth();
    done = done.filter(function (t) {
      if (!t.resolvedAt) return false;
      var d = new Date(t.resolvedAt);
      return d.getFullYear() === y && d.getMonth() === m;
    });
  }

  var stats = {}; // tg_id -> агрегаты
  done.forEach(function (t) {
    var id = t.adminId;
    if (!id) return;
    if (!stats[id]) stats[id] = { tg_id: id, name: t.adminName || id, count: 0, totalSec: 0, byType: {}, byCity: {} };
    var s = stats[id];
    s.count++;
    s.totalSec += t.elapsedSeconds || 0; // накопленное рабочее время заявки, не календарное
    if (t.adminName) s.name = t.adminName; // берём самое свежее отображаемое имя
    if (t.type) s.byType[t.type] = (s.byType[t.type] || 0) + 1;
    if (t.city) s.byCity[t.city] = (s.byCity[t.city] || 0) + 1;
  });

  var list = Object.keys(stats).map(function (id) {
    var s = stats[id];
    return {
      tg_id: s.tg_id,
      name: s.name,
      count: s.count,
      avgSeconds: s.count ? Math.round(s.totalSec / s.count) : 0,
      favoriteType: topKey_(s.byType),
      favoriteCity: topKey_(s.byCity)
    };
  });

  rankPoints_(list, function (a, b) { return b.count - a.count; }, 'countPoints');
  rankPoints_(list, function (a, b) { return a.avgSeconds - b.avgSeconds; }, 'speedPoints');
  list.forEach(function (e) { e.score = e.countPoints + e.speedPoints; });
  list.sort(function (a, b) {
    if (b.score !== a.score) return b.score - a.score;
    if (b.count !== a.count) return b.count - a.count;
    return a.avgSeconds - b.avgSeconds;
  });

  var result = { period: period, leaders: list };
  var json = JSON.stringify(result);
  if (json.length < 95000) cache.put(cacheKey, json, CACHE_TTL_SECONDS);
  return result;
}

// Ключ с максимальным значением в объекте-счётчике ({тип/город: count}).
function topKey_(obj) {
  var bestKey = '', bestVal = -1;
  Object.keys(obj).forEach(function (k) {
    if (obj[k] > bestVal) { bestVal = obj[k]; bestKey = k; }
  });
  return bestKey;
}

// Начисляет очки 3/2/1 за 1/2/3 место по компаратору cmp (сортирует "лучших" в начало).
function rankPoints_(list, cmp, pointsField) {
  var sorted = list.slice().sort(cmp);
  var points = [3, 2, 1];
  sorted.forEach(function (e, i) { e[pointsField] = i < points.length ? points[i] : 0; });
}

// ============================ ACCESS (доступ) ============================

// Сотрудник запрашивает доступ.
function requestAccess_(body) {
  var tgId = String(body.tg_id || '');
  if (!tgId) throw userError_('Не удалось определить профиль. Откройте бота в официальном Telegram.');
  if (isAuthorized_(tgId)) return { ok: true, already: true };
  rateLimit_(tgId, 'access', 5);
  upsertRequest_(tgId, body.name, body.tg_username, body.tg_photo);
  notify_('🔑 Запрос доступа к боту: ' + (body.name ? String(body.name) + ' ' : '') +
    (body.tg_username ? '@' + body.tg_username + ' ' : '') + '(id ' + tgId + ')');
  return { ok: true };
}

// Страница списка сотрудников фиксированного размера (не берём из body, как и
// для истории заявок) + опциональный поиск по имени/нику.
var ACCESS_PAGE_SIZE = 10;

// Для админской вкладки: ожидающие запросы (без пагинации — их обычно мало и
// админу нужно видеть все сразу) + сотрудники с доступом (постранично, с
// опциональным поиском по имени или @нику).
function getAccess_(body) {
  requireAdmin_(body);
  var roleRows = readRoleRows_();
  var inRoles = {};
  roleRows.forEach(function (r) { inRoles[String(r[0])] = true; });
  var requests = readRequests_().filter(function (r) { return !inRoles[r.tg_id]; });

  var employees = roleRows.filter(function (r) { return String(r[2]) === 'сотрудник'; })
    .map(function (r) {
      return { tg_id: String(r[0]), name: r[1] || String(r[0]), username: r[3] || '', photo_url: r[4] || '' };
    });

  var q = String(body.q || '').trim().toLowerCase();
  if (q) {
    employees = employees.filter(function (e) {
      return (e.name || '').toLowerCase().indexOf(q) !== -1 ||
             (e.username || '').toLowerCase().indexOf(q) !== -1;
    });
  }
  employees.sort(function (a, b) { return String(a.name || '').localeCompare(String(b.name || ''), 'ru'); });

  var totalPages = Math.max(1, Math.ceil(employees.length / ACCESS_PAGE_SIZE));
  var page = Math.floor(Number(body.page)) || 1;
  page = Math.min(Math.max(1, page), totalPages);
  var start = (page - 1) * ACCESS_PAGE_SIZE;

  return {
    requests: requests,
    employees: employees.slice(start, start + ACCESS_PAGE_SIZE),
    page: page,
    totalPages: totalPages,
    total: employees.length
  };
}

// Админ одобряет доступ → сотрудник попадает в «роли» (с контактами из запроса).
function approveAccess_(body) {
  requireAdmin_(body);
  var tgId = String(body.target_tg_id || '');
  if (!tgId) throw userError_('Не выбран сотрудник.');
  var name = String(body.target_name || '');
  var req = readRequests_().filter(function (r) { return r.tg_id === tgId; })[0] || {};
  if (!name) name = req.name || '';
  upsertRole_(tgId, name, 'сотрудник', req.username, req.photo_url);
  removeRequest_(tgId);
  notifyUser_(tgId, '✅ Доступ к боту одобрен. Откройте приложение заново.');
  return { ok: true };
}

// Админ отклоняет запрос доступа → запись удаляется из «запросы».
function rejectAccess_(body) {
  requireAdmin_(body);
  var tgId = String(body.target_tg_id || '');
  if (!tgId) throw userError_('Не выбран запрос.');
  removeRequest_(tgId);
  notifyUser_(tgId, '⛔ Запрос на доступ к боту отклонён администратором.');
  return { ok: true };
}

// Админ переименовывает сотрудника с доступом (имя автозаполняется самим сотрудником).
function renameRole_(body) {
  requireAdmin_(body);
  var tgId = String(body.target_tg_id || '');
  var name = String(body.target_name || '').trim();
  if (!tgId) throw userError_('Не выбран сотрудник.');
  if (!name) throw userError_('Укажите новое имя.');
  var role = getRoleRaw_(tgId);
  if (role.role === 'гость') throw userError_('Сотрудник не найден.');
  upsertRole_(tgId, name.slice(0, 80), role.role); // username/photo сохранятся
  return { ok: true, name: name.slice(0, 80) };
}

// Разовый бэкфилл ников: для сотрудников без username спрашиваем Telegram getChat.
// Работает для тех, кто запускал бота (т.е. пользовался Mini App). Фото не тянем.
function refreshContacts_(body) {
  requireAdmin_(body);
  var rows = readRoleRows_();
  var updated = 0, failed = 0;
  for (var i = 0; i < rows.length; i++) {
    if (String(rows[i][2]) !== 'сотрудник') continue;
    if (rows[i][3]) continue; // ник уже есть
    var info = tgGetChat_(rows[i][0]);
    if (info && info.username) { updateRoleContact_(rows[i][0], info.username, rows[i][4] || ''); updated++; }
    else failed++;
  }
  return { updated: updated, failed: failed };
}

function tgGetChat_(chatId) {
  if (!BOT_TOKEN || !chatId) return null;
  try {
    var res = UrlFetchApp.fetch('https://api.telegram.org/bot' + BOT_TOKEN + '/getChat?chat_id=' +
      encodeURIComponent(chatId), { muteHttpExceptions: true });
    var j = JSON.parse(res.getContentText() || '{}');
    if (j && j.ok && j.result) {
      return {
        username: j.result.username || '',
        name: [j.result.first_name, j.result.last_name].filter(Boolean).join(' ')
      };
    }
  } catch (e) { Logger.log('getChat: ' + e); }
  return null;
}

// Админ закрывает доступ → строка сотрудника удаляется из «роли».
function revokeAccess_(body) {
  requireAdmin_(body);
  var tgId = String(body.target_tg_id || '');
  if (!tgId) throw userError_('Не выбран сотрудник.');
  var target = getRoleRaw_(tgId);
  if (target.role === 'админ' && readRoleRows_().filter(function (r) {
    return String(r[2]) === 'админ';
  }).length <= 1) throw userError_('Нельзя закрыть доступ последнему администратору.');
  removeRole_(tgId);
  removeRequest_(tgId);
  notifyUser_(tgId, '⛔ Доступ к боту закрыт администратором.');
  return { ok: true };
}

// ---- работа с листом «роли» (удаление) и «запросы» ----

function removeRole_(tgId) {
  var sh = sheet_(SHEET_ROLES);
  var rows = sh.getDataRange().getValues();
  var removed = false;
  for (var i = rows.length - 1; i >= 1; i--) {
    if (String(rows[i][0]) === String(tgId)) { sh.deleteRow(i + 1); removed = true; }
  }
  CacheService.getScriptCache().remove(CACHE_KEY_ROLES);
  if (removed) bumpRolesRevision_();
  return removed;
}

// Ручные изменения в Google Таблице сразу сбрасывают соответствующий кэш.
function onEdit(e) {
  try {
    if (!e || !e.range) return;
    var sheetName = e.range.getSheet().getName();
    if (sheetName === SHEET_ROLES) {
      CacheService.getScriptCache().remove(CACHE_KEY_ROLES);
      bumpRolesRevision_();
    } else if (sheetName === SHEET_TICKETS) {
      invalidateTicketCache_();
      invalidateRowMap_();
    }
  } catch (err) {
    Logger.log('onEdit cache: ' + err);
  }
}

function requestsSheet_() {
  var ss = getSpreadsheet_();
  var sh = ss.getSheetByName(SHEET_REQUESTS);
  if (!sh) sh = ensureSheet_(ss, SHEET_REQUESTS, REQUESTS_HEADERS);
  return sh;
}

function readRequests_() {
  var sh = requestsSheet_();
  var rows = sh.getDataRange().getValues();
  var out = [];
  for (var i = 1; i < rows.length; i++) {
    if (rows[i][0]) out.push({
      tg_id: String(rows[i][0]), name: rows[i][1] || '', date: rows[i][2] || '',
      username: rows[i][3] || '', photo_url: rows[i][4] || ''
    });
  }
  return out;
}

function upsertRequest_(tgId, name, username, photo) {
  var sh = requestsSheet_();
  var rows = sh.getDataRange().getValues();
  for (var i = 1; i < rows.length; i++) {
    if (String(rows[i][0]) === String(tgId)) {
      sh.getRange(i + 1, 1, 1, 5).setValues([[
        tgId, name || rows[i][1], new Date(),
        username || rows[i][3] || '', photo || rows[i][4] || ''
      ]]);
      return;
    }
  }
  sh.appendRow([tgId, name || '', new Date(), username || '', photo || '']);
}

function removeRequest_(tgId) {
  var sh = requestsSheet_();
  var rows = sh.getDataRange().getValues();
  for (var i = rows.length - 1; i >= 1; i--) {
    if (String(rows[i][0]) === String(tgId)) sh.deleteRow(i + 1);
  }
}

function hasPendingRequest_(tgId) {
  try {
    var ss = getSpreadsheet_();
    var sh = ss.getSheetByName(SHEET_REQUESTS);
    if (!sh) return false;
    var rows = sh.getDataRange().getValues();
    for (var i = 1; i < rows.length; i++) {
      if (String(rows[i][0]) === String(tgId)) return true;
    }
  } catch (e) { Logger.log('hasPendingRequest_: ' + e); }
  return false;
}

// ============================ HELPERS ===========================

function sheet_(name) {
  var ss = getSpreadsheet_();
  var sh = ss.getSheetByName(name);
  if (!sh) throw userError_('Лист "' + name + '" не найден. Запусти setup().');
  return sh;
}

function requireAdmin_(body) {
  if (!isAdmin_(body.tg_id)) throw userError_('Доступ только для администраторов.');
}

function readTickets_() {
  // Таймер пересчитывается в rowToTicket_ при каждом вызове, поэтому даже из
  // кэша время остаётся точным.
  return readTicketDataRows_().map(function (row) { return rowToTicket_(row); });
}

// Полный список строк (активные+история) — нужен только там, где нельзя
// заранее отфильтровать по статусу (например «Мои заявки» сотрудника).
function readTicketDataRows_() {
  return readActiveTicketRows_().concat(readDoneTicketRows_());
}

// Только активные (не решена/отклонена) — использует getTickets_.
function readActiveTicketRows_() {
  var cache = CacheService.getScriptCache();
  var cached = getChunkedJsonCache_(cache, CACHE_KEY_TICKETS_ACTIVE);
  if (cached) return cached;
  return splitAndCacheTicketRows_().active;
}

// Только завершённые (решена/отклонена) — использует getHistory_.
function readDoneTicketRows_() {
  var cache = CacheService.getScriptCache();
  var cached = getChunkedJsonCache_(cache, CACHE_KEY_TICKETS_DONE);
  if (cached) return cached;
  return splitAndCacheTicketRows_().done;
}

// CacheService ограничивает одно значение 100 КБ. История быстро перерастает
// этот лимит, поэтому сохраняем JSON частями и собираем при чтении.
function getChunkedJsonCache_(cache, key) {
  var direct = cache.get(key);
  if (direct) { try { return JSON.parse(direct); } catch (e) {} }
  var count = Number(cache.get(key + '_chunks') || 0);
  if (!count) return null;
  var keys = [];
  for (var i = 0; i < count; i++) keys.push(key + '_chunk_' + i);
  var parts = cache.getAll(keys);
  var json = '';
  for (var j = 0; j < keys.length; j++) {
    if (!parts[keys[j]]) return null;
    json += parts[keys[j]];
  }
  try { return JSON.parse(json); } catch (e2) { return null; }
}

function putChunkedJsonCache_(cache, key, value) {
  var json = JSON.stringify(value);
  if (json.length < 95000) {
    cache.put(key, json, CACHE_TTL_SECONDS);
    cache.remove(key + '_chunks');
    return;
  }
  var count = Math.ceil(json.length / CACHE_CHUNK_SIZE);
  var parts = {};
  for (var i = 0; i < count; i++) {
    parts[key + '_chunk_' + i] = json.slice(i * CACHE_CHUNK_SIZE, (i + 1) * CACHE_CHUNK_SIZE);
  }
  cache.putAll(parts, CACHE_TTL_SECONDS);
  cache.put(key + '_chunks', String(count), CACHE_TTL_SECONDS);
  cache.remove(key);
}

// Один проход по листу (при промахе обоих кэшей), делит строки на активные и
// завершённые и кэширует каждую половину отдельно.
function splitAndCacheTicketRows_() {
  var sh = sheet_(SHEET_TICKETS);
  var rows = sh.getDataRange().getValues();
  var active = [], done = [];
  for (var i = 1; i < rows.length; i++) {
    if (!rows[i][0]) continue;
    var status = rows[i][7];
    if (status === STATUS.DONE || status === STATUS.REJECTED) done.push(rows[i]);
    else active.push(rows[i]);
  }
  var cache = CacheService.getScriptCache();
  putChunkedJsonCache_(cache, CACHE_KEY_TICKETS_ACTIVE, active);
  putChunkedJsonCache_(cache, CACHE_KEY_TICKETS_DONE, done);
  return { active: active, done: done };
}

function invalidateTicketCache_() {
  var cache = CacheService.getScriptCache();
  removeChunkedJsonCache_(cache, CACHE_KEY_TICKETS_ACTIVE);
  removeChunkedJsonCache_(cache, CACHE_KEY_TICKETS_DONE);
  cache.remove(CACHE_KEY_LEADERBOARD_ALL);
  cache.remove(CACHE_KEY_LEADERBOARD_MONTH);
}

// Обычное действие над заявкой не должно заставлять следующего пользователя
// снова читать весь лист. Если оба списка уже прогреты, точечно переносим/
// обновляем одну строку в кэше. При холодном кэше оставляем прежнее поведение.
function updateTicketCachesAfterWrite_(row) {
  var cache = CacheService.getScriptCache();
  var active = getChunkedJsonCache_(cache, CACHE_KEY_TICKETS_ACTIVE);
  cache.remove(CACHE_KEY_LEADERBOARD_ALL);
  cache.remove(CACHE_KEY_LEADERBOARD_MONTH);
  if (active === null) {
    removeChunkedJsonCache_(cache, CACHE_KEY_TICKETS_ACTIVE);
    removeChunkedJsonCache_(cache, CACHE_KEY_TICKETS_DONE);
    return;
  }
  var number = String(row[0]);
  active = active.filter(function (r) { return String(r[0]) !== number; });
  if (row[7] !== STATUS.DONE && row[7] !== STATUS.REJECTED) {
    active.push(row);
    putChunkedJsonCache_(cache, CACHE_KEY_TICKETS_ACTIVE, active);
    return;
  }
  var done = getChunkedJsonCache_(cache, CACHE_KEY_TICKETS_DONE);
  if (done === null) {
    removeChunkedJsonCache_(cache, CACHE_KEY_TICKETS_ACTIVE);
    removeChunkedJsonCache_(cache, CACHE_KEY_TICKETS_DONE);
    return;
  }
  done = done.filter(function (r) { return String(r[0]) !== number; });
  done.push(row);
  putChunkedJsonCache_(cache, CACHE_KEY_TICKETS_ACTIVE, active);
  putChunkedJsonCache_(cache, CACHE_KEY_TICKETS_DONE, done);
}

function removeChunkedJsonCache_(cache, key) {
  var count = Number(cache.get(key + '_chunks') || 0);
  var keys = [key, key + '_chunks'];
  for (var i = 0; i < count; i++) keys.push(key + '_chunk_' + i);
  cache.removeAll(keys);
}

// ============================ ROW INDEX CACHE (заявки) ============================
// Номер заявки → номер строки. Строки в листе «заявки» никогда не удаляются и
// не переставляются, поэтому индекс стабилен: кэшируем надолго и трогаем
// только при создании новой заявки. Благодаря этому withTicket_ читает ОДНУ
// строку (18 ячеек) вместо всего листа на каждое действие (взять/пауза/
// завершить/…), что и было главным тормозом по мере роста истории заявок.
var CACHE_KEY_ROWMAP = 'ticket_rowmap_v1';
var ROWMAP_TTL_SECONDS = 3600;

function buildRowMap_(sh) {
  var last = sh.getLastRow();
  var map = {};
  if (last >= 2) {
    var col = sh.getRange(2, 1, last - 1, 1).getValues();
    for (var i = 0; i < col.length; i++) {
      var v = col[i][0];
      if (v !== '' && v != null) map[String(v)] = i + 2;
    }
  }
  return map;
}

function getRowMap_(sh) {
  var cache = CacheService.getScriptCache();
  var cached = cache.get(CACHE_KEY_ROWMAP);
  if (cached) { try { return JSON.parse(cached); } catch (e) {} }
  var map = buildRowMap_(sh);
  var json = JSON.stringify(map);
  if (json.length < 95000) cache.put(CACHE_KEY_ROWMAP, json, ROWMAP_TTL_SECONDS);
  return map;
}

function invalidateRowMap_() {
  CacheService.getScriptCache().remove(CACHE_KEY_ROWMAP);
}

function readTicketRow_(sh, rowIdx) {
  var raw = sh.getRange(rowIdx, 1, 1, TICKETS_HEADERS.length).getValues()[0];
  // Нормализуем ширину строки до числа заголовков — на случай лишних
  // столбцов в листе, иначе setValues упадёт на несовпадении диапазона.
  var row = raw.slice(0, TICKETS_HEADERS.length);
  while (row.length < TICKETS_HEADERS.length) row.push('');
  return row;
}

function withTicket_(number, fn) {
  var sh = sheet_(SHEET_TICKETS);
  var num = String(number);
  var map = getRowMap_(sh);
  var rowIdx = map[num];
  var row = rowIdx ? readTicketRow_(sh, rowIdx) : null;
  if (!row || String(row[0]) !== num) {
    // Кэш индекса устарел или заявка только что создана — пересобираем один раз.
    invalidateRowMap_();
    map = getRowMap_(sh);
    rowIdx = map[num];
    row = rowIdx ? readTicketRow_(sh, rowIdx) : null;
  }
  if (!row || String(row[0]) !== num) throw userError_('Заявка ' + number + ' не найдена.');
  return fn(sh, rowIdx, row);
}

function normalizeTicketDateCells_(row) {
  var dateIndexes = [1, 11, 13, 14];
  for (var i = 0; i < dateIndexes.length; i++) {
    var idx = dateIndexes[i];
    if (row[idx] !== '' && row[idx] != null) row[idx] = formatTicketDate_(row[idx]);
  }
  return row;
}

function writeRow_(sh, rowIdx, row) {
  normalizeTicketDateCells_(row);
  sanitizeBridgeTicketRow_(row);
  sh.getRange(rowIdx, 1, 1, TICKETS_HEADERS.length).setValues([row]);
  updateTicketCachesAfterWrite_(row);
}

// Номер строки для новой заявки: сразу после последней строки С НОМЕРОМ (столбец A),
// игнорируя посторонний контент в дальних ячейках других столбцов.
function nextTicketRow_(sh) {
  var last = Math.max(sh.getLastRow(), 1);
  var col = sh.getRange(1, 1, last, 1).getValues();
  for (var i = col.length - 1; i >= 1; i--) {
    if (col[i][0] !== '' && col[i][0] != null) return i + 2;
  }
  return 2;
}

function rowToTicket_(row) {
  var acc = parseDuration_(row[12]);
  var running = row[7] === STATUS.WORK && row[11];
  var elapsed = acc + (running ? elapsedSinceStart_(row[11]) : 0);
  return {
    number: row[0],
    // Даты отдаём фронту всегда как ISO-строки; legacy Date тоже нормализуем —
    // так стабильны сортировка на сервере и разбор на клиенте.
    createdAt: toIso_(row[1]),
    type: row[2],
    city: row[3],
    office: row[4],
    senderName: row[5],
    description: row[6],
    status: row[7],
    creatorId: String(row[8] || ''),
    adminId: String(row[9] || ''),
    adminName: row[10] || '',
    isRunning: !!running,
    elapsedSeconds: elapsed,            // суммарное время в работе, секунды
    idleSeconds: parseDuration_(row[15]), // время не в работе, секунды
    fileUrl: row[16] || '',
    reason: row[17] || '',              // основание доработки/отклонения
    resolvedAt: toIso_(row[13]),
    updatedAt: toIso_(row[14])
  };
}

function nowTicketDate_() {
  return Utilities.formatDate(new Date(), DISPLAY_TZ, SHEET_DATE_TEXT_FORMAT);
}

// Date, ISO или локальный текст таблицы → Date.
function parseTicketDate_(v) {
  if (v == null || v === '') return null;
  if (v instanceof Date) return isNaN(v.getTime()) ? null : new Date(v.getTime());

  var s = String(v).trim();
  var local = s.match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?$/);
  if (local) {
    var pattern = local[6] == null ? 'dd.MM.yyyy HH:mm' : SHEET_DATE_TEXT_FORMAT;
    try {
      var parsedLocal = Utilities.parseDate(s, DISPLAY_TZ, pattern);
      if (!isNaN(parsedLocal.getTime())) return parsedLocal;
    } catch (e) {}
  }

  var parsed = new Date(s);
  return isNaN(parsed.getTime()) ? null : parsed;
}

function formatTicketDate_(v) {
  var parsed = parseTicketDate_(v);
  return parsed ? Utilities.formatDate(parsed, DISPLAY_TZ, SHEET_DATE_TEXT_FORMAT) : String(v || '');
}

// Значение даты из ячейки (Date или строка) → ISO-строка ('' если пусто).
function toIso_(v) {
  if (v == null || v === '') return '';
  var parsed = parseTicketDate_(v);
  return parsed ? parsed.toISOString() : String(v);
}

// Безопасная нормализация для поиска: в таблице часть полей иногда приходит
// не строками (числа/даты из Google Sheets). Прямой `.toLowerCase()` на таких
// значениях валил поиск истории с generic 500, хотя обычная история открывалась.
function searchText_(v) {
  if (v == null || v === '') return '';
  return String(v).trim().toLowerCase();
}

function elapsedSinceStart_(startVal) {
  var d = parseTicketDate_(startVal);
  if (!d) return 0;
  var diff = (new Date().getTime() - d.getTime()) / 1000;
  // Минимум 1 секунда когда таймер был запущен — Math.floor(0.x) = 0
  return diff > 0 ? Math.max(1, Math.floor(diff)) : 0;
}

function byCreatedDesc_(a, b) {
  return String(b.createdAt).localeCompare(String(a.createdAt));
}

function generateNumber_(sh) {
  // Раньше здесь читались ВСЕ 18 колонок листа только чтобы собрать множество
  // занятых номеров — используем уже готовый индекс номер→строка (1 колонка,
  // обычно из кэша).
  var existing = getRowMap_(sh);

  var letters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
  for (var attempt = 0; attempt < 200; attempt++) {
    var l = letters.charAt(Math.floor(Math.random() * letters.length));
    var d = ('000' + Math.floor(Math.random() * 1000)).slice(-3);
    var candidate = l + d;
    if (!existing[candidate]) return candidate;
  }
  // Фолбэк: детерминированный перебор — гарантированно уникальный номер (или явная ошибка).
  for (var li = 0; li < letters.length; li++) {
    for (var ni = 0; ni < 1000; ni++) {
      var cand = letters.charAt(li) + ('000' + ni).slice(-3);
      if (!existing[cand]) return cand;
    }
  }
  throw userError_('Пространство номеров заявок исчерпано — обратитесь к администратору.');
}

function formatDuration_(totalSeconds) {
  var s = Math.max(0, Math.floor(totalSeconds));
  var h = Math.floor(s / 3600);
  var m = Math.floor((s % 3600) / 60);
  var sec = s % 60;
  function pad(n) { return ('0' + n).slice(-2); }
  return pad(h) + ':' + pad(m) + ':' + pad(sec);
}

// "ММ:СС" для таблицы и уведомлений (минут может быть больше 59).
function formatMinSec_(totalSeconds) {
  var s = Math.max(0, Math.floor(totalSeconds || 0));
  function pad(n) { return ('0' + n).slice(-2); }
  return pad(Math.floor(s / 60)) + ':' + pad(s % 60);
}

// Разбор длительности из ячейки: число (старые секунды) или "ММ:СС"/"ЧЧ:ММ:СС".
function parseDuration_(val) {
  if (val === '' || val == null) return 0;
  if (typeof val === 'number') return Math.floor(val);
  var str = String(val).trim();
  if (str.indexOf(':') === -1) return Math.floor(Number(str) || 0);
  var p = str.split(':').map(function (x) { return Number(x) || 0; });
  if (p.length === 3) return p[0] * 3600 + p[1] * 60 + p[2];
  if (p.length === 2) return p[0] * 60 + p[1];
  return 0;
}

// Сохраняет вложение (data URL base64, любой тип) в Google Drive, возвращает ссылку.
// filename — исходное имя файла от клиента (необязательно), используется для имени и расширения.
// Серверный лимит размера: клиентская проверка (20 МБ) обходится прямым POST,
// поэтому ограничиваем и здесь. base64 даёт ~4/3 от размера → 20 МБ ≈ 28e6 символов.
var MAX_ATTACHMENT_B64 = 28000000;

// Опасные типы: рендерятся в браузере (stored-XSS в домене Drive) или исполняемые.
var BLOCKED_MIME = {
  'text/html': 1, 'application/xhtml+xml': 1, 'image/svg+xml': 1,
  'application/javascript': 1, 'text/javascript': 1, 'application/x-msdownload': 1,
  'application/x-msdos-program': 1, 'application/x-sh': 1, 'application/x-httpd-php': 1,
  'application/x-msdownload; format=pe32': 1
};
var BLOCKED_EXT = {
  html: 1, htm: 1, xhtml: 1, shtml: 1, svg: 1, mhtml: 1,
  exe: 1, bat: 1, cmd: 1, com: 1, scr: 1, msi: 1, dll: 1, apk: 1, jar: 1,
  js: 1, jse: 1, mjs: 1, vbs: 1, vbe: 1, ps1: 1, sh: 1, hta: 1, wsf: 1, reg: 1,
  php: 1, phtml: 1, php3: 1, php4: 1, php5: 1, pht: 1
};

function assertFileAllowed_(contentType, name) {
  if (BLOCKED_MIME[String(contentType).toLowerCase().trim()]) {
    throw userError_('Такой тип файла нельзя прикреплять.');
  }
  var ext = (String(name || '').match(/\.([a-z0-9]+)\s*$/i) || [])[1];
  if (ext && BLOCKED_EXT[ext.toLowerCase()]) {
    throw userError_('Такой тип файла нельзя прикреплять.');
  }
}

function saveFile_(dataUrl, ticketNo, filename) {
  var m = String(dataUrl).match(/^data:([^;]*);base64,(.*)$/);
  if (!m) throw userError_('Некорректный файл.');
  if (m[2].length > MAX_ATTACHMENT_B64) throw userError_('Файл слишком большой (макс. 20 МБ).');
  var contentType = m[1] || 'application/octet-stream';
  assertFileAllowed_(contentType, filename);
  var safeName = sanitizeFilename_(filename);
  if (!safeName) safeName = ticketNo + '_' + Date.now() + extFromContentType_(contentType);
  // Имя в Drive: номер заявки + исходное имя — чтобы легко находить вложение по заявке.
  var driveName = ticketNo + '_' + safeName;
  var blob = Utilities.newBlob(Utilities.base64Decode(m[2]), contentType, driveName);
  var file = getAttachmentFolder_().createFile(blob);
  file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
  return file.getUrl();
}

function sanitizeFilename_(name) {
  if (!name) return '';
  // Убираем путь, управляющие/спецсимволы пути и DEL. Также выкидываем bidi-override,
  // zero-width и BOM — иначе через U+202E имя «photo_gpj.exe» может отображаться
  // как «photo_exe.jpg» (подмена расширения для пользователя). Сравниваем по кодам,
  // чтобы не держать невидимые символы в исходнике.
  var base = String(name).replace(/^.*[\\\/]/, '');
  var bad = '<>:"/\\|?*';
  var out = '';
  for (var i = 0; i < base.length; i++) {
    var c = base.charCodeAt(i);
    if (c < 0x20 || c === 0x7f || bad.indexOf(base.charAt(i)) !== -1) { out += '_'; continue; }
    if ((c >= 0x200b && c <= 0x200f) || (c >= 0x202a && c <= 0x202e) ||
        (c >= 0x2060 && c <= 0x2069) || c === 0xfeff) { continue; } // невидимые — выкидываем
    out += base.charAt(i);
  }
  return out.trim().slice(0, 120);
}

function extFromContentType_(contentType) {
  var ct = String(contentType).toLowerCase();
  if (ct.indexOf('png') !== -1) return '.png';
  if (ct.indexOf('webp') !== -1) return '.webp';
  if (ct.indexOf('gif') !== -1) return '.gif';
  if (ct.indexOf('jpeg') !== -1 || ct.indexOf('jpg') !== -1) return '.jpg';
  if (ct.indexOf('pdf') !== -1) return '.pdf';
  if (ct.indexOf('wordprocessingml') !== -1) return '.docx';
  if (ct.indexOf('spreadsheetml') !== -1) return '.xlsx';
  if (ct.indexOf('presentationml') !== -1) return '.pptx';
  if (ct === 'application/msword') return '.doc';
  if (ct === 'application/vnd.ms-excel') return '.xls';
  if (ct.indexOf('zip') !== -1) return '.zip';
  if (ct.indexOf('csv') !== -1) return '.csv';
  if (ct.indexOf('text/plain') !== -1) return '.txt';
  return '.bin';
}

function getAttachmentFolder_() {
  var props = PropertiesService.getScriptProperties();
  var id = props.getProperty('SCREENSHOT_FOLDER_ID'); // ключ исторический, папку не пересоздаём
  if (id) { try { return DriveApp.getFolderById(id); } catch (e) {} }
  var name = 'Ticketsbot — файлы';
  var it = DriveApp.getFoldersByName(name);
  var folder = it.hasNext() ? it.next() : DriveApp.createFolder(name);
  props.setProperty('SCREENSHOT_FOLDER_ID', folder.getId());
  return folder;
}

// Личное сообщение пользователю (работает, только если он запускал бота). true при доставке.
function notifyUser_(chatId, text) {
  if (!BOT_TOKEN || !chatId) return false;
  try {
    var res = UrlFetchApp.fetch('https://api.telegram.org/bot' + BOT_TOKEN + '/sendMessage', {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({ chat_id: String(chatId), text: text }),
      muteHttpExceptions: true,
      deadline: 10  // таймаут 10 сек
    });
    return !!JSON.parse(res.getContentText() || '{}').ok;
  } catch (err) {
    return false;
  }
}

function notify_(text) {
  if (!BOT_TOKEN || !NOTIFY_CHAT_ID) return;
  var payload = { chat_id: NOTIFY_CHAT_ID, text: text };
  if (NOTIFY_THREAD_ID) payload.message_thread_id = Number(NOTIFY_THREAD_ID); // отправка в конкретную тему
  try {
    UrlFetchApp.fetch('https://api.telegram.org/bot' + BOT_TOKEN + '/sendMessage', {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify(payload),
      muteHttpExceptions: true,
      deadline: 10  // таймаут 10 сек — не даём уведомлению подвесить всё выполнение
    });
  } catch (err) {
    // уведомления не должны ломать основную операцию
  }
}

// Личное сообщение + сообщение в группу ОДНИМ параллельным запросом
// (UrlFetchApp.fetchAll), а не двумя последовательными fetch(). Действия вроде
// «завершить»/«отклонить»/«на доработку»/«передать» раньше ждали два похода в
// Telegram API подряд, прежде чем ответить клиенту, — теперь один. Возвращает
// массив булевых (доставлено ли) в том же порядке, что и msgs.
function notifyBatch_(msgs) {
  var reqs = [], flags = [];
  for (var i = 0; i < msgs.length; i++) {
    var m = msgs[i];
    if (!m || !m.chatId || !m.text || !BOT_TOKEN) { flags.push(false); continue; }
    var payload = { chat_id: String(m.chatId), text: m.text };
    if (m.threadId) payload.message_thread_id = Number(m.threadId);
    reqs.push({
      url: 'https://api.telegram.org/bot' + BOT_TOKEN + '/sendMessage',
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify(payload),
      muteHttpExceptions: true,
      deadline: 10  // таймаут 10 сек — не даём уведомлению подвесить всё выполнение
    });
    flags.push(reqs.length - 1); // индекс в reqs для этого сообщения
  }
  if (!reqs.length) return flags.map(function () { return false; });
  var responses;
  try {
    responses = UrlFetchApp.fetchAll(reqs);
  } catch (err) {
    return flags.map(function () { return false; }); // уведомления не должны ломать основную операцию
  }
  return flags.map(function (idx) {
    if (idx === false) return false;
    try { return !!JSON.parse(responses[idx].getContentText() || '{}').ok; } catch (e) { return false; }
  });
}

// ============================ РАЗОВЫЕ ВЫГРУЗКИ (ручной запуск) ============================
// Выбери эту функцию в выпадающем списке редактора → ▶ Выполнить → результат
// смотри в «Журнал выполнения» (или Ctrl+Enter → View → Executions).
function debugRestrictedTypeAccess() {
  return debugTicketTypeAccess_('Перемещение');
}

// Кто видит выбранный тип заявки согласно флажкам в листе «роли».
function debugTicketTypeAccess_(type) {
  type = String(type || '').trim();
  if (TICKET_TYPES.indexOf(type) === -1) throw userError_('Неизвестный тип заявки: ' + type);
  var rows = readRoleRows_();
  var withAccess = [];
  rows.forEach(function (r) {
    if (allowedTicketTypesForRoleRow_(r).indexOf(type) !== -1 && r[1]) {
      withAccess.push({ tg_id: String(r[0]), name: r[1], username: r[3] || '', role: r[2] });
    }
  });
  Logger.log(JSON.stringify({ type: type, withAccess: withAccess }, null, 2));
  return { type: type, withAccess: withAccess };
}
