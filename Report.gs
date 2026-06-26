/**
 * Сводный отчёт по заявкам в листе «Отчет» (таблицы + графики + фильтр периода).
 * =============================================================================
 * ВАЖНО: добавь этот файл в ТОТ ЖЕ проект Apps Script, где лежит Code.gs
 * (использует getSpreadsheet_, SHEET_TICKETS, parseDuration_, formatDuration_).
 *
 * Запуск: функция buildReport (или меню «📊 Отчёт» в самой таблице).
 *
 * Фильтр периода (по дате создания заявки): ячейки C3 (с) и E3 (по) в листе «Отчет».
 *   Пусто = за всё время. Можно задать месяц, конкретный день (с=по) или диапазон.
 *   Быстрые периоды — через меню «📊 Отчёт».
 *
 * Секции:
 *   1) Заявки по офисам          — столбчатая
 *   2) Заявки по типам           — круговая
 *   3) Время по администраторам  — суммарное и СРЕДНЕЕ время на заявку
 *   4) «Перемещение» по городам  — общее число + столбчатая
 */

var REPORT_SHEET = 'Отчет';

function buildReport() {
  var ss = getSpreadsheet_();
  var sh = ss.getSheetByName(REPORT_SHEET);
  if (!sh) sh = ss.insertSheet(REPORT_SHEET);

  var period = readPeriod_(sh); // читаем фильтр ДО очистки

  sh.getCharts().forEach(function (c) { sh.removeChart(c); });
  sh.clear();
  sh.setColumnWidth(1, 200).setColumnWidth(2, 120).setColumnWidth(3, 140)
    .setColumnWidth(4, 150).setColumnWidth(5, 90);

  // ---- агрегация с фильтром по дате создания ----
  var rows = ss.getSheetByName(SHEET_TICKETS).getDataRange().getValues();
  var byOffice = {}, byType = {}, adminSec = {}, adminCnt = {}, moveCity = {}, moveTotal = 0, total = 0;
  for (var i = 1; i < rows.length; i++) {
    var r = rows[i];
    if (!r[0]) continue;
    if (!inPeriod_(r[1], period)) continue;
    total++;
    var type = r[2], city = r[3], office = r[4], admin = r[10], dur = parseDuration_(r[12]);
    if (office) byOffice[office] = (byOffice[office] || 0) + 1;
    if (type) byType[type] = (byType[type] || 0) + 1;
    if (admin) { adminSec[admin] = (adminSec[admin] || 0) + dur; adminCnt[admin] = (adminCnt[admin] || 0) + 1; }
    if (type === 'Перемещение') { moveTotal++; if (city) moveCity[city] = (moveCity[city] || 0) + 1; }
  }

  // ---- шапка + панель фильтра ----
  sh.getRange(1, 1).setValue('СВОДНЫЙ ОТЧЁТ ПО ЗАЯВКАМ').setFontSize(16).setFontWeight('bold');
  sh.getRange(2, 1).setValue('Обновлено: ' + fmtDateTime_() + '   ·   Период: ' + period.label +
    '   ·   Всего заявок: ' + total).setFontColor('#666666');
  writeFilter_(sh, period);

  var top = 6;

  top = section_R_(sh, top, '1. Заявки по офисам', ['Офис', 'Заявок'],
    pairsDesc_R_(byOffice), Charts.ChartType.COLUMN);

  top = section_R_(sh, top, '2. Заявки по типам', ['Тип', 'Заявок'],
    pairsDesc_R_(byType), Charts.ChartType.PIE);

  // 3) Администраторы: суммарное + среднее на заявку
  var adminRows = Object.keys(adminSec).map(function (k) {
    var sec = adminSec[k], cnt = adminCnt[k] || 1;
    return [k, Math.round(sec / 60 * 10) / 10, formatDuration_(sec),
            formatDuration_(Math.round(sec / cnt)), adminCnt[k]];
  }).sort(function (a, b) { return b[1] - a[1]; });
  top = section_R_(sh, top, '3. Время по администраторам',
    ['Администратор', 'Время, мин', 'Суммарное (Ч:ММ:СС)', 'Среднее на заявку', 'Заявок'],
    adminRows, Charts.ChartType.COLUMN);

  // 4) Перемещение по городам
  sh.getRange(top, 1).setValue('4. Заявки «Перемещение» по городам').setFontWeight('bold').setFontSize(13);
  sh.getRange(top, 4).setValue('Всего «Перемещение»: ' + moveTotal).setFontWeight('bold').setFontColor('#2b6fff');
  top = section_R_(sh, top + 1, '', ['Город', 'Заявок'], pairsDesc_R_(moveCity), Charts.ChartType.COLUMN);

  SpreadsheetApp.flush();
  try { ss.toast('Период: ' + period.label + ' · Всего: ' + total, '📊 Отчёт обновлён', 5); } catch (e) {}
  Logger.log('Отчёт обновлён. Период: ' + period.label + ', всего: ' + total);
  return 'OK';
}

// ===================== Период / фильтр =====================

function readPeriod_(sh) {
  var f = sh.getRange('C3').getValue();
  var t = sh.getRange('E3').getValue();
  var from = (f instanceof Date) ? new Date(f.getFullYear(), f.getMonth(), f.getDate(), 0, 0, 0) : null;
  var to = (t instanceof Date) ? new Date(t.getFullYear(), t.getMonth(), t.getDate(), 23, 59, 59) : null;
  return makePeriod_(from, to);
}

function makePeriod_(from, to) {
  var label = (!from && !to) ? 'за всё время'
    : (from && to) ? ('с ' + fmtDate_(from) + ' по ' + fmtDate_(to))
    : (from ? ('с ' + fmtDate_(from)) : ('по ' + fmtDate_(to)));
  return { from: from, to: to, label: label };
}

function inPeriod_(createdRaw, period) {
  if (!period.from && !period.to) return true;
  if (!createdRaw) return false;
  var d = (createdRaw instanceof Date) ? createdRaw : new Date(createdRaw);
  if (isNaN(d.getTime())) return false;
  if (period.from && d < period.from) return false;
  if (period.to && d > period.to) return false;
  return true;
}

function writeFilter_(sh, period) {
  sh.getRange('A3').setValue('Фильтр по дате создания:').setFontWeight('bold');
  sh.getRange('B3').setValue('с:').setFontWeight('bold').setHorizontalAlignment('right');
  sh.getRange('D3').setValue('по:').setFontWeight('bold').setHorizontalAlignment('right');
  var c = sh.getRange('C3'), e = sh.getRange('E3');
  c.setNumberFormat('dd.MM.yyyy'); e.setNumberFormat('dd.MM.yyyy');
  c.setBackground('#fff8e1'); e.setBackground('#fff8e1');
  if (period.from) c.setValue(period.from); else c.clearContent();
  if (period.to) e.setValue(period.to); else e.clearContent();
  sh.getRange('F3').setValue('← впишите даты и нажмите «📊 Отчёт → Обновить». Пусто = за всё время.')
    .setFontColor('#999999');
}

// Быстрые периоды из меню
function reportThisMonth() { var n = new Date(); setPeriod_(new Date(n.getFullYear(), n.getMonth(), 1), new Date(n.getFullYear(), n.getMonth() + 1, 0)); }
function reportLastMonth() { var n = new Date(); setPeriod_(new Date(n.getFullYear(), n.getMonth() - 1, 1), new Date(n.getFullYear(), n.getMonth(), 0)); }
function reportAllTime() { setPeriod_(null, null); }

function setPeriod_(from, to) {
  var ss = getSpreadsheet_();
  var sh = ss.getSheetByName(REPORT_SHEET);
  if (!sh) sh = ss.insertSheet(REPORT_SHEET);
  var c = sh.getRange('C3'), e = sh.getRange('E3');
  c.setNumberFormat('dd.MM.yyyy'); e.setNumberFormat('dd.MM.yyyy');
  if (from) c.setValue(from); else c.clearContent();
  if (to) e.setValue(to); else e.clearContent();
  buildReport();
}

// ===================== Отрисовка секции =====================

function section_R_(sh, top, title, headers, dataRows, chartType) {
  var headerRow = top;
  if (title) { sh.getRange(top, 1).setValue(title).setFontWeight('bold').setFontSize(13); headerRow = top + 1; }

  sh.getRange(headerRow, 1, 1, headers.length).setValues([headers])
    .setFontWeight('bold').setBackground('#eef1f5');
  var n = dataRows.length;
  if (n) sh.getRange(headerRow + 1, 1, n, headers.length).setValues(dataRows);

  if (n) {
    var range = sh.getRange(headerRow, 1, n + 1, 2); // категория + значение (первые 2 столбца)
    var chart = sh.newChart()
      .setChartType(chartType)
      .addRange(range)
      .setNumHeaders(1)
      .setPosition(headerRow, 6, 0, 0)
      .setOption('title', title || 'Перемещение по городам')
      .setOption('width', 460)
      .setOption('height', 300)
      .setOption('legend', { position: chartType === Charts.ChartType.PIE ? 'right' : 'none' })
      .build();
    sh.insertChart(chart);
  }
  return Math.max(headerRow + n + 2, top + 16) + 1;
}

function pairsDesc_R_(obj) {
  return Object.keys(obj).map(function (k) { return [k, obj[k]]; })
    .sort(function (a, b) { return b[1] - a[1]; });
}

// ===================== Утилиты дат =====================

function fmtDateTime_() { return Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'dd.MM.yyyy HH:mm'); }
function fmtDate_(d) { return Utilities.formatDate(d, Session.getScriptTimeZone(), 'dd.MM.yyyy'); }

// ===================== Автообновление по расписанию =====================

function scheduleReportEvery6h() {
  removeReportTriggers();
  ScriptApp.newTrigger('buildReport').timeBased().everyHours(6).create();
  return 'OK: отчёт будет обновляться каждые 6 часов.';
}

function scheduleReportDaily8() {
  removeReportTriggers();
  ScriptApp.newTrigger('buildReport').timeBased().everyDays(1).atHour(8).create();
  return 'OK: отчёт будет обновляться ежедневно около 8:00.';
}

function removeReportTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'buildReport') ScriptApp.deleteTrigger(t);
  });
  return 'OK: триггеры обновления отчёта удалены.';
}

// ===================== Меню в таблице =====================

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('📊 Отчёт')
    .addItem('🔄 Обновить (по фильтру)', 'buildReport')
    .addSeparator()
    .addItem('Период: текущий месяц', 'reportThisMonth')
    .addItem('Период: прошлый месяц', 'reportLastMonth')
    .addItem('Период: за всё время', 'reportAllTime')
    .addSeparator()
    .addItem('Автообновление: каждые 6 часов', 'scheduleReportEvery6h')
    .addItem('Автообновление: ежедневно в 8:00', 'scheduleReportDaily8')
    .addItem('Выключить автообновление', 'removeReportTriggers')
    .addToUi();
}
