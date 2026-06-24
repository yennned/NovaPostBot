/**
 * «Внести»: синк черновика приёмки → книга «Склад» (additive) + лог «Історія».
 *
 * Контейнер-bound к книге «Приймання». Книга «Склад» берётся по ID из
 * Script Property STOCK_BOOK_ID (Project Settings → Script Properties).
 *
 * Поток (docs/04-warehouse-sheets.md): «Внести» → сводка «что прибавится» →
 * Підтвердити/Змінити. На «Підтвердити»: Склад[артикул] += кількість (новый
 * артикул → новая строка) + запись в «Історія», затем черновик очищается.
 *
 * Колонки «Приймання»: Дата · Артикул · Назва · Категорія · Кількість · Ціна ·
 * Накладна · Стан · Оброблено. Берём строки со Стан != «брак» (годне/пусто).
 */

var STOCK_HEADERS = ['Артикул', 'Назва', 'Категорія', 'Кількість', 'Ціна'];
var HISTORY_TAB = 'Історія';
var HISTORY_HEADERS = ['Час', 'Лист (клієнт)', 'Артикул', 'Кількість +', 'Накладна', 'Хто'];

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('📦 Склад')
    .addItem('Внести приймання → Склад', 'submitIntake')
    .addToUi();
}

function getStockBook_() {
  var id = PropertiesService.getScriptProperties().getProperty('STOCK_BOOK_ID');
  if (!id) {
    throw new Error('Не задано STOCK_BOOK_ID у Script Properties.');
  }
  return SpreadsheetApp.openById(id);
}

/** Прочитать черновик активного листа приёмки: агрегаты по артикулу. */
function readDraft_(sheet) {
  var values = sheet.getDataRange().getValues();
  if (values.length < 2) return { items: [], rows: 0 };
  var head = values[0].map(function (h) { return String(h).trim().toLowerCase(); });
  var ci = {
    sku: head.indexOf('артикул'),
    name: head.indexOf('назва'),
    cat: head.indexOf('категорія'),
    qty: head.indexOf('кількість'),
    price: head.indexOf('ціна'),
    state: head.indexOf('стан')
  };
  if (ci.sku < 0 || ci.qty < 0) {
    throw new Error('У листі немає колонок Артикул/Кількість.');
  }
  var agg = {};
  var counted = 0;
  for (var r = 1; r < values.length; r++) {
    var row = values[r];
    var sku = String(row[ci.sku] || '').trim();
    var qty = Number(String(row[ci.qty] || '0').replace(',', '.'));
    var state = ci.state >= 0 ? String(row[ci.state] || '').trim().toLowerCase() : '';
    if (!sku || !qty || qty <= 0) continue;
    if (state === 'брак') continue; // брак у склад не йде
    if (!agg[sku]) {
      agg[sku] = {
        sku: sku, qty: 0,
        name: ci.name >= 0 ? String(row[ci.name] || '').trim() : '',
        cat: ci.cat >= 0 ? String(row[ci.cat] || '').trim() : '',
        price: ci.price >= 0 ? String(row[ci.price] || '').trim() : ''
      };
    }
    agg[sku].qty += qty;
    counted++;
  }
  return { items: Object.keys(agg).map(function (k) { return agg[k]; }), rows: counted };
}

function submitIntake() {
  var ui = SpreadsheetApp.getUi();
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  var tab = sheet.getName();

  var draft = readDraft_(sheet);
  if (draft.items.length === 0) {
    ui.alert('Немає рядків для внесення (порожньо або тільки брак).');
    return;
  }

  // Сводка «что прибавится» → Підтвердити / Змінити.
  var lines = draft.items.map(function (it) {
    return '• ' + it.sku + ' (' + (it.name || '—') + '): +' + it.qty;
  });
  var resp = ui.alert(
    'Внести у «Склад» → лист «' + tab + '»',
    lines.join('\n') + '\n\nПідтвердити внесення?',
    ui.ButtonSet.YES_NO // YES = Підтвердити, NO = Змінити (повернутися до редагування)
  );
  if (resp !== ui.Button.YES) {
    return; // «Змінити»: нічого не пишемо, клієнт править чернетку і тисне знову
  }

  applyToStock_(tab, draft.items);
  clearDraft_(sheet);
  ui.alert('Внесено: ' + draft.items.length + ' позицій у лист «' + tab + '».');
}

function applyToStock_(tab, items) {
  var book = getStockBook_();
  var ws = book.getSheetByName(tab);
  if (!ws) {
    ws = book.insertSheet(tab);
    ws.appendRow(STOCK_HEADERS);
    ws.setFrozenRows(1);
  }
  var values = ws.getDataRange().getValues();
  var head = values[0].map(function (h) { return String(h).trim().toLowerCase(); });
  var skuCol = head.indexOf('артикул');
  var qtyCol = head.indexOf('кількість');
  if (skuCol < 0 || qtyCol < 0) throw new Error('У «Склад» немає колонок Артикул/Кількість.');

  var rowBySku = {};
  for (var r = 1; r < values.length; r++) {
    var s = String(values[r][skuCol] || '').trim();
    if (s) rowBySku[s] = r + 1; // 1-based
  }

  var history = ensureHistory_(book);
  var who = Session.getActiveUser().getEmail() || 'apps-script';
  var now = new Date();

  items.forEach(function (it) {
    if (rowBySku[it.sku]) {
      var cell = ws.getRange(rowBySku[it.sku], qtyCol + 1);
      var before = Number(String(cell.getValue() || '0').replace(',', '.'));
      cell.setValue(before + it.qty);
    } else {
      ws.appendRow([it.sku, it.name || it.sku, it.cat || '', it.qty, it.price || '']);
    }
    history.appendRow([now, tab, it.sku, it.qty, '', who]);
  });
}

function ensureHistory_(book) {
  var h = book.getSheetByName(HISTORY_TAB);
  if (!h) {
    h = book.insertSheet(HISTORY_TAB);
    h.appendRow(HISTORY_HEADERS);
    h.setFrozenRows(1);
  }
  return h;
}

/** Очистить черновик до пустого листа (шапку оставляем). */
function clearDraft_(sheet) {
  var last = sheet.getLastRow();
  if (last > 1) {
    sheet.getRange(2, 1, last - 1, sheet.getLastColumn()).clearContent();
  }
}
