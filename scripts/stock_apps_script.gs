/**
 * Книга «Склад»: журнал ручных правок «Кількість» + валидация ввода.
 *
 * Контейнер-bound к книге «Склад» (не к «Приймання» — там свой скрипт,
 * `scripts/intake_apps_script.gs`). Никаких других книг не открывает, поэтому
 * хватает простого триггера `onEdit` — устанавливать installable не нужно.
 *
 * ЗАЧЕМ ЭТО ЕСТЬ. Колонкой «Кількість» владеет Postgres, но правка ячейки руками
 * остаётся рабочим способом коррекции: зеркало (`app/services/stock_mirror.py`)
 * отличает её от собственного отставания по `mirrored_quantity` и применяет
 * движением `manual`. Чего зеркало не может узнать — КТО правил и КОГДА: в лист
 * оно приходит через 5 минут и видит только новое число. Автор терялся.
 * Этот скрипт пишет автора в лист-журнал `_Правки`, а бот забирает его оттуда
 * ровно в тот цикл, когда правку заметил (`StockSheetMirror.read_edit_authors`).
 *
 * ПОЧЕМУ ЖУРНАЛ НЕ ЗАПОЛНЯЕТСЯ МУСОРОМ. Apps Script не вызывает `onEdit` на
 * изменения, сделанные скриптом или через Sheets API. Значит ни записи зеркала
 * (service-account, ~1600 ячеек за цикл), ни перенос приёмки из «Приймання» сюда
 * не попадают — только правка человека мышкой. Это же снимает вопрос рекурсии
 * при откате неверного значения ниже.
 *
 * Установка — руками, и это не лень. Apps Script API не работает с сервис-аккаунтами
 * (документировано Google), а в нашем GCP-проекте он к тому же не включён вовсе
 * (проверено: 403 SERVICE_DISABLED). Значит выкатить скрипт из репозитория нельзя
 * ничем — ни `clasp`, ни своим кодом; нужен живой владелец книги.
 *   1. Книга «Склад» → Extensions → Apps Script → вставить этот файл.
 *   2. Настройки проекта → часовой пояс Europe/Kyiv (или скопировать
 *      `scripts/appsscript.json` в манифест проекта).
 *   3. Сохранить. Первая правка «Кількість» попросит авторизацию — разрешить.
 *   4. Меню «📦 Склад» → «⏱ Прибирати щодня автоматично» — один раз.
 *   5. Проверить: поправить количество → в `_Правки` появилась строка.
 */

var EDITS_TAB = '_Правки';
var EDITS_HEADERS = ['Час', 'Лист', 'Артикул', 'Було', 'Стало', 'Хто'];
var HISTORY_TAB = 'Історія';
var EDITS_KEEP_DAYS = 60;
var HISTORY_KEEP_DAYS = 60;
//: Сколько последних строк «Історія» не трогать НИКОГДА, как бы стары они ни были.
//: Водораздел ингеста — это последняя перенесённая строка журнала; удали её, и бот
//: перестанет понимать, докуда дошёл. Обычно она у самого конца (ингест ходит раз в
//: минуту), но если он стоял сутки — уедет вглубь. Хвост и есть этот запас.
var HISTORY_KEEP_TAIL = 50;

// ───────────────────────────── меню ─────────────────────────────

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('📦 Склад')
    .addItem('🧾 Журнал правок', 'showEdits')
    // Срок в подписи собирается из константы, а не вписан числом: подпись и порог
    // обязаны меняться одним движением. Иначе меню обещает одно, а кнопка делает
    // другое — и заметить это можно только пересчитав удалённые строки руками.
    .addItem('🧹 Прибрати правки, старші за ' + EDITS_KEEP_DAYS + ' днів', 'trimEdits')
    .addItem(
      '📦 Прибрати історію приймання, старішу за ' + HISTORY_KEEP_DAYS + ' днів',
      'trimHistory'
    )
    .addItem('⏱ Прибирати щодня автоматично', 'enableDailyTrim')
    .addToUi();
}

/**
 * Поставить суточный триггер на `trimEdits` — один раз, руками из меню.
 *
 * Из `onOpen` это сделать нельзя: простой триггер работает без авторизации и
 * `ScriptApp.newTrigger` ему недоступен. Без этого пункта журнал не чистится
 * никогда: `trimEdits` висел только на ручном пункте меню, а бот читает лист
 * целиком — то есть каждая правка навсегда удорожала все последующие чтения.
 */
function enableDailyTrim() {
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === 'trimEdits') {
      SpreadsheetApp.getActive().toast('Щоденне прибирання вже увімкнено.', '📦 Склад', 5);
      return;
    }
  }
  ScriptApp.newTrigger('trimEdits').timeBased().everyDays(1).atHour(4).create();
  SpreadsheetApp.getActive().toast('Готово: прибирання щодня о 4-й ранку.', '📦 Склад', 5);
}

function showEdits() {
  var sheet = ensureEdits_(SpreadsheetApp.getActive());
  sheet.showSheet();
  sheet.activate();
}

/**
 * Журнал нужен боту только «свежий»: он ищет автора правки, которую заметил в
 * этом же цикле зеркала (5 минут). Старые строки — это история для человека, и
 * держать их вечно значит платить за них при каждом чтении.
 */
function trimEdits() {
  var sheet = ensureEdits_(SpreadsheetApp.getActive());
  var last = sheet.getLastRow();
  if (last < 2) return;
  var times = sheet.getRange(2, 1, last - 1, 1).getValues();
  var edge = new Date().getTime() - EDITS_KEEP_DAYS * 24 * 60 * 60 * 1000;
  var keepFrom = 0;
  for (var i = 0; i < times.length; i++) {
    var when = times[i][0] instanceof Date ? times[i][0].getTime() : 0;
    if (when >= edge) { keepFrom = i; break; }
    keepFrom = i + 1;
  }
  if (keepFrom > 0) sheet.deleteRows(2, keepFrom);
  SpreadsheetApp.getActive().toast('Прибрано рядків: ' + keepFrom, '📦 Склад', 5);
}

/**
 * Прибрать старые строки «Історія» — легально и безопасно.
 *
 * ЗАЧЕМ ОТДЕЛЬНАЯ КНОПКА. Журнал растёт вечно, и рано или поздно в него лезут
 * руками. Раньше это останавливало ингест: он держит водораздел по НОМЕРУ строки,
 * а удаление сдвигает все номера ниже. Теперь бот умеет найти свой водораздел по
 * отпечатку и переставить номер сам (`app/services/stock_ingest.py`), поэтому
 * удаление сверху безопасно — но только сверху и только заведомо старого.
 *
 * ДВЕ ГРАНИЦЫ, И ОБЕ НУЖНЫ. Возраст (`HISTORY_KEEP_DAYS`) отсекает то, что ингест давно
 * перенёс: он ходит раз в минуту. Хвост (`HISTORY_KEEP_TAIL`) страхует от случая,
 * когда ингест стоял долго и водораздел уехал вглубь: удалить саму строку-
 * водораздел значит вернуть ровно ту остановку, от которой мы уходим.
 */
function trimHistory() {
  var ui = SpreadsheetApp.getUi();
  var sheet = SpreadsheetApp.getActive().getSheetByName(HISTORY_TAB);
  if (!sheet) { ui.alert('Листа «' + HISTORY_TAB + '» ще немає.'); return; }

  var last = sheet.getLastRow();
  var total = last - 1; // без шапки
  if (total <= HISTORY_KEEP_TAIL) {
    ui.alert('Прибирати нічого: у журналі ' + Math.max(total, 0) + ' рядків.');
    return;
  }

  var times = sheet.getRange(2, 1, total, 1).getValues();
  var edge = new Date().getTime() - HISTORY_KEEP_DAYS * 24 * 60 * 60 * 1000;
  var old = 0;
  for (var i = 0; i < times.length; i++) {
    var when = times[i][0] instanceof Date ? times[i][0].getTime() : 0;
    if (when >= edge) break;   // журнал append-only → дальше только свежее
    old = i + 1;
  }
  // Хвост неприкосновенен, каким бы старым он ни был.
  var removable = Math.min(old, total - HISTORY_KEEP_TAIL);
  if (removable <= 0) {
    ui.alert('Прибирати нічого: усе старе входить в останні ' + HISTORY_KEEP_TAIL + ' рядків.');
    return;
  }

  var resp = ui.alert(
    'Прибрати історію приймання',
    'Видалити ' + removable + ' найстаріших рядків (старіші за ' + HISTORY_KEEP_DAYS +
      ' днів)?\nЗалишиться: ' + (total - removable) + '.\n\n' +
      'Залишок у боті це не змінює — рядки давно перенесені.',
    ui.ButtonSet.YES_NO
  );
  if (resp !== ui.Button.YES) return;

  sheet.deleteRows(2, removable);
  ui.alert('Прибрано рядків: ' + removable + '.');
}

// ─────────────────────────── служебное ───────────────────────────

function isServiceTab_(name) {
  return !name || name.charAt(0) === '_' || name === HISTORY_TAB;
}

/** Карта 1-based индексов колонок листа склада по заголовкам. */
function stockCols_(sheet) {
  var head = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0]
    .map(function (h) { return String(h).trim().toLowerCase(); });
  function idx(n) { return head.indexOf(n) + 1; } // 0 → колонки нет
  return { sku: idx('артикул'), qty: idx('кількість') };
}

function ensureEdits_(book) {
  var sheet = book.getSheetByName(EDITS_TAB);
  if (!sheet) {
    sheet = book.insertSheet(EDITS_TAB);
    sheet.appendRow(EDITS_HEADERS);
    sheet.setFrozenRows(1);
    sheet.hideSheet();
  }
  return sheet;
}

/**
 * Кто правит.
 *
 * ЧЕСТНО ОБ ОГРАНИЧЕНИИ. Наши книги живут на личных Gmail, а не в Workspace-домене.
 * В простом триггере Google отдаёт `getActiveUser().getEmail()` только владельцу
 * скрипта; для всех остальных редакторов — пустую строку, и обойти это со стороны
 * скрипта нельзя. То есть на нашей конфигурации автор появится у правок владельца
 * книги, а у правок Степана и менеджеров будет «—».
 *
 * Скрипт от этого не бесполезен: он валидирует ввод (мусор и отрицательные не
 * доезжают до бота), откатывает неверное значение и пишет в журнал время, лист,
 * артикул и «было → стало». Автор — приятное дополнение, а не смысл.
 */
function editorEmail_(e) {
  try {
    if (e && e.user && e.user.getEmail) {
      var fromEvent = e.user.getEmail();
      if (fromEvent) return fromEvent;
    }
    return Session.getActiveUser().getEmail() || '—';
  } catch (err) {
    return '—';
  }
}

// ───────────────────────────── триггер ─────────────────────────────

/**
 * Simple trigger: срабатывает только на правку человека в UI. Изменения скрипта
 * и Sheets API (то есть все записи бота) сюда не приходят — это гарантия Apps
 * Script, и именно на ней стоит и чистота журнала, и отсутствие рекурсии при
 * откате неверного значения.
 */
function onEdit(e) {
  try {
    if (!e || !e.range) return;
    var range = e.range;
    if (range.getNumRows() !== 1 || range.getNumColumns() !== 1) return; // только точечная правка
    var sheet = range.getSheet();
    var tab = sheet.getName();
    if (isServiceTab_(tab)) return;
    var row = range.getRow();
    if (row < 2) return; // шапка

    var cols = stockCols_(sheet);
    if (!cols.qty || range.getColumn() !== cols.qty) return;

    var was = e.oldValue == null ? '' : String(e.oldValue).trim();
    var now = e.value == null ? '' : String(e.value).trim();
    if (was === now) return;

    // Валидация до журнала: неверное значение не должно ни попасть в лист, ни
    // оставить след «правки», которой не будет. Бот такое число тоже отвергнет
    // (`_edit_verdict`), но узнает об этом лишь через цикл зеркала, а человек —
    // сразу и на месте.
    if (!isWholeNonNegative_(now)) {
      range.setValue(e.oldValue == null ? '' : e.oldValue);
      SpreadsheetApp.getActive().toast(
        'Кількість — ціле число ≥ 0. Значення повернуто.', '📦 Склад', 6);
      return;
    }

    var sku = cols.sku ? String(sheet.getRange(row, cols.sku).getValue()).trim() : '';
    if (!sku) return; // строка без артикула боту не адресуется

    ensureEdits_(SpreadsheetApp.getActive())
      .appendRow([new Date(), tab, sku, was, now, editorEmail_(e)]);
    range.setNote('Правка: ' + (was || '—') + ' → ' + now +
      '\n' + editorEmail_(e) + ', ' + Utilities.formatDate(
        new Date(), Session.getScriptTimeZone(), 'dd.MM.yyyy HH:mm'));
  } catch (err) {
    // onEdit не должен ронять UI — только лог.
    console.error(err && err.stack ? err.stack : err);
  }
}

function isWholeNonNegative_(raw) {
  if (raw === '') return true; // очистка ячейки = 0, это законная правка
  var text = String(raw).replace(/\s/g, '').replace(',', '.');
  if (!/^\d+(\.0+)?$/.test(text)) return false;
  return Number(text) >= 0;
}
