#!/usr/bin/env python
"""Провижининг Google Sheets под склад: книги «Склад» и «Приймання».

Создаёт (или дозаполняет) две книги так, чтобы их структура совпадала с тем, что
читает/пишет бот (см. app/sheets/, docs/04-warehouse-sheets.md):

  «Склад»    — source of truth остатков, бот ЧИТАЕТ и списывает. Лист на клиента,
               имя листа = ПІБ клиента (stock_sheet_key). Колонки:
               Артикул · Назва · Категорія · Кількість · Ціна.
  «Приймання»— Apps-Script-документ (бот не читает). Лист на клиента. Колонки:
               Дата · Артикул · Назва · Категорія · Кількість · Ціна · Накладна ·
               Стан · Оброблено.

Запуск (из корня репо, service-account.json уже в ./secrets/):

    PYTHONPATH=. .venv/bin/python scripts/provision_sheets.py \
        --share you@gmail.com [--clients "Тест Клієнт,Демо"] [--dry-run]

Идемпотентность: если SHEETS_STOCK_BOOK_ID / SHEETS_INTAKE_BOOK_ID заданы в .env —
книги открываются по ключу и недостающие листы дозаполняются; иначе книги
создаются с нуля и их ID печатаются для вставки в .env.
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal, InvalidOperation
from typing import Any

import gspread
from app.config import get_settings
from app.db.base import get_sessionmaker
from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User
from google.oauth2.service_account import Credentials
from sqlalchemy import select

# Канонический порядок колонок — строго как append/чтение в app/sheets/inventory.py.
STOCK_HEADERS = ["Артикул", "Назва", "Категорія", "Кількість", "Ціна"]
INTAKE_HEADERS = [
    "Дата",
    "Артикул",
    "Назва",
    "Категорія",
    "Кількість",
    "Ціна",
    "Накладна",
    "Стан",
    "Оброблено",
]
TEMPLATE_TAB = "_TEMPLATE"  # листы-образцы (скрыты), не клиентские

STOCK_TITLE = "Склад"
INTAKE_TITLE = "Приймання"

# Провижинингу нужен полный drive (создать + расшарить), не readonly как в рантайме.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SUMMARY_TITLE = "📊 Зведення"
LOW_STOCK = 3  # совпадает с settings.low_stock_threshold
PROTECT_DESC = "Залишки править лише бот/Script (owner/dev — за винятком)"
_DEFAULT_TABS = {"Sheet1", "Аркуш1", "Лист1"}


def _rgb(r: float, g: float, b: float) -> dict:
    return {"red": r, "green": g, "blue": b}


HEADER_BG = _rgb(0.17, 0.29, 0.45)
HEADER_FG = _rgb(1, 1, 1)
BAND2 = _rgb(0.93, 0.95, 0.98)
RED, AMBER, GREEN = _rgb(0.96, 0.80, 0.78), _rgb(0.99, 0.91, 0.71), _rgb(0.72, 0.88, 0.80)
RED_FG, GREEN_FG = _rgb(0.61, 0.10, 0.10), _rgb(0.10, 0.40, 0.20)
# Пастельные «блоки продукта» по категориям.
CATEGORY_PALETTE = [
    _rgb(0.85, 0.82, 0.96),
    _rgb(0.82, 0.92, 0.86),
    _rgb(0.99, 0.89, 0.80),
    _rgb(0.82, 0.90, 0.96),
    _rgb(0.96, 0.85, 0.90),
    _rgb(0.93, 0.93, 0.80),
]


def _grid(sid: int, r0: int, r1: int, c0: int, c1: int) -> dict:
    return {
        "sheetId": sid,
        "startRowIndex": r0,
        "endRowIndex": r1,
        "startColumnIndex": c0,
        "endColumnIndex": c1,
    }


def _cf_rule(grid: dict, ctype: str, value, bg: dict, fg: dict | None, bold: bool = False) -> dict:
    values = value if isinstance(value, list) else [value]
    fmt: dict = {"backgroundColor": bg}
    text_fmt: dict = {}
    if fg:
        text_fmt["foregroundColor"] = fg
    if bold:
        text_fmt["bold"] = True
    if text_fmt:
        fmt["textFormat"] = text_fmt
    return {
        "addConditionalFormatRule": {
            "index": 0,
            "rule": {
                "ranges": [grid],
                "booleanRule": {
                    "condition": {
                        "type": ctype,
                        "values": [{"userEnteredValue": v} for v in values],
                    },
                    "format": fmt,
                },
            },
        }
    }


def authorize() -> gspread.Client:
    settings = get_settings()
    raw = settings.google_sa_json.strip()
    if not raw:
        raise SystemExit("GOOGLE_SA_JSON не настроен (ожидаю ./secrets/service-account.json)")
    if raw.startswith("{"):
        import json

        creds = Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(raw, scopes=SCOPES)
    return gspread.authorize(creds)


async def active_client_tabs() -> list[str]:
    """ПІБ активных клиентов из БД (= имена листов, stock_sheet_key)."""
    sm = get_sessionmaker()
    async with sm() as session:
        rows = (
            await session.execute(
                select(User.full_name, User.telegram_id).where(
                    User.role == UserRole.client,
                    User.status == UserStatus.active,
                )
            )
        ).all()
    # full_name или telegram_id — точно как stock_sheet_key(client).
    return [(name or str(tg)).strip() for name, tg in rows if (name or tg)]


def open_or_create(gc: gspread.Client, book_id: str, title: str) -> tuple[Any, bool]:
    if book_id:
        return gc.open_by_key(book_id), False
    return gc.create(title), True


def ensure_worksheet(book: Any, title: str, headers: list[str], hidden: bool = False) -> Any:
    existing = {ws.title: ws for ws in book.worksheets()}
    if title in existing:
        ws = existing[title]
    else:
        ws = book.add_worksheet(title=title, rows=1000, cols=max(len(headers), 10))
    # Заголовки в строке 1 (перезаписываем — порядок колонок критичен для бота).
    ws.update(values=[headers], range_name="A1")
    ws.freeze(rows=1)
    if hidden:
        book.batch_update(
            {
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": ws.id, "hidden": True},
                            "fields": "hidden",
                        }
                    }
                ]
            }
        )
    return ws


def style_header(book: Any, ws: Any, ncols: int) -> None:
    book.batch_update(
        {
            "requests": [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": ncols,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {"bold": True},
                                "backgroundColor": {"red": 0.85, "green": 0.9, "blue": 0.95},
                                "horizontalAlignment": "CENTER",
                            }
                        },
                        "fields": "userEnteredFormat(textFormat,backgroundColor,horizontalAlignment)",
                    }
                }
            ]
        }
    )


def setup_intake_validation(book: Any, ws: Any) -> None:
    sid = ws.id
    state_col = INTAKE_HEADERS.index("Стан")  # 7
    done_col = INTAKE_HEADERS.index("Оброблено")  # 8
    book.batch_update(
        {
            "requests": [
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": sid,
                            "startRowIndex": 1,
                            "endRowIndex": 1000,
                            "startColumnIndex": state_col,
                            "endColumnIndex": state_col + 1,
                        },
                        "rule": {
                            "condition": {
                                "type": "ONE_OF_LIST",
                                "values": [
                                    {"userEnteredValue": "годне"},
                                    {"userEnteredValue": "брак"},
                                ],
                            },
                            "showCustomUi": True,
                            "strict": False,
                        },
                    }
                },
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": sid,
                            "startRowIndex": 1,
                            "endRowIndex": 1000,
                            "startColumnIndex": done_col,
                            "endColumnIndex": done_col + 1,
                        },
                        "rule": {"condition": {"type": "BOOLEAN"}},
                    }
                },
            ]
        }
    )


def share(book: Any, emails: list[str]) -> None:
    for email in emails:
        book.share(email, perm_type="user", role="writer", notify=False)


def _clear_dynamic(sheet_meta: dict, sid: int) -> list[dict]:
    """Снос прежних бэндингов/условных правил/фильтра/нашей защиты — идемпотентность."""
    reqs: list[dict] = []
    for band in sheet_meta.get("bandedRanges", []) or []:
        reqs.append({"deleteBanding": {"bandedRangeId": band["bandedRangeId"]}})
    rules = sheet_meta.get("conditionalFormats", []) or []
    for idx in range(len(rules) - 1, -1, -1):
        reqs.append({"deleteConditionalFormatRule": {"sheetId": sid, "index": idx}})
    if sheet_meta.get("basicFilter"):
        reqs.append({"clearBasicFilter": {"sheetId": sid}})
    for pr in sheet_meta.get("protectedRanges", []) or []:
        if pr.get("description") == PROTECT_DESC:
            reqs.append({"deleteProtectedRange": {"protectedRangeId": pr["protectedRangeId"]}})
    return reqs


def style_stock_worksheet(book: Any, ws: Any, sheet_meta: dict) -> int:
    """Богатое оформление листа «Склад» клиента. Возвращает число строк данных.

    ВАЖНО: на Кількість/Ціна НЕ ставим numberFormat — в книге локаль с запятой,
    «0.00» показал бы «259,00», а gspread.get_all_records прочитал бы это как 25900
    (×100) и сломал цену боту. Только выравнивание + очистка формата (голые числа).
    """
    records = ws.get_all_records(default_blank="")
    n = len(records)
    last = n + 1
    sid = ws.id
    cats = sorted({str(r.get("Категорія", "")).strip() for r in records if r.get("Категорія")})

    reqs = _clear_dynamic(sheet_meta, sid)
    reqs.append(
        {
            "repeatCell": {
                "range": _grid(sid, 0, 1, 0, 5),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": HEADER_BG,
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                        "textFormat": {"bold": True, "foregroundColor": HEADER_FG, "fontSize": 11},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)",
            }
        }
    )
    reqs.append(
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        }
    )
    if last >= 2:
        reqs.append(
            {
                "addBanding": {
                    "bandedRange": {
                        "range": _grid(sid, 0, last, 0, 5),
                        "rowProperties": {
                            "headerColor": HEADER_BG,
                            "firstBandColor": _rgb(1, 1, 1),
                            "secondBandColor": BAND2,
                        },
                    }
                }
            }
        )
    # выравнивание + очистка numberFormat (маска включает numberFormat, значения не даём)
    reqs.append(
        {
            "repeatCell": {
                "range": _grid(sid, 1, 1000, 3, 4),  # D: Кількість
                "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
                "fields": "userEnteredFormat(numberFormat,horizontalAlignment)",
            }
        }
    )
    reqs.append(
        {
            "repeatCell": {
                "range": _grid(sid, 1, 1000, 4, 5),  # E: Ціна
                "cell": {"userEnteredFormat": {"horizontalAlignment": "RIGHT"}},
                "fields": "userEnteredFormat(numberFormat,horizontalAlignment)",
            }
        }
    )
    dcol = _grid(sid, 1, 1000, 3, 4)
    reqs += [
        _cf_rule(dcol, "NUMBER_LESS_THAN_EQ", str(LOW_STOCK), RED, RED_FG, bold=True),
        _cf_rule(dcol, "NUMBER_BETWEEN", [str(LOW_STOCK + 1), "9"], AMBER, None),
        _cf_rule(dcol, "NUMBER_GREATER_THAN_EQ", "10", GREEN, GREEN_FG),
    ]
    if cats:  # умные чипы: дропдаун категорий + цвет-блок по категории
        reqs.append(
            {
                "setDataValidation": {
                    "range": _grid(sid, 1, 1000, 2, 3),
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_LIST",
                            "values": [{"userEnteredValue": c} for c in cats],
                        },
                        "showCustomUi": True,
                        "strict": False,
                    },
                }
            }
        )
        ccol = _grid(sid, 1, 1000, 2, 3)
        for i, cat in enumerate(cats):
            reqs.append(
                _cf_rule(ccol, "TEXT_EQ", cat, CATEGORY_PALETTE[i % len(CATEGORY_PALETTE)], None)
            )
    reqs.append({"setBasicFilter": {"filter": {"range": _grid(sid, 0, max(last, 2), 0, 5)}}})
    # защита залишків (warningOnly), как было в provision
    reqs.append(
        {
            "addProtectedRange": {
                "protectedRange": {
                    "range": _grid(sid, 1, 1000, 0, len(STOCK_HEADERS)),
                    "description": PROTECT_DESC,
                    "warningOnly": True,
                }
            }
        }
    )

    book.batch_update({"requests": reqs})
    book.batch_update(
        {
            "requests": [
                {
                    "autoResizeDimensions": {
                        "dimensions": {
                            "sheetId": sid,
                            "dimension": "COLUMNS",
                            "startIndex": 0,
                            "endIndex": 5,
                        }
                    }
                }
            ]
        }
    )
    return n


def _to_decimal(raw) -> Decimal:
    try:
        return Decimal(str(raw).replace(" ", "").replace(",", "."))
    except (InvalidOperation, AttributeError):
        return Decimal(0)


def build_summary(book: Any, data_ws: Any) -> None:
    """Лист «📊 Зведення»: KPI (с валютой — бот его НЕ читает) + живой pivot по категориям."""
    records = data_ws.get_all_records(default_blank="")
    positions = sum(1 for r in records if r.get("Артикул") and r.get("Назва"))
    units = sum(int(_to_decimal(r.get("Кількість", 0))) for r in records)
    value = sum(_to_decimal(r.get("Кількість", 0)) * _to_decimal(r.get("Ціна", 0)) for r in records)

    try:
        ws = book.worksheet(SUMMARY_TITLE)
    except gspread.WorksheetNotFound:
        ws = book.add_worksheet(title=SUMMARY_TITLE, rows=200, cols=8)
    ws.clear()
    sid = ws.id
    ws.update(
        values=[
            [f"📊 Зведення складу — {data_ws.title}"],
            [],
            ["Позицій", positions],
            ["Одиниць", units],
            ["Вартість, ₴", float(value)],
        ],
        range_name="A1",
    )

    meta = next(
        s for s in book.fetch_sheet_metadata()["sheets"] if s["properties"]["sheetId"] == sid
    )
    reqs = _clear_dynamic(meta, sid)
    reqs.append({"mergeCells": {"range": _grid(sid, 0, 1, 0, 4), "mergeType": "MERGE_ALL"}})
    reqs.append(
        {
            "repeatCell": {
                "range": _grid(sid, 0, 1, 0, 4),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": HEADER_BG,
                        "horizontalAlignment": "CENTER",
                        "textFormat": {"bold": True, "foregroundColor": HEADER_FG, "fontSize": 13},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)",
            }
        }
    )
    reqs.append(
        {
            "repeatCell": {
                "range": _grid(sid, 2, 5, 0, 1),
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat",
            }
        }
    )
    reqs.append(
        {
            "repeatCell": {
                "range": _grid(sid, 4, 5, 1, 2),
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": {"type": "CURRENCY", "pattern": "#,##0.00 ₴"}
                    }
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        }
    )
    reqs.append(
        {
            "updateCells": {
                "start": {"sheetId": sid, "rowIndex": 7, "columnIndex": 0},
                "fields": "pivotTable",
                "rows": [
                    {
                        "values": [
                            {
                                "pivotTable": {
                                    "source": _grid(data_ws.id, 0, len(records) + 1, 0, 5),
                                    "rows": [
                                        {
                                            "sourceColumnOffset": 2,
                                            "showTotals": True,
                                            "sortOrder": "ASCENDING",
                                        }
                                    ],
                                    "values": [
                                        {
                                            "summarizeFunction": "COUNTA",
                                            "sourceColumnOffset": 0,
                                            "name": "Позицій",
                                        },
                                        {
                                            "summarizeFunction": "SUM",
                                            "sourceColumnOffset": 3,
                                            "name": "Одиниць",
                                        },
                                    ],
                                    "valueLayout": "HORIZONTAL",
                                }
                            }
                        ]
                    }
                ],
            }
        }
    )
    book.batch_update({"requests": reqs})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--share", default="", help="email(ы) через запятую — дать доступ writer")
    parser.add_argument("--clients", default="", help="доп. имена листов (тестовые), через запятую")
    parser.add_argument("--dry-run", action="store_true", help="только показать план, без записи")
    args = parser.parse_args()

    settings = get_settings()
    db_tabs = asyncio.run(active_client_tabs())
    extra = [c.strip() for c in args.clients.split(",") if c.strip()]
    # Уникальные, с сохранением порядка: клиенты из БД + тестовые из флага.
    tabs: list[str] = list(dict.fromkeys(db_tabs + extra))
    emails = [e.strip() for e in args.share.split(",") if e.strip()]

    print(f"Листы под клиентов: {tabs or '(нет — только _TEMPLATE)'}")
    print(f"Share writer: {emails or '(никому — SA останется владельцем)'}")
    if args.dry_run:
        print("dry-run: выходим без изменений.")
        return

    gc = authorize()

    # --- «Склад» ---
    stock, _ = open_or_create(gc, settings.sheets_stock_book_id, STOCK_TITLE)
    style_header(stock, ensure_worksheet(stock, TEMPLATE_TAB, STOCK_HEADERS), len(STOCK_HEADERS))
    client_ws = [ensure_worksheet(stock, tab, STOCK_HEADERS) for tab in tabs]
    _drop_empty_defaults(stock)
    # одна выборка метаданных на книгу → идемпотентная чистка прежнего оформления
    meta_map = {s["properties"]["sheetId"]: s for s in stock.fetch_sheet_metadata()["sheets"]}
    summary_src = None
    for ws in client_ws:
        rows = style_stock_worksheet(stock, ws, meta_map.get(ws.id, {}))
        if summary_src is None and rows > 0:
            summary_src = ws
    if summary_src is not None:
        build_summary(stock, summary_src)
    share(stock, emails)

    # --- «Приймання» ---
    intake, _ = open_or_create(gc, settings.sheets_intake_book_id, INTAKE_TITLE)
    tmpl_i = ensure_worksheet(intake, TEMPLATE_TAB, INTAKE_HEADERS)
    style_header(intake, tmpl_i, len(INTAKE_HEADERS))
    setup_intake_validation(intake, tmpl_i)
    for tab in tabs:
        ws = ensure_worksheet(intake, tab, INTAKE_HEADERS)
        style_header(intake, ws, len(INTAKE_HEADERS))
        setup_intake_validation(intake, ws)
    _drop_empty_defaults(intake)
    share(intake, emails)

    print("\n=== ГОТОВО. Впиши в .env: ===")
    print(f"SHEETS_STOCK_BOOK_ID={stock.id}")
    print(f"SHEETS_INTAKE_BOOK_ID={intake.id}")
    print(f"\nСклад:     {stock.url}")
    print(f"Приймання: {intake.url}")
    if not emails:
        print("\n⚠ Книги владеет service-account (нет UI/квоты Drive). Дай себе доступ:")
        print("   --share you@gmail.com  (или вручную Share → твой Google-аккаунт)")
    print("\nApps Script «Внести» — вставь scripts/intake_apps_script.gs в книгу")
    print("«Приймання»: Extensions → Apps Script. IMPORTRANGE между книгами требует")
    print("разового подтверждения «Allow access» в UI при первом обращении.")


def _drop_empty_defaults(book: Any) -> None:
    """Убрать пустые дефолтные вкладки Google (Sheet1/Лист1) — и у новых, и у ручных книг."""
    for ws in book.worksheets():
        if ws.title in _DEFAULT_TABS and not any(any(row) for row in ws.get_all_values()):
            book.del_worksheet(ws)


if __name__ == "__main__":
    main()
