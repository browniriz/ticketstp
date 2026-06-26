/**
 * Генератор инструкции для сотрудников в виде красиво оформленного Google Документа.
 * =================================================================================
 * Как использовать:
 *   1. В редакторе Apps Script нажми «+» рядом с «Файлы» → «Скрипт» → назови «Guide».
 *      (или вставь в любой проект Apps Script — код самодостаточный)
 *   2. Вставь этот код.
 *   3. Выбери функцию createEmployeeGuide → Выполнить.
 *   4. При первом запуске разреши доступ (Google Docs/Drive).
 *   5. Ссылка на готовый документ появится в «Журнале выполнения» (Ctrl+Enter),
 *      а сам документ — в корне твоего Google Диска. Его можно редактировать и делиться.
 */

// Цвета (читаемые на белом фоне)
var C_RED = '#d23f3f';
var C_AMBER = '#c8860b';
var C_GREEN = '#1f9d57';
var C_ACCENT = '#2b6fff';
var C_VIOLET = '#6f5cff';  // на доработке
var C_CYAN = '#1f8fa8';    // исправлена
var C_GRAY = '#6b7280';    // отклонена
var BG_NOTE = '#fff7e6';   // жёлтая выноска
var BG_TIP = '#eef4ff';    // голубая выноска

function createEmployeeGuide() {
  var doc = DocumentApp.create('Заявки для ТП — инструкция для сотрудников');
  var body = doc.getBody();
  body.setMarginTop(48).setMarginBottom(48).setMarginLeft(56).setMarginRight(56);

  body.appendParagraph('«Заявки для ТП»').setHeading(DocumentApp.ParagraphHeading.TITLE);
  body.appendParagraph('Инструкция для сотрудников — как отправить заявку и отслеживать её статус')
      .setHeading(DocumentApp.ParagraphHeading.SUBTITLE);

  lead_(body, 'Что это. ',
    'Через бота вы отправляете заявки на исправление проблем (Ломбард, Скупка, Касса, ' +
    'Ошибка, Перемещение). Специалисты техподдержки видят заявку сразу, берут её в работу ' +
    'и решают. Вы в любой момент видите статус своей заявки.');

  // 1
  h2_(body, '1. Как открыть');
  numbered_(body, [
    'В Telegram откройте бота @ticketstpbot и нажмите «Старт».',
    'Внизу нажмите синюю кнопку меню «Заявки» — откроется приложение.'
  ]);
  noteBox_(body, BG_NOTE, C_AMBER,
    '⚠ Важно: работает только в официальном приложении Telegram (на телефоне или ПК). ' +
    'В сторонних клиентах (например, TELEGA) приложение не сможет вас определить и работать не будет.');

  // 2
  h2_(body, '2. Как создать заявку');
  body.appendParagraph('Откроется вкладка «Новая заявка». Заполните поля сверху вниз:');
  fieldList_(body, [
    ['Тип заявки', ' — выберите из списка: Ломбард / Скупка / Касса / Ошибка / Перемещение.'],
    ['Город', ' — выберите ваш город.'],
    ['Офис', ' — список офисов появится после выбора города.'],
    ['Имя отправителя', ' — обычно подставляется автоматически; при необходимости поправьте.'],
    ['Описание проблемы', ' — опишите подробно: что произошло, в каком офисе, номер договора/чека, что уже пробовали.'],
    ['Файл (необязательно)', ' — нажмите «Прикрепить файл» и выберите фото, скрин ошибки или документ (любой тип файла).']
  ]);
  body.appendParagraph('Когда все обязательные поля заполнены, кнопка «Отправить заявку» станет ' +
    'активной — нажмите её. Заявке присваивается номер (например, C734), и она сразу уходит специалистам.');
  noteBox_(body, BG_TIP, C_ACCENT, '💡 Прикреплённый файл (скриншот ошибки или документ) заметно ускоряет решение.');

  // 3
  h2_(body, '3. Мои заявки и статусы');
  body.appendParagraph('Вкладка «Мои заявки» — список всех ваших заявок с цветовой меткой статуса. ' +
    'Нажмите на карточку, чтобы посмотреть подробности.');
  statusItem_(body, 'Создана', C_RED, ' — отправлена, ещё не взята в работу.');
  statusItem_(body, 'В работе', C_AMBER, ' — специалист занимается заявкой (идёт таймер).');
  statusItem_(body, 'На паузе', C_AMBER, ' — работа временно приостановлена.');
  statusItem_(body, 'Отправлен на доработку', C_VIOLET, ' — специалист вернул заявку вам: нужно исправить данные (см. раздел 4).');
  statusItem_(body, 'Исправлена', C_CYAN, ' — вы внесли правки и вернули заявку; ждёт повторного взятия в работу.');
  statusItem_(body, 'Решена', C_GREEN, ' — проблема устранена.');
  statusItem_(body, 'Заявка отклонена', C_GRAY, ' — специалист отклонил заявку с указанием причины.');

  // 4
  h2_(body, '4. Если заявку вернули на доработку');
  body.appendParagraph('Если в заявке нашли несоответствие или недостоверные данные (в тексте или в выбранном ' +
    'типе), специалист вернёт её вам со статусом «Отправлен на доработку» и укажет причину.');
  numbered_(body, [
    'Откройте заявку во вкладке «Мои заявки» — вверху будет видна причина доработки.',
    'Исправьте нужные поля (тип, город, офис, имя, описание; при необходимости замените файл).',
    'Нажмите «Отправить снова» — заявка вернётся специалистам со статусом «Исправлена».'
  ]);
  noteBox_(body, BG_TIP, C_ACCENT, '💡 Время, уже потраченное специалистом до доработки, сохраняется — ' +
    'после исправления таймер продолжится с того же места.');

  // 5
  h2_(body, '5. Полезно знать');
  bullets_(body, [
    'Не создавайте дубли одной и той же проблемы — лучше дополните существующую заявку деталями.',
    'Указывайте конкретику: номера договоров/чеков, время, офис.',
    'Прикреплённый файл (скриншот ошибки или документ) сильно ускоряет решение.',
    'Статусы обновляются автоматически — обновлять вручную не нужно.'
  ]);

  // 6
  h2_(body, '6. Если что-то не работает');
  bullets_(body, [
    'Не открывается или пишет «Не удалось определить профиль» — откройте бота в официальном Telegram.',
    'Кнопка «Отправить заявку» серая — не заполнено обязательное поле (тип, город, офис, имя или описание).',
    'Не вижу свою заявку в «Мои заявки» — переоткройте вкладку или потяните список вниз.'
  ]);

  body.appendParagraph('По вопросам работы бота — обращайтесь к администратору техподдержки.')
      .editAsText().setForegroundColor('#6b7280');

  doc.saveAndClose();
  Logger.log('Готово! Документ: ' + doc.getUrl());
  return doc.getUrl();
}

// ----------------------------- хелперы -----------------------------

function h2_(body, text) {
  return body.appendParagraph(text).setHeading(DocumentApp.ParagraphHeading.HEADING2);
}

function lead_(body, lead, rest) {
  var p = body.appendParagraph(lead + rest);
  p.editAsText().setBold(0, lead.length - 1, true);
  return p;
}

function numbered_(body, items) {
  items.forEach(function (it) {
    body.appendListItem(it).setGlyphType(DocumentApp.GlyphType.NUMBER);
  });
}

function bullets_(body, items) {
  items.forEach(function (it) {
    body.appendListItem(it).setGlyphType(DocumentApp.GlyphType.BULLET);
  });
}

function fieldList_(body, pairs) {
  pairs.forEach(function (pr) {
    var li = body.appendListItem(pr[0] + pr[1]).setGlyphType(DocumentApp.GlyphType.NUMBER);
    li.editAsText().setBold(0, pr[0].length - 1, true);
  });
}

function statusItem_(body, name, color, rest) {
  var li = body.appendListItem(name + rest).setGlyphType(DocumentApp.GlyphType.BULLET);
  var t = li.editAsText();
  t.setBold(0, name.length - 1, true);
  t.setForegroundColor(0, name.length - 1, color);
  return li;
}

// Цветная выноска (1 ячейка-таблица с фоном и рамкой акцентного цвета).
function noteBox_(body, bg, accent, text) {
  var table = body.appendTable([[text]]);
  table.setBorderColor(accent).setBorderWidth(1);
  var cell = table.getCell(0, 0);
  cell.setBackgroundColor(bg);
  cell.setPaddingTop(8).setPaddingBottom(8).setPaddingLeft(12).setPaddingRight(12);
  return table;
}
