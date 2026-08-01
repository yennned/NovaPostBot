/**
 * «Приймання» → «Склад»: умный ввод + перенос остатков (additive) + лог «Історія».
 *
 * Контейнер-bound к книге «Приймання». Книга «Склад» берётся по ID из
 * Script Property STOCK_BOOK_ID (Project Settings → Script Properties).
 *
 * Возможности:
 *  1) Кнопка меню «Внести оброблені → Склад»: берёт только строки с галочкой
 *     «Оброблено» (и Стан ≠ «брак»), показывает сводку + подтверждение, аддитивно
 *     прибавляет к «Склад», пишет в «Історія» и очищает именно перенесённые строки.
 *  2) Живой справочник из «Склад» (скрытая вкладка `_Довідник`), обновляется
 *     пунктом меню «🔄 Оновити довідник». Базовые дропдауны Артикул/Назва/Категорія
 *     на каждой вкладке клиента строятся из него.
 *  3) onEdit-помощник: выбор Категорії сужает список Назв; выбор Назви/Артикула
 *     автоподставляет остальные поля (Артикул/Назва + Категорія + Ціна); первая
 *     правка строки авто-ставит сегодняшнюю Дату (формат dd.MM.yyyy).
 *
 * Колонки «Приймання»: Дата · Артикул · Назва · Категорія · Кількість · Ціна ·
 * Накладна · Стан · Оброблено. Брак у склад не идёт.
 */

var STOCK_HEADERS = ['Артикул', 'Назва', 'Категорія', 'Кількість', 'Ціна'];
var HISTORY_TAB = 'Історія';
var HISTORY_HEADERS = ['Час', 'Лист (клієнт)', 'Артикул', 'Кількість +', 'Накладна', 'Хто'];
var REF_TAB = '_Довідник';
var REF_HEADERS = ['Лист', 'Артикул', 'Назва', 'Категорія', 'Ціна'];

// ───────────────────────────── меню ─────────────────────────────

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('📦 Склад')
    .addItem('Внести оброблені → Склад', 'submitProcessed')
    .addItem('Внести всі (годні) → Склад', 'submitAll')
    .addSeparator()
    .addItem('🔄 Оновити довідник (зі «Склад»)', 'refreshRef')
    .addToUi();
  // Авто-обновление справочника здесь НЕЛЬЗЯ: onOpen — simple trigger без права
  // открывать другую книгу. Обновление — только по пункту меню (полная авторизация).
}

function getStockBook_() {
  var id = PropertiesService.getScriptProperties().getProperty('STOCK_BOOK_ID');
  if (!id) {
    throw new Error('Не задано STOCK_BOOK_ID у Script Properties (Project Settings → Script Properties).');
  }
  return SpreadsheetApp.openById(id);
}

// ─────────────────── индексы колонок листа приёмки ───────────────────

/** Карта 1-based индексов колонок по заголовкам активного листа приёмки. */
function intakeCols_(sheet) {
  var head = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0]
    .map(function (h) { return String(h).trim().toLowerCase(); });
  function idx(n) { return head.indexOf(n) + 1; } // 0 → колонки нет
  return {
    date: idx('дата'), sku: idx('артикул'), name: idx('назва'), cat: idx('категорія'),
    qty: idx('кількість'), price: idx('ціна'), ttn: idx('накладна'),
    state: idx('стан'), done: idx('оброблено')
  };
}

function isServiceTab_(name) {
  return !name || name.charAt(0) === '_' || name === HISTORY_TAB;
}

// ───────────────── живой справочник из «Склад» ─────────────────

/**
 * Пересобрать `_Довідник` из книги «Склад» и переустановить базовые дропдауны
 * (Артикул/Назва/Категорія) на каждой вкладке клиента.
 * Запускается пунктом меню (полная авторизация — открывает книгу «Склад»).
 */
function refreshRef() {
  var book = SpreadsheetApp.getActiveSpreadsheet();
  var stock = getStockBook_();

  var ref = book.getSheetByName(REF_TAB);
  if (!ref) { ref = book.insertSheet(REF_TAB); ref.hideSheet(); }
  ref.clear();
  ref.appendRow(REF_HEADERS);
  ref.setFrozenRows(1);

  var intakeTabs = book.getSheets()
    .map(function (s) { return s.getName(); })
    .filter(function (n) { return !isServiceTab_(n); });

  var rows = [];
  var perTab = {}; // tab -> {names, skus, cats}

  intakeTabs.forEach(function (tab) {
    var ws = stock.getSheetByName(tab);
    if (!ws) return; // нет вкладки в «Склад» — справочник для неё пуст
    var vals = ws.getDataRange().getValues();
    if (vals.length < 2) return;
    var head = vals[0].map(function (h) { return String(h).trim().toLowerCase(); });
    var iS = head.indexOf('артикул'), iN = head.indexOf('назва'), iC = head.indexOf('категорія');
    var iP = head.indexOf('ціна');
    var acc = { names: [], skus: [], cats: [], sn: {}, ss: {}, sc: {} };
    perTab[tab] = acc;
    for (var r = 1; r < vals.length; r++) {
      var sku = iS >= 0 ? String(vals[r][iS] || '').trim() : '';
      var nm = iN >= 0 ? String(vals[r][iN] || '').trim() : '';
      var cat = iC >= 0 ? String(vals[r][iC] || '').trim() : '';
      var price = iP >= 0 ? vals[r][iP] : ''; // сырое значение (число/текст) — как в «Склад»
      if (!sku && !nm) continue;
      rows.push([tab, sku, nm, cat, price]);
      if (nm && !acc.sn[nm]) { acc.names.push(nm); acc.sn[nm] = 1; }
      if (sku && !acc.ss[sku]) { acc.skus.push(sku); acc.ss[sku] = 1; }
      if (cat && !acc.sc[cat]) { acc.cats.push(cat); acc.sc[cat] = 1; }
    }
  });

  if (rows.length) ref.getRange(2, 1, rows.length, REF_HEADERS.length).setValues(rows);

  // базовые (полные) дропдауны + формат «Дата» на каждой вкладке клиента
  intakeTabs.forEach(function (tab) {
    var acc = perTab[tab];
    if (!acc) return;
    var sheet = book.getSheetByName(tab);
    var cols = intakeCols_(sheet);
    var lastRow = Math.max(sheet.getMaxRows(), 2);
    if (cols.sku) applyListValidation_(sheet.getRange(2, cols.sku, lastRow - 1, 1), acc.skus);
    if (cols.name) applyListValidation_(sheet.getRange(2, cols.name, lastRow - 1, 1), acc.names);
    if (cols.cat) applyListValidation_(sheet.getRange(2, cols.cat, lastRow - 1, 1), acc.cats);
    if (cols.date) {
      // формат ДД.ММ.РРРР + календарь-пикер (авто-дата ставится в onEdit)
      var dRange = sheet.getRange(2, cols.date, lastRow - 1, 1);
      dRange.setNumberFormat('dd.MM.yyyy');
      dRange.setDataValidation(
        SpreadsheetApp.newDataValidation().requireDate().setAllowInvalid(true).build()
      );
    }
  });

  SpreadsheetApp.getActiveSpreadsheet()
    .toast('Довідник оновлено: ' + rows.length + ' позицій.', '📦 Склад', 5);
}

/** Справочник для конкретной вкладки клиента (для onEdit). Читает `_Довідник`. */
function getRefFor_(tab) {
  var out = { byName: {}, bySku: {}, catToNames: {}, names: [], skus: [], cats: [] };
  var ref = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(REF_TAB);
  if (!ref) return out;
  var vals = ref.getDataRange().getValues();
  var sn = {}, ss = {}, sc = {}, snc = {};
  for (var i = 1; i < vals.length; i++) {
    if (String(vals[i][0] || '').trim() !== tab) continue;
    var sku = String(vals[i][1] || '').trim();
    var nm = String(vals[i][2] || '').trim();
    var cat = String(vals[i][3] || '').trim();
    var price = vals[i].length > 4 ? vals[i][4] : ''; // сырое значение цены
    if (nm) {
      out.byName[nm.toLowerCase()] = { sku: sku, name: nm, cat: cat, price: price };
      if (!sn[nm]) { out.names.push(nm); sn[nm] = 1; }
      if (cat) {
        if (!out.catToNames[cat]) out.catToNames[cat] = [];
        if (!snc[cat + '|' + nm]) { out.catToNames[cat].push(nm); snc[cat + '|' + nm] = 1; }
      }
    }
    if (sku) {
      out.bySku[sku.toLowerCase()] = { sku: sku, name: nm, cat: cat, price: price };
      if (!ss[sku]) { out.skus.push(sku); ss[sku] = 1; }
    }
    if (cat && !sc[cat]) { out.cats.push(cat); sc[cat] = 1; }
  }
  return out;
}

/** Поставить на ячейку/диапазон дропдаун из списка (нестрогий — допускает «Новий товар»). */
function applyListValidation_(rangeOrCell, list) {
  if (!list || !list.length) { rangeOrCell.clearDataValidations(); return; }
  var rule = SpreadsheetApp.newDataValidation()
    .requireValueInList(list, true) // showDropdown = true
    .setAllowInvalid(true)          // strict=false → можно ввести новое значение
    .build();
  rangeOrCell.setDataValidation(rule);
}

// ─────────────────────── onEdit: умный ввод ───────────────────────

/**
 * Simple trigger. Работает только внутри книги «Приймання» (читает `_Довідник`,
 * пишет значения/валидации в тот же файл) — прав хватает, установка installable
 * триггера не нужна. Книгу «Склад» здесь НЕ открываем (это делает refreshRef).
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

    var cols = intakeCols_(sheet);
    var col = range.getColumn();
    var val = e.value != null ? String(e.value).trim() : '';

    // (6) авто-дата: первая правка строки и Дата пуста → сегодня
    if (cols.date && (col === cols.sku || col === cols.name || col === cols.qty) && val) {
      var dcell = sheet.getRange(row, cols.date);
      if (dcell.getValue() === '') dcell.setValue(new Date());
    }

    var ref = getRefFor_(tab);

    // (2) категория → сузить список назв в этой строке
    if (col === cols.cat && cols.name) {
      var nameCell = sheet.getRange(row, cols.name);
      if (val && ref.catToNames[val]) applyListValidation_(nameCell, ref.catToNames[val]);
      else applyListValidation_(nameCell, ref.names);
      return;
    }

    // (3) назва → автоподстановка артикула + категории + цены
    if (col === cols.name && val) {
      var byN = ref.byName[val.toLowerCase()];
      if (byN) {
        if (cols.sku && byN.sku) sheet.getRange(row, cols.sku).setValue(byN.sku);
        if (cols.cat && byN.cat) sheet.getRange(row, cols.cat).setValue(byN.cat);
        if (cols.price && byN.price !== '' && byN.price != null) {
          sheet.getRange(row, cols.price).setValue(byN.price);
        }
      }
      return;
    }

    // (4) артикул → автоподстановка назви + категории + цены
    if (col === cols.sku && val) {
      var byS = ref.bySku[val.toLowerCase()];
      if (byS) {
        if (cols.name && byS.name) sheet.getRange(row, cols.name).setValue(byS.name);
        if (cols.cat && byS.cat) sheet.getRange(row, cols.cat).setValue(byS.cat);
        if (cols.price && byS.price !== '' && byS.price != null) {
          sheet.getRange(row, cols.price).setValue(byS.price);
        }
      }
      return;
    }
  } catch (err) {
    // onEdit не должен ронять UI — только лог.
    console.error(err && err.stack ? err.stack : err);
  }
}

// ─────────────────── перенос черновика в «Склад» ───────────────────

function submitProcessed() { submitIntake_(true); }
function submitAll() { submitIntake_(false); }

/**
 * @param {boolean} onlyProcessed  true — только строки с галочкой «Оброблено».
 */
function submitIntake_(onlyProcessed) {
  var ui = SpreadsheetApp.getUi();
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  var tab = sheet.getName();
  if (isServiceTab_(tab)) { ui.alert('Оберіть лист клієнта (не службовий).'); return; }

  var draft = readDraft_(sheet, onlyProcessed);
  if (draft.items.length === 0) {
    ui.alert(onlyProcessed
      ? 'Немає відмічених «Оброблено» рядків (годних) для внесення.'
      : 'Немає рядків для внесення (порожньо або тільки брак).');
    return;
  }

  var lines = draft.items.map(function (it) {
    return '• ' + it.sku + ' (' + (it.name || '—') + '): +' + it.qty;
  });
  var resp = ui.alert(
    'Внести у «Склад» → лист «' + tab + '»',
    lines.join('\n') + '\n\nПідтвердити внесення?',
    ui.ButtonSet.YES_NO // YES = Підтвердити, NO = Змінити (повернутися до редагування)
  );
  if (resp !== ui.Button.YES) return;

  applyToStock_(tab, draft.items); // бросит понятную ошибку, если листа в «Склад» нет
  clearRows_(sheet, draft.srcRows); // очистить только перенесённые строки
  ui.alert('Внесено: ' + draft.items.length + ' позицій у лист «' + tab + '».');
}

/**
 * Прочитать черновик активного листа: агрегаты по артикулу + исходные строки.
 * @param {boolean} onlyProcessed  учитывать только строки с «Оброблено»=TRUE.
 * @return {{items:Array, rows:number, srcRows:number[]}}
 */
function readDraft_(sheet, onlyProcessed) {
  var values = sheet.getDataRange().getValues();
  if (values.length < 2) return { items: [], rows: 0, srcRows: [] };
  var head = values[0].map(function (h) { return String(h).trim().toLowerCase(); });
  var ci = {
    sku: head.indexOf('артикул'), name: head.indexOf('назва'), cat: head.indexOf('категорія'),
    qty: head.indexOf('кількість'), price: head.indexOf('ціна'),
    state: head.indexOf('стан'), done: head.indexOf('оброблено')
  };
  if (ci.sku < 0 || ci.qty < 0) throw new Error('У листі немає колонок Артикул/Кількість.');

  var agg = {};
  var srcRows = {};
  var counted = 0;
  for (var r = 1; r < values.length; r++) {
    var row = values[r];
    var sku = String(row[ci.sku] || '').trim();
    var qty = Number(String(row[ci.qty] || '0').replace(',', '.'));
    var state = ci.state >= 0 ? String(row[ci.state] || '').trim().toLowerCase() : '';
    var done = ci.done >= 0 ? row[ci.done] === true : false;
    if (!sku || !qty || qty <= 0) continue;
    if (state === 'брак') continue;        // брак у склад не йде
    if (onlyProcessed && !done) continue;   // тільки відмічені «Оброблено»
    if (!agg[sku]) {
      agg[sku] = {
        sku: sku, qty: 0,
        name: ci.name >= 0 ? String(row[ci.name] || '').trim() : '',
        cat: ci.cat >= 0 ? String(row[ci.cat] || '').trim() : '',
        price: ci.price >= 0 ? String(row[ci.price] || '').trim() : ''
      };
      srcRows[sku] = [];
    }
    agg[sku].qty += qty;
    srcRows[sku].push(r + 1); // 1-based номер исходной строки
    counted++;
  }

  var allRows = [];
  Object.keys(srcRows).forEach(function (k) {
    srcRows[k].forEach(function (rn) { allRows.push(rn); });
  });
  return {
    items: Object.keys(agg).map(function (k) { return agg[k]; }),
    rows: counted,
    srcRows: allRows
  };
}

function applyToStock_(tab, items) {
  var book = getStockBook_();
  var ws = book.getSheetByName(tab);
  if (!ws) {
    // НЕ создаём молча — иначе остаток «уходит» в новую вкладку и клиент его не видит.
    throw new Error(
      'У книзі «Склад» немає листа «' + tab + '». Імена листів «Приймання» і «Склад» ' +
      'мають збігатися. Внесення скасовано — виправте назву листа і повторіть.'
    );
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

/** Удалить перенесённые строки (снизу вверх, чтобы индексы не сползали). */
function clearRows_(sheet, rowNums) {
  if (!rowNums || !rowNums.length) return;
  var uniq = rowNums.slice().sort(function (a, b) { return b - a; });
  var seen = {};
  uniq.forEach(function (rn) {
    if (seen[rn]) return;
    seen[rn] = 1;
    sheet.deleteRow(rn);
  });
}
