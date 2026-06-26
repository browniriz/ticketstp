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

function scriptProp_(key) {
  return PropertiesService.getScriptProperties().getProperty(key) || '';
}

var BOT_TOKEN = scriptProp_('BOT_TOKEN');
var SPREADSHEET_ID = scriptProp_('SPREADSHEET_ID');
var NOTIFY_CHAT_ID = scriptProp_('NOTIFY_CHAT_ID');
var NOTIFY_THREAD_ID = scriptProp_('NOTIFY_THREAD_ID');
var BOOTSTRAP_ADMIN_IDS = scriptProp_('BOOTSTRAP_ADMIN_IDS');
var BOOTSTRAP_ADMIN_NAME = scriptProp_('BOOTSTRAP_ADMIN_NAME') || 'Администратор';

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
    BOOTSTRAP_ADMIN_NAME: ''  // напр. Администратор
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

var ROLES_HEADERS = ['tg_id', 'имя', 'роль', 'username', 'photo_url']; // роль: сотрудник | админ
var REQUESTS_HEADERS = ['tg_id', 'имя', 'дата_запроса', 'username', 'photo_url']; // ожидающие одобрения доступа

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
var CACHE_TTL_SECONDS = 20;
var CACHE_KEY_TICKETS = 'ticket_rows_v1';
var CACHE_KEY_ROLES = 'role_rows_v1';

// ============================ ROUTING ============================

function doGet(e) {
  return json_({ success: true, data: { pong: true } });
}

function doPost(e) {
  try {
    var body = (e && e.postData && e.postData.contents) ? JSON.parse(e.postData.contents) : {};
    var action = body.action;
    var data;

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
// ВАЖНО: freshness по auth_date НЕ проверяем — Mini App переиспользует один initData
// весь сеанс (включая опрос каждые 4с), и короткое окно ломало бы длинные сессии.
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
  ensureSheet_(ss, SHEET_ROLES, ROLES_HEADERS);
  var ts = ensureSheet_(ss, SHEET_TICKETS, TICKETS_HEADERS);
  formatTicketColumns_(ts);
  migrateTicketDates_(ts); // существующие ISO-строки → настоящие даты
  CacheService.getScriptCache().remove(CACHE_KEY_TICKETS);
  ensureSheet_(ss, SHEET_REQUESTS, REQUESTS_HEADERS);
  getAttachmentFolder_(); // создаёт папку в Drive и запрашивает доступ при первом запуске

  if (BOOTSTRAP_ADMIN_IDS) {
    var ids = String(BOOTSTRAP_ADMIN_IDS).split(',');
    for (var i = 0; i < ids.length; i++) {
      var id = ids[i].trim();
      if (id) upsertRole_(id, BOOTSTRAP_ADMIN_NAME, 'админ');
    }
  }
  return 'OK: листы созданы, админ(ы) добавлены.';
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
  // Всегда синхронизируем строку заголовков: это и создаёт их с нуля, и
  // мигрирует при переименовании/добавлении столбцов (данные строк не трогаются).
  sh.getRange(1, 1, 1, headers.length).setValues([headers]);
  sh.setFrozenRows(1);
  sh.getRange(1, 1, 1, headers.length).setFontWeight('bold');
  return sh;
}

// Часовой пояс и формат отображения дат в таблице.
var DISPLAY_TZ = 'Asia/Yekaterinburg'; // UTC+5 (Пермь/Екатеринбург/Челябинск/Магнитогорск)
var SHEET_DATE_FORMAT = 'dd.MM.yyyy HH:mm';

// Даты теперь хранятся как НАСТОЯЩИЕ даты (формат дд.ММ.гггг чч:мм, местное время),
// а длительности/ссылки/текст — как «обычный текст».
// Колонки с датами и длительностями — формат «обычный текст», иначе Google Sheets
// сам превращает "2026-..Z" и "ММ:СС" в дату/время и ломает расчёт таймера.
function formatTicketColumns_(sh) {
  var rows = sh.getMaxRows();
  // Колонки-даты (1-based): дата_создания(2), work_started_at(12), дата_решения(14), последнее_изменение(15)
  var dateCols = [2, 12, 14, 15];
  // Текстовые: Затраченное время(13), Время не в работе(16), Файл(17), основание(18)
  var textCols = [13, 16, 17, 18];
  for (var i = 0; i < dateCols.length; i++) {
    sh.getRange(1, dateCols[i], rows, 1).setNumberFormat(SHEET_DATE_FORMAT);
  }
  for (var j = 0; j < textCols.length; j++) {
    sh.getRange(1, textCols[j], rows, 1).setNumberFormat('@');
  }
}

// Разовая миграция: существующие ISO-строки в датных колонках → настоящие Date,
// чтобы отображались как дд.ММ.гггг чч:мм в местном времени. Запускается из setup().
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
      if (typeof v === 'string' && v) {
        var d = new Date(v);
        if (!isNaN(d.getTime())) { vals[i][0] = d; changed = true; }
      }
    }
    if (changed) rng.setValues(vals);
  }
}

// ============================ ROLES =============================

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
      return { role: rows[i][2] || 'сотрудник', name: rows[i][1] || '' };
    }
  }
  // не внесён в «роли» → доступа нет; сообщаем, отправлял ли уже запрос
  return { role: 'гость', name: '', pending: hasPendingRequest_(tgId) };
}

function isAuthorized_(tgId) {
  var role = getRole_({ tg_id: tgId }).role;
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
      // [tg_id, имя, роль, username, photo_url]
      data.push([String(rows[i][0]), rows[i][1], rows[i][2], rows[i][3] || '', rows[i][4] || '']);
    }
  }
  cache.put(CACHE_KEY_ROLES, JSON.stringify(data), CACHE_TTL_SECONDS);
  return data;
}

function isAdmin_(tgId) {
  return getRole_({ tg_id: tgId }).role === 'админ';
}

function upsertRole_(tgId, name, role, username, photo) {
  var sh = sheet_(SHEET_ROLES);
  var rows = sh.getDataRange().getValues();
  for (var i = 1; i < rows.length; i++) {
    if (String(rows[i][0]) === String(tgId)) {
      // Контакты не затираем пустыми — сохраняем прежние, если новые не переданы.
      var u = (username != null && username !== '') ? username : (rows[i][3] || '');
      var p = (photo != null && photo !== '') ? photo : (rows[i][4] || '');
      sh.getRange(i + 1, 1, 1, 5).setValues([[tgId, name, role, u, p]]);
      CacheService.getScriptCache().remove(CACHE_KEY_ROLES);
      return;
    }
  }
  sh.appendRow([tgId, name, role, username || '', photo || '']);
  CacheService.getScriptCache().remove(CACHE_KEY_ROLES);
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
  var sh = sheet_(SHEET_TICKETS);
  var number = generateNumber_(sh);
  var now = new Date();
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
  // Пишем сразу после последней строки С НОМЕРОМ (столбец A), а не после
  // getLastRow() — иначе посторонний контент в дальних ячейках уводит запись вниз.
  sh.getRange(nextTicketRow_(sh), 1, 1, row.length).setValues([row]);
  CacheService.getScriptCache().remove(CACHE_KEY_TICKETS);

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
  var all = readTickets_().filter(function (t) {
    return t.status !== STATUS.DONE && t.status !== STATUS.REJECTED;
  });
  all.sort(byCreatedDesc_);
  return { tickets: all };
}

function getHistory_(body) {
  requireAdmin_(body);
  // История = терминальные статусы: решена и отклонена.
  var done = readTickets_().filter(function (t) {
    return t.status === STATUS.DONE || t.status === STATUS.REJECTED;
  });
  done.sort(function (a, b) {
    return String(b.resolvedAt).localeCompare(String(a.resolvedAt));
  });
  return { tickets: done };
}

function takeTicket_(body) {
  requireAdmin_(body);
  return withTicket_(body.number, function (sh, rowIdx, row) {
    // Взять можно только новую («создана») или возвращённую сотрудником («исправлена»).
    // Для «исправлена» накопленное время в кол.12 сохраняется → таймер продолжит с него.
    if (row[7] !== STATUS.NEW && row[7] !== STATUS.FIXED) {
      throw userError_('Заявку нельзя взять в работу в текущем статусе.');
    }
    var now = new Date();
    // «Время не в работе»: от создания до первого взятия (заполняется один раз).
    if (!row[15] && row[1]) {
      var idleSec = Math.max(0, Math.floor((new Date(now).getTime() - new Date(row[1]).getTime()) / 1000));
      row[15] = formatMinSec_(idleSec);
    }
    row[7] = STATUS.WORK;
    row[9] = String(body.tg_id || '');
    row[10] = String(body.name || getRole_({ tg_id: body.tg_id }).name || '');
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
    row[14] = new Date();
    writeRow_(sh, rowIdx, row);
    return { ticket: rowToTicket_(row) };
  });
}

function resumeTicket_(body) {
  requireAdmin_(body);
  return withTicket_(body.number, function (sh, rowIdx, row) {
    if (row[7] !== STATUS.PAUSE) throw userError_('Заявку нельзя возобновить.');
    row[7] = STATUS.WORK;
    row[11] = new Date();
    row[14] = row[11];
    writeRow_(sh, rowIdx, row);
    notify_('🟡 Заявка ' + row[0] + ' снова в работе');
    return { ticket: rowToTicket_(row) };
  });
}

function finishTicket_(body) {
  requireAdmin_(body);
  return withTicket_(body.number, function (sh, rowIdx, row) {
    if (row[7] !== STATUS.WORK && row[7] !== STATUS.PAUSE) {
      throw userError_('Завершить можно только заявку в работе или на паузе.');
    }
    var acc = parseDuration_(row[12]) + elapsedSinceStart_(row[11]);
    var now = new Date();
    row[7] = STATUS.DONE;
    row[11] = '';
    row[12] = formatMinSec_(acc);
    row[13] = now;            // дата_решения
    row[14] = now;
    writeRow_(sh, rowIdx, row);
    notify_('🟢 Заявка ' + row[0] + ' решена\nЗатрачено: ' + formatMinSec_(acc) +
      (row[10] ? '\nИсполнитель: ' + row[10] : ''));
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
    row[14] = new Date();
    row[17] = reason;
    writeRow_(sh, rowIdx, row);
    notifyUser_(row[8], '✏️ Заявка ' + row[0] + ' возвращена на доработку.\nОснование: ' + reason +
      '\nОткройте приложение, исправьте данные и отправьте заявку снова.');
    notify_('✏️ Заявка ' + row[0] + ' отправлена на доработку (' + (row[10] || '—') + ')\nОснование: ' + reason);
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
    var now = new Date();
    row[7] = STATUS.REJECTED;
    row[9] = String(body.tg_id || row[9] || '');
    row[10] = String(body.name || getRole_({ tg_id: body.tg_id }).name || row[10] || '');
    row[11] = '';
    row[12] = formatMinSec_(acc);
    row[13] = now;                // дата_решения (для истории)
    row[14] = now;
    row[17] = reason;
    writeRow_(sh, rowIdx, row);
    notifyUser_(row[8], '🚫 Заявка ' + row[0] + ' отклонена.\nОснование: ' + reason);
    notify_('🚫 Заявка ' + row[0] + ' отклонена (' + (row[10] || '—') + ')\nОснование: ' + reason);
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
    row[14] = new Date();
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
  var to = getRole_({ tg_id: toId });
  if (to.role !== 'админ') throw userError_('Получатель не является администратором.');
  return withTicket_(body.number, function (sh, rowIdx, row) {
    if (row[7] !== STATUS.WORK && row[7] !== STATUS.PAUSE) {
      throw userError_('Передать можно только заявку в работе или на паузе.');
    }
    var fromName = String(body.name || getRole_({ tg_id: body.tg_id }).name || row[10] || '');
    row[9] = toId;
    row[10] = to.name || '';
    row[14] = new Date();
    writeRow_(sh, rowIdx, row);
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
    row[14] = new Date();
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

// Для админской вкладки: ожидающие запросы + сотрудники с доступом.
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
  return { requests: requests, employees: employees };
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
  var role = getRole_({ tg_id: tgId });
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
  removeRole_(tgId);
  removeRequest_(tgId);
  notifyUser_(tgId, '⛔ Доступ к боту закрыт администратором.');
  return { ok: true };
}

// ---- работа с листом «роли» (удаление) и «запросы» ----

function removeRole_(tgId) {
  var sh = sheet_(SHEET_ROLES);
  var rows = sh.getDataRange().getValues();
  for (var i = rows.length - 1; i >= 1; i--) {
    if (String(rows[i][0]) === String(tgId)) sh.deleteRow(i + 1);
  }
  CacheService.getScriptCache().remove(CACHE_KEY_ROLES);
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

// Строки листа "заявки" (без шапки) с кэшем.
function readTicketDataRows_() {
  var cache = CacheService.getScriptCache();
  var cached = cache.get(CACHE_KEY_TICKETS);
  if (cached) { try { return JSON.parse(cached); } catch (e) {} }
  var sh = sheet_(SHEET_TICKETS);
  var rows = sh.getDataRange().getValues();
  var data = [];
  for (var i = 1; i < rows.length; i++) {
    if (rows[i][0]) data.push(rows[i]);
  }
  var json = JSON.stringify(data);
  // CacheService ограничивает значение 100 КБ — крупные объёмы не кэшируем.
  if (json.length < 95000) cache.put(CACHE_KEY_TICKETS, json, CACHE_TTL_SECONDS);
  return data;
}

function withTicket_(number, fn) {
  var sh = sheet_(SHEET_TICKETS);
  var rows = sh.getDataRange().getValues();
  for (var i = 1; i < rows.length; i++) {
    if (String(rows[i][0]) === String(number)) {
      // Нормализуем ширину строки до числа заголовков — на случай лишних
      // столбцов в листе, иначе setValues упадёт на несовпадении диапазона.
      var row = rows[i].slice(0, TICKETS_HEADERS.length);
      while (row.length < TICKETS_HEADERS.length) row.push('');
      return fn(sh, i + 1, row);
    }
  }
  throw userError_('Заявка ' + number + ' не найдена.');
}

function writeRow_(sh, rowIdx, row) {
  sh.getRange(rowIdx, 1, 1, TICKETS_HEADERS.length).setValues([row]);
  CacheService.getScriptCache().remove(CACHE_KEY_TICKETS);
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
    // Даты отдаём фронту всегда как ISO-строку (в ячейке теперь хранится Date) —
    // так стабильна сортировка на сервере и разбор на клиенте.
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

// Значение даты из ячейки (Date или строка) → ISO-строка ('' если пусто).
function toIso_(v) {
  if (v == null || v === '') return '';
  if (v instanceof Date) return v.toISOString();
  return String(v);
}

function elapsedSinceStart_(startIso) {
  if (!startIso) return 0;
  var diff = (new Date().getTime() - new Date(startIso).getTime()) / 1000;
  return diff > 0 ? Math.floor(diff) : 0;
}

function byCreatedDesc_(a, b) {
  return String(b.createdAt).localeCompare(String(a.createdAt));
}

function generateNumber_(sh) {
  var existing = {};
  var rows = sh.getDataRange().getValues();
  for (var i = 1; i < rows.length; i++) existing[String(rows[i][0])] = true;

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
      muteHttpExceptions: true
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
      muteHttpExceptions: true
    });
  } catch (err) {
    // уведомления не должны ломать основную операцию
  }
}
