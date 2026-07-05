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
import contextlib
import json
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

import gspread
from app.config import get_settings
from app.db.base import get_engine, get_sessionmaker
from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User
from app.services.client_sheet_sync import _VIEW_HEADERS, _VIEW_TAB
from app.sheets.client import _STOCK_EXPECTED_HEADERS
from google.oauth2.service_account import Credentials
from gspread.utils import ValueInputOption, rowcol_to_a1
from sqlalchemy import select

# Колонки листа «Склад». Первые 5 (Артикул..Ціна) — каноничны для чтения ботом и
# живут единым источником в app/sheets/client (_STOCK_EXPECTED_HEADERS); здесь только
# дополняем их Резервом (F, пишет бот из Postgres через client_sheet_sync) и Доступно
# (G, ARRAYFORMULA =Кількість−Резерв, см. write_available_formula). Передаём первые 5
# как expected_headers, иначе панель-итог справа валит get_all_records при повторе.
STOCK_READ_HEADERS = list(_STOCK_EXPECTED_HEADERS)
STOCK_HEADERS = [*STOCK_READ_HEADERS, "Резерв", "Доступно"]


def _col_a1(col0: int) -> str:
    """0-based индекс колонки → буква A1 (0→A, 5→F, 9→J, 26→AA)."""
    return rowcol_to_a1(1, col0 + 1)[:-1]


# Панель «Зведення» справа от данных A–G (0-based колонки): тонкий разрыв, лейблы, значения.
PANEL_GAP_COL = len(STOCK_HEADERS)  # H — разрыв сразу после данных
PANEL_LABEL_COL = PANEL_GAP_COL + 1  # I — лейблы
PANEL_VALUE_COL = PANEL_GAP_COL + 2  # J — значения/селекторы (дропдауны)
_PANEL_VALUE_A1 = _col_a1(PANEL_VALUE_COL)  # «J» — для формул-ссылок на селекторы
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
# Панель «Зведення»: подзаголовки секций и подсветка ячеек-селекторов (дропдаунов).
SUBHEADER_BG = _rgb(0.30, 0.42, 0.58)
PICK_BG = _rgb(1.0, 0.97, 0.80)


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


# --- Персональные книги-зеркала клиента (read-only) --------------------------

# Вкладку/заголовки берём из client_sheet_sync (единый источник) — их наполняет
# рантайм `_sync_view_book`, провижн лишь создаёт книгу + вкладку и раздаёт доступ.


def share_readonly(book: Any, emails: list[str]) -> None:
    """Дать клиенту read-only доступ. Без email — «будь-хто з посиланням» (viewer).

    `with_link=True` — доступ только по ссылке (allowFileDiscovery=false), книга не
    индексируется поиском; ссылку клиенту отдаёт только бот (кнопка в «📦 Товари»).
    Книга персональная — чужой склад по ней не откроется.
    """
    if emails:
        for email in emails:
            book.share(email, perm_type="user", role="reader", notify=False)
    else:
        book.share(None, perm_type="anyone", role="reader", with_link=True)


async def clients_without_view_book() -> list[tuple[str, str]]:
    """Активные клиенты без книги-зеркала → `(user_id, label)`.

    `label` = `full_name` или `telegram_id` — как `client_label` в `_sync_view_book`.
    """
    sm = get_sessionmaker()
    async with sm() as session:
        rows = (
            await session.execute(
                select(User.id, User.full_name, User.telegram_id).where(
                    User.role == UserRole.client,
                    User.status == UserStatus.active,
                    User.stock_view_book_id.is_(None),
                )
            )
        ).all()
    return [(str(uid), (name or str(tg)).strip()) for uid, name, tg in rows]


async def provision_client_view_books(
    gc: gspread.Client, clients: list[tuple[str, str]], emails: list[str]
) -> int:
    """Создать по книге-зеркалу на клиента, записать id в БД, раздать read-only.

    Порядок: создать книгу → записать `stock_view_book_id` (свой короткий сеанс,
    БД-соединение не висит на медленных вызовах Drive) → только потом шаринг. Так ни
    сбой шаринга, ни сбой на другом клиенте не оставляют созданную книгу без записи в
    БД, и повторный прогон не плодит дубли-сироты. Если шаринг упал — книга уже
    отслежена, её нужно расшарить вручную (логируется). Возвращает число полностью
    успешных (создана + записана + расшарена).
    """
    sm = get_sessionmaker()
    created = 0
    for user_id, label in clients:
        try:
            book = gc.create(f"Склад — {label}")
            format_view_book(gc, book, label)  # оформить «Товари» как основной «Склад»
        except Exception as exc:  # админ-скрипт: логируем и продолжаем со след. клиентом
            print(f"  ! {label}: не вдалося створити книгу: {exc}")
            continue
        # book_id фиксируем в БД СРАЗУ после создания — до шаринга: даже если шаринг
        # упадёт, книга «отслежена» и повторный прогон не создаст дубль-сироту.
        async with sm() as session:
            user = await session.get(User, uuid.UUID(user_id))
            if user is None:
                print(f"  ! {label}: клієнта вже нема в БД — книга {book.url} осиротіла")
                continue
            user.stock_view_book_id = book.id
            await session.commit()
        try:
            share_readonly(book, emails)
        except Exception as exc:
            print(
                f"  ! {label}: книга {book.url} створена й записана, але шаринг не вдався "
                f"({exc}) — поділіться вручну (read-only)."
            )
            continue
        created += 1
        print(f"  • {label}: {book.url}")
    return created


# --- Ручная привязка книги-зеркала (SA не может создавать файлы — нет квоты Drive) ---
# Владелец создаёт книгу личным Google-аккаунтом, шарит на SA как редактора; этот путь
# лишь проверяет доступ SA и пишет stock_view_book_id. Для ≤15 клиентов — проще OAuth/
# Shared Drive (те окупаются на сотнях книг). См. docs / план QA #3.


def _extract_book_id(url_or_id: str) -> str:
    """Из ссылки Google Sheets (`…/spreadsheets/d/<id>/…`) или голого id → id книги."""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url_or_id.strip())
    return match.group(1) if match else url_or_id.strip()


def _sa_email() -> str | None:
    """`client_email` сервис-аккаунта из GOOGLE_SA_JSON — для подсказки про шаринг."""
    raw = get_settings().google_sa_json.strip()
    try:
        if raw.startswith("{"):
            data = json.loads(raw)
        else:
            with open(raw, encoding="utf-8") as fh:
                data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data.get("client_email")


async def _resolve_client(ref: str) -> tuple[str, str]:
    """Клиент по `ref` → `(user_id, label)`. Числовой → telegram_id, иначе ILIKE по ПІБ.

    Требует ровно одно совпадение (role=client), иначе `SystemExit` с подсказкой.
    """
    ref = ref.strip()
    cond = User.telegram_id == int(ref) if ref.isdigit() else User.full_name.ilike(f"%{ref}%")
    sm = get_sessionmaker()
    async with sm() as session:
        rows = (
            await session.execute(
                select(User.id, User.full_name, User.telegram_id).where(
                    User.role == UserRole.client, cond
                )
            )
        ).all()
    if not rows:
        raise SystemExit(f"Клієнта за '{ref}' не знайдено (role=client).")
    if len(rows) > 1:
        names = ", ".join(f"{n or '—'} ({t})" for _, n, t in rows)
        raise SystemExit(f"За '{ref}' кілька клієнтів: {names}. Уточніть telegram_id.")
    uid, name, tg = rows[0]
    return str(uid), (name or str(tg))


async def _save_view_book_id(user_id: str, book_id: str) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        user = await session.get(User, uuid.UUID(user_id))
        if user is None:
            raise SystemExit("Клієнта вже нема в БД.")
        user.stock_view_book_id = book_id
        await session.commit()


def attach_view_book(gc: gspread.Client, book_id: str, source_tab: str | None = None) -> str:
    """Проверить доступ SA к вручную созданной книге, оформить «Товари», раздать read-only.

    `source_tab` — лист клиента в основном «Складі» (подтянуть остатки для оформления).
    Возвращает `book.url`. Падает с внятной подсказкой, если SA не расшарен как редактор.
    """
    sa = _sa_email() or "сервіс-акаунт"
    try:
        book = gc.open_by_key(book_id)
    except Exception as exc:  # нет доступа/не тот id
        raise SystemExit(
            f"SA не має доступу до книги ({exc}). Поділіться таблицею на {sa} як Редактора."
        ) from exc
    try:
        format_view_book(gc, book, source_tab)  # доступ на запись + оформление «как Склад»
    except Exception as exc:
        raise SystemExit(
            f"SA не може писати в книгу ({exc}). Дайте {sa} доступ Редактора (не Читача)."
        ) from exc
    try:
        share_readonly(book, [])  # «будь-хто з посиланням → Читач» для клиента
    except Exception as exc:  # у SA может не быть права менять доступ — не критично
        print(
            f"  ! link-viewer не виставлено автоматично ({exc}) — зробіть вручну: "
            "Доступ за посиланням → Читач."
        )
    return book.url


def format_view_book(gc: gspread.Client, book: Any, source_tab: str | None = None) -> None:
    """Оформить книгу-зеркало клиента ТОЧНО как основной «Склад» — один лист «Товари».

    То же оформление, что у клиентского листа «Склада»: тёмная шапка, бэндинг, подсветка
    низкого остатка, цвет-чипы категорий, автоширина, формула «Доступно» и боковая панель
    «Зведення» (I–J: Всього / За категорією / За товаром). Отдельных листов НЕ создаём —
    как в основной таблице. Идемпотентно.

    `source_tab` — имя листа клиента в основном «Складі» (= его stock_sheet_key). Если
    задан, при оформлении подтягиваем текущие остатки, чтобы чипы/бэндинг/панель сразу
    совпали с данными (иначе на пустой книге данные-зависимое оформление не наложилось бы).
    Рантайм-синк далее держит данные свежими (пишет только A2:F).

    Панель read-only: селекторы «За категорією/За товаром» зритель не меняет (нет прав) —
    работает секция «Всього» и живые формулы; так же выглядит и основная таблица.
    """
    ensure_locale(book)  # pin uk_UA — обяз. для «;»-формул панели/«Доступно»
    ws = ensure_worksheet(book, _VIEW_TAB, _VIEW_HEADERS)  # заголовки A1 + freeze(1)
    # «Без лишних листов»: снести отдельный лист сводки из ранней версии (если остался).
    with contextlib.suppress(gspread.WorksheetNotFound):
        book.del_worksheet(book.worksheet(SUMMARY_TITLE))
    _drop_empty_defaults(book)  # убрать дефолтную «Лист1»/«Sheet1»
    ws.batch_clear(["A2:G1000"])  # снять старые данные/преамбулу
    rows = _read_stock_rows(gc, source_tab)  # текущие остатки из основного «Складу»
    if rows:
        ws.update(values=rows, range_name=f"A2:F{1 + len(rows)}")
    meta = next(
        (s for s in book.fetch_sheet_metadata()["sheets"] if s["properties"]["sheetId"] == ws.id),
        {},
    )
    style_stock_worksheet(book, ws, meta)  # шапка/бэндинг/CF/чипы/автоширина (по данным)
    write_available_formula(ws)  # G = Кількість − Резерв (ARRAYFORMULA)
    write_side_summary(book, ws)  # боковая панель «Зведення» (I–J) — как в основной


def _read_stock_rows(gc: gspread.Client, source_tab: str | None) -> list[list]:
    """Остатки клиента из основного «Складу» (лист `source_tab`) в порядке «Товари» A–F:
    Артикул, Назва, Категорія, Кількість, Ціна, Резерв. Нет книги/листа → пусто (не падаем)."""
    stock_id = get_settings().sheets_stock_book_id
    if not stock_id or not source_tab:
        return []
    try:
        ws = gc.open_by_key(stock_id).worksheet(source_tab)
    except gspread.WorksheetNotFound:
        return []
    records = ws.get_all_records(default_blank="", expected_headers=STOCK_READ_HEADERS)
    return [
        [
            r.get("Артикул", ""),
            r.get("Назва", ""),
            r.get("Категорія", ""),
            r.get("Кількість", ""),
            r.get("Ціна", ""),
            r.get("Резерв", "") or 0,
        ]
        for r in records
        if r.get("Артикул")
    ]


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
    records = ws.get_all_records(default_blank="", expected_headers=STOCK_READ_HEADERS)
    n = sum(1 for r in records if r.get("Артикул"))  # без «фантом»-строк панели справа
    last = n + 1
    sid = ws.id
    ncol = len(STOCK_HEADERS)  # 7: A-E данные + Резерв(F)/Доступно(G)
    cats = sorted({str(r.get("Категорія", "")).strip() for r in records if r.get("Категорія")})

    reqs = _clear_dynamic(sheet_meta, sid)
    reqs.append(
        {
            "repeatCell": {
                "range": _grid(sid, 0, 1, 0, ncol),
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
                        "range": _grid(sid, 0, last, 0, ncol),
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
    reqs.append(
        {
            "repeatCell": {
                "range": _grid(sid, 1, 1000, 5, ncol),  # F: Резерв, G: Доступно
                "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
                "fields": "userEnteredFormat(numberFormat,horizontalAlignment)",
            }
        }
    )
    dcol = _grid(sid, 1, 1000, 3, 4)
    low_stock = get_settings().low_stock_threshold  # единый порог с рантаймом бота
    reqs += [
        _cf_rule(dcol, "NUMBER_LESS_THAN_EQ", str(low_stock), RED, RED_FG, bold=True),
        _cf_rule(dcol, "NUMBER_BETWEEN", [str(low_stock + 1), "9"], AMBER, None),
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
    reqs.append({"setBasicFilter": {"filter": {"range": _grid(sid, 0, max(last, 2), 0, ncol)}}})
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
                            "endIndex": len(STOCK_HEADERS),
                        }
                    }
                }
            ]
        }
    )
    return n


def write_available_formula(ws: Any) -> None:
    """Доступно (G) = Кількість − Резерв одной ARRAYFORMULA (авто по всем строкам).

    Резерв (F) пишет бот из Postgres (client_sheet_sync.write_reserved). Пустой F → 0,
    тогда Доступно = Кількість. Локаль книги с запятой → разделитель аргументов «;».
    """
    ws.update(
        values=[['=ARRAYFORMULA(IF(A2:A="";"";D2:D-F2:F))']],
        range_name="G2",
        value_input_option=ValueInputOption.user_entered,
    )


def _to_decimal(raw) -> Decimal:
    try:
        return Decimal(str(raw).replace(" ", "").replace(",", "."))
    except (InvalidOperation, AttributeError):
        return Decimal(0)


def build_summary(book: Any, data_ws: Any) -> None:
    """Лист «📊 Зведення»: KPI (с валютой — бот его НЕ читает) + живой pivot по категориям."""
    records = data_ws.get_all_records(default_blank="", expected_headers=STOCK_READ_HEADERS)
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


def side_summary_cells() -> list[list[str]]:
    """Значения панели «Зведення» (I1:J18): лейбл + формула/селектор (USER_ENTERED).

    Три секции (формулы, не статичные числа → пересчёт живьём при правке остатка
    ботом/приёмкой/руками; открытые диапазоны авто-захватывают новые строки):
      • Всього — позиції/одиниці/вартість по всьому листу;
      • За категорією — фільтр по ячейке-селектору J7 (дропдаун «Всі»+категорії);
      • За товаром — фільтр по ячейке-селектору J13 (дропдаун артикулів A2:A).
    Книга в локали с запятой → разделитель аргументов «;».
    """
    cat = f"${_PANEL_VALUE_A1}$7"  # ячейка выбора категории (селектор)
    sku = f"${_PANEL_VALUE_A1}$13"  # ячейка выбора товара (Артикул)
    return [
        ["📊 Зведення", ""],
        ["Позицій", "=COUNTA(A2:A)"],
        ["Одиниць", "=SUM(D2:D)"],
        ["Вартість, ₴", "=SUMPRODUCT(D2:D;E2:E)"],
        ["", ""],
        ["За категорією", ""],
        ["Категорія", "Всі"],
        ["Позицій", f'=IF({cat}="Всі";COUNTA(A2:A);COUNTIF(C2:C;{cat}))'],
        ["Одиниць", f'=IF({cat}="Всі";SUM(D2:D);SUMIF(C2:C;{cat};D2:D))'],
        [
            "Вартість, ₴",
            f'=IF({cat}="Всі";SUMPRODUCT(D2:D;E2:E);SUMPRODUCT((C2:C={cat})*D2:D*E2:E))',
        ],
        ["", ""],
        ["За товаром", ""],
        ["Артикул", ""],
        ["Назва", f'=IFERROR(VLOOKUP({sku};A2:E;2;0);"")'],
        ["Категорія", f'=IFERROR(VLOOKUP({sku};A2:E;3;0);"")'],
        ["Кількість", f'=IF({sku}="";"";SUMIF(A2:A;{sku};D2:D))'],
        ["Ціна, ₴", f'=IFERROR(VLOOKUP({sku};A2:E;5;0);"")'],
        [
            "Вартість, ₴",
            f'=IF({sku}="";"";SUMIF(A2:A;{sku};D2:D)*IFERROR(VLOOKUP({sku};A2:E;5;0);0))',
        ],
    ]


def write_side_summary(book: Any, ws: Any) -> None:
    """Интерактивная панель «Зведення» СПРАВА вплотную к данным (колонки I–J).

    Справа (а не внизу) → строки растут вниз (`appendRow` приёмки/бота) и панель их
    не задевает: итог автоматический и никогда не «сползает», без правки Apps Script.
    Дропдауны (Data Validation) в ячейках-селекторах J7 (категорія) и J13 (артикул);
    подсчёты — формулы из `side_summary_cells`. Бот читает A:E с `expected_headers`,
    лишние колонки справа чтение не ломают (см. app/sheets/client.py).
    """
    sid = ws.id
    lbl, val, end = PANEL_LABEL_COL, PANEL_VALUE_COL, PANEL_VALUE_COL + 1
    panel_range = f"{_col_a1(lbl)}1:{_PANEL_VALUE_A1}18"
    records = ws.get_all_records(default_blank="", expected_headers=STOCK_READ_HEADERS)
    cats = sorted({str(r.get("Категорія", "")).strip() for r in records if r.get("Категорія")})
    safe_title = ws.title.replace("'", "''")
    sku_range = f"='{safe_title}'!$A$2:$A$1000"

    # Снимаем прежние merge баннеров ДО записи (иначе запись их «закрытых» ячеек упадёт
    # при повторном прогоне). unmergeCells на не-смерженном диапазоне — безопасный no-op.
    book.batch_update(
        {
            "requests": [
                {"unmergeCells": {"range": _grid(sid, r, r + 1, lbl, end)}} for r in (0, 5, 11)
            ]
        }
    )
    # raw=True по умолчанию → формулы стали бы текстом; форсим USER_ENTERED.
    ws.update(
        values=side_summary_cells(),
        range_name=panel_range,
        value_input_option=ValueInputOption.user_entered,
    )

    border = {"style": "SOLID", "color": _rgb(0.78, 0.80, 0.85)}

    def _col_width(idx: int, px: int) -> dict:
        return {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sid,
                    "dimension": "COLUMNS",
                    "startIndex": idx,
                    "endIndex": idx + 1,
                },
                "properties": {"pixelSize": px},
                "fields": "pixelSize",
            }
        }

    def _bg(r0: int, r1: int, color: dict) -> dict:
        return {
            "repeatCell": {
                "range": _grid(sid, r0, r1, lbl, end),
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        }

    def _banner(r0: int, color: dict, font_size: int) -> dict:
        return {
            "repeatCell": {
                "range": _grid(sid, r0, r0 + 1, lbl, end),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": color,
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                        "textFormat": {
                            "bold": True,
                            "foregroundColor": HEADER_FG,
                            "fontSize": font_size,
                        },
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)",
            }
        }

    def _numfmt(r0: int, r1: int, ntype: str, pattern: str) -> dict:
        return {
            "repeatCell": {
                "range": _grid(sid, r0, r1, val, end),
                "cell": {
                    "userEnteredFormat": {"numberFormat": {"type": ntype, "pattern": pattern}}
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        }

    def _align_left(r0: int, r1: int) -> dict:
        return {
            "repeatCell": {
                "range": _grid(sid, r0, r1, val, end),
                "cell": {"userEnteredFormat": {"horizontalAlignment": "LEFT"}},
                "fields": "userEnteredFormat.horizontalAlignment",
            }
        }

    reqs = [
        _col_width(PANEL_GAP_COL, 22),  # разрыв-разделитель (тонкий)
        _col_width(lbl, 150),  # лейблы
        _col_width(val, 124),  # значения/селекторы
        # база: лейблы bold слева, значения справа, всё по центру вертикали
        {
            "repeatCell": {
                "range": _grid(sid, 1, 18, lbl, val),
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                        "horizontalAlignment": "LEFT",
                        "verticalAlignment": "MIDDLE",
                    }
                },
                "fields": "userEnteredFormat(textFormat,horizontalAlignment,verticalAlignment)",
            }
        },
        {
            "repeatCell": {
                "range": _grid(sid, 1, 18, val, end),
                "cell": {
                    "userEnteredFormat": {
                        "horizontalAlignment": "RIGHT",
                        "verticalAlignment": "MIDDLE",
                    }
                },
                "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment)",
            }
        },
        # карточки-фон под строками результатов (всього / категорія / товар)
        _bg(1, 4, BAND2),
        _bg(7, 10, BAND2),
        _bg(13, 18, BAND2),
        # merge баннера и подзаголовков секций
        {"mergeCells": {"range": _grid(sid, 0, 1, lbl, end), "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": _grid(sid, 5, 6, lbl, end), "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": _grid(sid, 11, 12, lbl, end), "mergeType": "MERGE_ALL"}},
        _banner(0, HEADER_BG, 11),
        _banner(5, SUBHEADER_BG, 10),
        _banner(11, SUBHEADER_BG, 10),
        # ячейки-селекторы (дропдауны): подсветка + значение по левому краю
        _bg(6, 7, PICK_BG),
        _bg(12, 13, PICK_BG),
        _align_left(6, 7),
        _align_left(12, 13),
        # форматы чисел: ціле (позиції/одиниці/кількість), валюта (вартість/ціна)
        _numfmt(1, 3, "NUMBER", "#,##0"),
        _numfmt(3, 4, "CURRENCY", "#,##0.00 ₴"),
        _numfmt(7, 9, "NUMBER", "#,##0"),
        _numfmt(9, 10, "CURRENCY", "#,##0.00 ₴"),
        _numfmt(15, 16, "NUMBER", "#,##0"),
        _numfmt(16, 18, "CURRENCY", "#,##0.00 ₴"),
        # дропдаун категорій: «Всі» + наявні категорії
        {
            "setDataValidation": {
                "range": _grid(sid, 6, 7, val, end),
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": v} for v in ["Всі", *cats]],
                    },
                    "showCustomUi": True,
                    "strict": False,
                },
            }
        },
        # дропдаун товара: артикули з A2:A
        {
            "setDataValidation": {
                "range": _grid(sid, 12, 13, val, end),
                "rule": {
                    "condition": {
                        "type": "ONE_OF_RANGE",
                        "values": [{"userEnteredValue": sku_range}],
                    },
                    "showCustomUi": True,
                    "strict": False,
                },
            }
        },
        {
            "updateBorders": {
                "range": _grid(sid, 0, 18, lbl, end),
                "top": border,
                "bottom": border,
                "left": border,
                "right": border,
                "innerHorizontal": border,
                "innerVertical": border,
            }
        },
    ]
    book.batch_update({"requests": reqs})


def ensure_locale(book: Any, locale: str = "uk_UA") -> None:
    """Закрепить локаль книги (идемпотентно).

    Формулы панели «Зведення» и `write_available_formula` используют «;» как разделитель
    аргументов — это верно только для comma-decimal локали (uk_UA/ru_RU). Без явной
    установки книга наследует дефолт service-account (обычно en_US, dot-decimal), где
    разделитель «,» → все «;»-формулы молча ломаются. Ставим явно, чтобы инвариант
    держался by-construction.
    """
    book.batch_update(
        {
            "requests": [
                {
                    "updateSpreadsheetProperties": {
                        "properties": {"locale": locale},
                        "fields": "locale",
                    }
                }
            ]
        }
    )


def _run_db(coro):
    """`asyncio.run` + dispose кэшированного движка в том же цикле.

    Движок SQLAlchemy кэшируется на уровне модуля, а asyncpg-соединения привязаны к
    циклу событий. Без dispose следующий `asyncio.run` получил бы протухшее
    loop-bound соединение из пула → «RuntimeError: Event loop is closed».
    """

    async def _wrap():
        try:
            return await coro
        finally:
            await get_engine().dispose()

    return asyncio.run(_wrap())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--share", default="", help="email(ы) через запятую — дать доступ writer")
    parser.add_argument("--clients", default="", help="доп. имена листов (тестовые), через запятую")
    parser.add_argument("--dry-run", action="store_true", help="только показать план, без записи")
    parser.add_argument(
        "--client-books",
        action="store_true",
        help="создать персональные read-only книги-зеркала для клиентов без stock_view_book_id",
    )
    parser.add_argument(
        "--attach-book",
        default="",
        help="URL или id вручную созданной книги-зеркала — привязать к клиенту (--for)",
    )
    parser.add_argument(
        "--for",
        dest="attach_for",
        default="",
        help="клиент для --attach-book: telegram_id или фрагмент ПІБ",
    )
    args = parser.parse_args()

    settings = get_settings()
    db_tabs = _run_db(active_client_tabs())
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

    # --- Привязка вручную созданной книги-зеркала к клиенту ---
    if args.attach_book:
        if not args.attach_for:
            raise SystemExit("--attach-book потребує --for <telegram_id|фрагмент ПІБ>")
        book_id = _extract_book_id(args.attach_book)
        user_id, label = _run_db(_resolve_client(args.attach_for))
        # label = full_name = имя листа клиента в основном «Складі» (stock_sheet_key).
        url = attach_view_book(gc, book_id, label)
        _run_db(_save_view_book_id(user_id, book_id))
        print(f"Привʼязано: {label} → {url}\nstock_view_book_id = {book_id}")
        return

    # --- Персональные книги-зеркала клиентов (read-only) ---
    if args.client_books:
        pending = _run_db(clients_without_view_book())
        print(f"\nКниги-зеркала для клиентов без stock_view_book_id: {len(pending)}")
        if pending:
            created = _run_db(provision_client_view_books(gc, pending, emails))
            print(f"stock_view_book_id записан для {created} з {len(pending)} клиентів.")
        # Наполнение строк «Товари» делает рантайм-синк при следующей операции клиента.
        return

    # --- «Склад» ---
    stock, _ = open_or_create(gc, settings.sheets_stock_book_id, STOCK_TITLE)
    ensure_locale(stock)  # «;»-формулы панели требуют comma-decimal локали (uk_UA)
    style_header(stock, ensure_worksheet(stock, TEMPLATE_TAB, STOCK_HEADERS), len(STOCK_HEADERS))
    client_ws = [ensure_worksheet(stock, tab, STOCK_HEADERS) for tab in tabs]
    _drop_empty_defaults(stock)
    # одна выборка метаданных на книгу → идемпотентная чистка прежнего оформления
    meta_map = {s["properties"]["sheetId"]: s for s in stock.fetch_sheet_metadata()["sheets"]}
    summary_src = None
    for ws in client_ws:
        rows = style_stock_worksheet(stock, ws, meta_map.get(ws.id, {}))
        write_available_formula(ws)  # Доступно (G) = Кількість − Резерв (ARRAYFORMULA)
        write_side_summary(stock, ws)  # авто-итог + фильтры справа на каждом листе
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
