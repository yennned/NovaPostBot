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
from app.db.models.client_account import ClientAccount, ClientAccountMembership
from app.db.models.enums import ClientAccountStatus
from app.db.models.user import User
from app.services.client_sheet_sync import _VIEW_HEADERS, _VIEW_TAB, ViewRow, _view_data_row
from app.sheets.client import _STOCK_EXPECTED_HEADERS
from app.sheets.history import HISTORY_TAB
from google.oauth2.service_account import Credentials
from gspread.utils import ValueInputOption, rowcol_to_a1
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
# H — ЕДИНСТВЕННЫЙ разделитель: всё, что правее, панель, и второго разрыва в ней нет.
PANEL_GAP_COL = len(STOCK_HEADERS)  # H — разрыв сразу после данных
PANEL_LABEL_COL = PANEL_GAP_COL + 1  # I — лейблы
PANEL_VALUE_COL = PANEL_GAP_COL + 2  # J — значения/селекторы (дропдауны)
_PANEL_VALUE_A1 = _col_a1(PANEL_VALUE_COL)  # «J» — для формул-ссылок на селекторы
# Разрез по категориям — I..L, ниже интерактивных блоков. Он заменил отдельный лист
# «📊 Зведення»: тот строился по ОДНОМУ (первому непустому) листу книги, то есть
# показывал разрез одного клиента, подписанный как свод всей книги.
BREAKDOWN_END_COL = PANEL_LABEL_COL + 4  # exclusive: I,J,K,L
#: Сколько строк под разрезом форматируем. Категорий у аккаунта десятки, не тысячи;
#: формула спиллится сама, а формат числа заранее ставится с запасом.
BREAKDOWN_FORMAT_ROWS = 200
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

#: Отдельный лист сводки из ранней версии. Больше не создаётся — разрез по
#: категориям переехал в панель на листе каждого клиента; имя осталось, чтобы
#: провижн умел этот лист снести.
SUMMARY_TITLE = "📊 Зведення"
PROTECT_DESC = "Залишки править лише бот/Script (owner/dev — за винятком)"
HISTORY_PROTECT_DESC = (
    "Журнал приймання. Не редагувати і не видаляти рядки вручну: по ньому бот "
    "переносить залишок. Прибирання — меню «📦 Склад»."
)
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


async def active_client_tabs(session: AsyncSession | None = None) -> list[str]:
    """Имена листов складов — по АККАУНТАМ, ровно как их адресует бот.

    Раньше здесь выбирались `User` с `role=client`, и это было наследство модели
    «лист на человека». Миграция `d4e5f6a7b8c0` снесла `users.stock_sheet_key`:
    склад принадлежит бизнес-аккаунту, у работника своего листа нет. Запрос по
    `User` пережил её и давал двойной промах — работнику аккаунта (он тоже
    `role=client`) заводился лишний лист по его ПІБ, а аккаунту, чьё имя не
    совпадает с ПІБ владельца, лист не заводился вовсе. Второе тише и хуже: бот
    читает несуществующую вкладку, то есть показывает пустой склад, а «Внести»
    падает «немає листа».

    Имя считается тем же выражением, что `stock_sheet_key()` в
    `app/services/inventory_backend.py`. Разойдись они — провижн и читатель снова
    смотрели бы в разные вкладки.

    `session` — для тестов: без него функция открывает свою, и незакоммиченные
    данные вызывающего ей не видны.
    """
    stmt = select(ClientAccount.stock_sheet_key, ClientAccount.name, ClientAccount.id).where(
        ClientAccount.status == ClientAccountStatus.active
    )
    if session is not None:
        rows = (await session.execute(stmt)).all()
    else:
        sm = get_sessionmaker()
        async with sm() as own:
            rows = (await own.execute(stmt)).all()
    return [(key or (name or "").strip() or str(aid)) for key, name, aid in rows]


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
    date_col = INTAKE_HEADERS.index("Дата")  # 0
    state_col = INTAKE_HEADERS.index("Стан")  # 7
    done_col = INTAKE_HEADERS.index("Оброблено")  # 8
    book.batch_update(
        {
            "requests": [
                # Дата: формат ДД.ММ.РРРР (Apps Script авто-ставит сегодняшнюю дату
                # при первой правке строки; ручной ввод через точку принимается).
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sid,
                            "startRowIndex": 1,
                            "endRowIndex": 1000,
                            "startColumnIndex": date_col,
                            "endColumnIndex": date_col + 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {"type": "DATE", "pattern": "dd.MM.yyyy"}
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                },
                # Дата: календарь-пикер по двойному клику (нестрого — допускает ручной ввод).
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": sid,
                            "startRowIndex": 1,
                            "endRowIndex": 1000,
                            "startColumnIndex": date_col,
                            "endColumnIndex": date_col + 1,
                        },
                        "rule": {
                            "condition": {"type": "DATE_IS_VALID"},
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


async def accounts_without_view_book() -> list[tuple[str, str, str]]:
    """Аккаунты без книги-зеркала → `(account_id, label, source_tab)`.

    `label` — для названия книги; `source_tab` — вкладка аккаунта в основном
    «Складі» (`stock_sheet_key`), откуда подтягивается остаток для оформления.

    Скоуп — аккаунт, а не пользователь, и это не косметика. Миграция
    `d4e5f6a7b8c0` (2026-07-15) снесла `users.stock_sheet_key` и
    `users.stock_view_book_id`: склад принадлежит бизнес-аккаунту, у работника
    своего листа нет. Запрос здесь остался по `User` и с тех пор падал
    `AttributeError` ещё на построении — то есть `--client-books` не работал
    вовсе, а `--attach-book` вместе с ним.
    """
    sm = get_sessionmaker()
    async with sm() as session:
        rows = (
            await session.execute(
                select(
                    ClientAccount.id,
                    ClientAccount.name,
                    ClientAccount.stock_sheet_key,
                ).where(ClientAccount.stock_view_book_id.is_(None))
            )
        ).all()
    return [
        (str(aid), (name or str(aid)).strip(), (ssk or name or str(aid)).strip())
        for aid, name, ssk in rows
    ]


#: У сервис-аккаунта нет собственного Drive: любая попытка создать файл упирается
#: в «The user's Drive storage quota has been exceeded» (403). Это не настройка и
#: не квота, которую можно поднять, — у SA просто нет хранилища. Проверено живьём
#: на боевом ключе 2026-08-03. Обходы: Shared Drive (нужен Workspace, у нас личные
#: Gmail) или создание книги человеком с последующим `--attach-book`.
NO_DRIVE_QUOTA_HINT = (
    "Сервіс-акаунт не може створювати файли в Google Drive — у нього немає власного "
    "сховища (403 storage quota exceeded). Це не лікується прапорцем.\n"
    "Робочий шлях для книги-дзеркала:\n"
    "  1. Власник створює порожню таблицю своїм Google-акаунтом;\n"
    "  2. ділиться нею на сервіс-акаунт як Редактора;\n"
    "  3. PYTHONPATH=. .venv/bin/python scripts/provision_sheets.py \\\n"
    "         --env-file .env.prod --attach-book <URL> --for <акаунт>\n"
    "Скрипт оформить «Товари», роздасть read-only і запише stock_view_book_id."
)


def _is_drive_quota_error(exc: Exception) -> bool:
    """Отличить «у SA нет Drive» от прочих сбоев Google.

    Важно именно отличить: остальные ошибки создания книги — про конкретный
    аккаунт, и цикл обязан идти дальше. Эта — общая, и ещё двадцать таких же
    сообщений подряд только спрячут причину.
    """
    return "storage quota" in str(exc).lower()


async def provision_client_view_books(
    gc: gspread.Client, clients: list[tuple[str, str, str]], emails: list[str]
) -> int:
    """Создать по книге-зеркалу на аккаунт, записать id в БД, раздать read-only.

    Порядок: создать книгу → записать `stock_view_book_id` (свой короткий сеанс,
    БД-соединение не висит на медленных вызовах Drive) → только потом шаринг. Так ни
    сбой шаринга, ни сбой на другом клиенте не оставляют созданную книгу без записи в
    БД, и повторный прогон не плодит дубли-сироты. Если шаринг упал — книга уже
    отслежена, её нужно расшарить вручную (логируется). Возвращает число полностью
    успешных (создана + записана + расшарена).
    """
    sm = get_sessionmaker()
    created = 0
    for account_id, label, source_tab in clients:
        try:
            book = gc.create(f"Склад — {label}")
            format_view_book(gc, book, source_tab)  # оформить «Товари» как основной «Склад»
        except Exception as exc:  # админ-скрипт: логируем и продолжаем со след. аккаунтом
            if _is_drive_quota_error(exc):
                raise SystemExit(NO_DRIVE_QUOTA_HINT) from exc
            print(f"  ! {label}: не вдалося створити книгу: {exc}")
            continue
        # book_id фиксируем в БД СРАЗУ после создания — до шаринга: даже если шаринг
        # упадёт, книга «отслежена» и повторный прогон не создаст дубль-сироту.
        async with sm() as session:
            account = await session.get(ClientAccount, uuid.UUID(account_id))
            if account is None:
                print(f"  ! {label}: акаунта вже нема в БД — книга {book.url} осиротіла")
                continue
            account.stock_view_book_id = book.id
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


async def _resolve_client(ref: str) -> tuple[str, str, str]:
    """Клиент по `ref` → `(user_id, label, source_tab)`. Числовой → telegram_id, иначе ILIKE по ПІБ.

    `source_tab` = имя вкладки клиента в основном «Складі» (persisted `stock_sheet_key`;
    он может расходиться с текущим `full_name` после смены ПІБ — берём именно его, иначе
    подтяжка остатков не найдёт лист). Требует ровно одно совпадение, иначе `SystemExit`.
    """
    ref = ref.strip()
    sm = get_sessionmaker()
    async with sm() as session:
        if ref.isdigit():
            # Телефон/telegram_id указывает на человека — от него идём к его аккаунту.
            cond = ClientAccount.id.in_(
                select(ClientAccountMembership.account_id)
                .join(User, User.id == ClientAccountMembership.user_id)
                .where(User.telegram_id == int(ref))
            )
        else:
            cond = ClientAccount.name.ilike(f"%{ref}%")
        rows = (
            await session.execute(
                select(ClientAccount.id, ClientAccount.name, ClientAccount.stock_sheet_key).where(
                    cond
                )
            )
        ).all()
    if not rows:
        raise SystemExit(f"Акаунта за '{ref}' не знайдено.")
    if len(rows) > 1:
        names = ", ".join(f"{n or '—'} ({i})" for i, n, _ in rows)
        raise SystemExit(f"За '{ref}' кілька акаунтів: {names}. Уточніть telegram_id.")
    aid, name, ssk = rows[0]
    return str(aid), (name or str(aid)), (ssk or name or str(aid))


async def _save_view_book_id(account_id: str, book_id: str) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        account = await session.get(ClientAccount, uuid.UUID(account_id))
        if account is None:
            raise SystemExit("Акаунта вже нема в БД.")
        account.stock_view_book_id = book_id
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

    То же оформление данных, что у клиентского листа «Склада»: тёмная шапка, бэндинг,
    подсветка низкого остатка, цвет-чипы категорий, автоширина, формула «Доступно».
    Отдельных листов НЕ создаём. Идемпотентно.

    `source_tab` — имя листа клиента в основном «Складі» (= его stock_sheet_key). Если
    задан, при оформлении подтягиваем текущие остатки, чтобы чипы/бэндинг/панель сразу
    совпали с данными (иначе на пустой книге данные-зависимое оформление не наложилось бы).
    Рантайм-синк далее держит данные свежими (пишет только A2:F).

    Панель — read-only-версия (`write_readonly_summary`, I–L): без дропдаунов (зритель
    их не меняет — книга только для чтения). Блок «Всього» + статичная таблица разреза
    «За категорією» (живые формулы, фиксирован лишь список категорий). Секции «За товаром»
    нет — поиск делает бот, а лист и так построчный.
    """
    ensure_locale(book)  # pin uk_UA — обяз. для «;»-формул панели/«Доступно»
    ws = ensure_worksheet(book, _VIEW_TAB, _VIEW_HEADERS)  # заголовки A1 + freeze(1)
    # «Без лишних листов»: снести отдельный лист сводки из ранней версии (если остался).
    with contextlib.suppress(gspread.WorksheetNotFound):
        book.del_worksheet(book.worksheet(SUMMARY_TITLE))
    _drop_empty_defaults(book)  # убрать дефолтную «Лист1»/«Sheet1»
    rows = _read_stock_rows(gc, source_tab)  # текущие остатки из основного «Складу»
    if rows is not None:
        # Чистим ВЕСЬ лист, а не первые 1000 строк: у крупнейшего клиента их 1636, и
        # жёсткая граница оставляла бы хвост от прошлого оформления жить дальше —
        # позиции, которых на складе давно нет, но которые клиент продолжает видеть.
        # Чистка идёт ПОСЛЕ успешного чтения: иначе сбой Google стирал бы остаток.
        ws.batch_clear([f"A2:G{max(2, ws.row_count)}"])
        if rows:
            # Свежий лист заводится на 1000 строк (`ensure_worksheet`), а у крупнейшего
            # клиента позиций 1636: без расширения сетки запись упала бы на «Range
            # exceeds grid limits» — то есть книга-зеркало для него не оформлялась вовсе.
            if ws.row_count < 1 + len(rows):
                ws.add_rows(1 + len(rows) - ws.row_count)
            ws.update(values=rows, range_name=f"A2:F{1 + len(rows)}")
    meta = next(
        (s for s in book.fetch_sheet_metadata()["sheets"] if s["properties"]["sheetId"] == ws.id),
        {},
    )
    style_stock_worksheet(book, ws, meta)  # шапка/бэндинг/CF/чипы/автоширина (по данным)
    write_available_formula(ws)  # G = Кількість − Резерв (ARRAYFORMULA)
    write_readonly_summary(book, ws)  # read-only-панель «Зведення» (I–L): статичный разрез


def _read_stock_rows(gc: gspread.Client, source_tab: str | None) -> list[list] | None:
    """Остатки клиента из основного «Складу» (лист `source_tab`) → строки «Товари» A–F.

    Порядок колонок — единый источник `_view_data_row` (тот же, что пишет рантайм-синк):
    строим `ViewRow` из записей «Складу» и прогоняем через него, чтобы контракт A–F жил
    в одном месте.

    `None` — **не прочитали** (нет книги, листа, доступа); `[]` — прочитали, и там пусто.
    Разница не косметическая: вызывающий по этому решает, чистить ли лист-зеркало. Раньше
    оба случая возвращали `[]`, и сбой чтения на секунду означал бы стереть клиенту весь
    видимый остаток — молча и до следующего провижна.
    """
    stock_id = get_settings().sheets_stock_book_id
    if not stock_id or not source_tab:
        return None
    try:
        ws = gc.open_by_key(stock_id).worksheet(source_tab)
        records = ws.get_all_records(default_blank="", expected_headers=STOCK_READ_HEADERS)
    except Exception as exc:
        print(f"  ! остатки з основного «Складу» не прочитані ({exc}) — оформлюю без даних.")
        return None
    rows = []
    for r in records:
        if not r.get("Артикул"):
            continue
        price = r.get("Ціна", "")
        rows.append(
            _view_data_row(
                ViewRow(
                    sku=r.get("Артикул", ""),
                    name=r.get("Назва", ""),
                    category=r.get("Категорія", "") or None,
                    price=_to_decimal(price) if price != "" else None,
                    stock=int(_to_decimal(r.get("Кількість", 0))),
                    reserved=int(_to_decimal(r.get("Резерв", 0))),
                    available=0,  # не пишется (G — ARRAYFORMULA); нужен лишь для типа
                )
            )
        )
    return rows


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


def protect_history(book: Any) -> bool:
    """Повесить предупреждение на лист «Історія». `False` — листа ещё нет.

    Лист заводит лениво Apps Script при первом «Внести» (`ensureHistory_`), там же
    ставится защита. Здесь — для книг, где журнал появился раньше этой защиты.

    `warningOnly`, а не запрет, и это не полумера: строки в журнал пишет Apps Script
    **от имени нажавшего «Внести»**, а не сервис-аккаунт. Жёсткий protected range
    без всех этих людей в `editors` остановил бы саму приёмку — то есть защита
    сломала бы то, что защищает.
    """
    try:
        ws = book.worksheet(HISTORY_TAB)
    except gspread.WorksheetNotFound:
        return False
    meta = next(
        s for s in book.fetch_sheet_metadata()["sheets"] if s["properties"]["sheetId"] == ws.id
    )
    reqs = [
        {"deleteProtectedRange": {"protectedRangeId": pr["protectedRangeId"]}}
        for pr in meta.get("protectedRanges", []) or []
        if pr.get("description") == HISTORY_PROTECT_DESC
    ]
    reqs.append(
        {
            "addProtectedRange": {
                "protectedRange": {
                    "range": {"sheetId": ws.id},
                    "description": HISTORY_PROTECT_DESC,
                    "warningOnly": True,
                }
            }
        }
    )
    book.batch_update({"requests": reqs})
    return True


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


# --- Общие билдеры batch-update запросов оформления панели «Зведення» ---------
# Их зовут ОБЕ панели (write_side_summary — основная, write_readonly_summary —
# зеркало), чтобы форматирование жило в одном месте и панели не расходились.
_PANEL_BORDER = {"style": "SOLID", "color": _rgb(0.78, 0.80, 0.85)}
_CURRENCY_FMT = "#,##0.00 ₴"
_INT_FMT = "#,##0"


def _col_width_req(sid: int, idx: int, px: int, span: int = 1) -> dict:
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sid,
                "dimension": "COLUMNS",
                "startIndex": idx,
                "endIndex": idx + span,
            },
            "properties": {"pixelSize": px},
            "fields": "pixelSize",
        }
    }


def _bg_req(sid: int, r0: int, r1: int, c0: int, c1: int, color: dict) -> dict:
    return {
        "repeatCell": {
            "range": _grid(sid, r0, r1, c0, c1),
            "cell": {"userEnteredFormat": {"backgroundColor": color}},
            "fields": "userEnteredFormat.backgroundColor",
        }
    }


def _banner_req(sid: int, r0: int, c0: int, c1: int, color: dict, font_size: int) -> dict:
    return {
        "repeatCell": {
            "range": _grid(sid, r0, r0 + 1, c0, c1),
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


def _numfmt_req(sid: int, r0: int, r1: int, c0: int, c1: int, ntype: str, pattern: str) -> dict:
    return {
        "repeatCell": {
            "range": _grid(sid, r0, r1, c0, c1),
            "cell": {"userEnteredFormat": {"numberFormat": {"type": ntype, "pattern": pattern}}},
            "fields": "userEnteredFormat.numberFormat",
        }
    }


def _merge_req(sid: int, r0: int, r1: int, c0: int, c1: int) -> dict:
    return {"mergeCells": {"range": _grid(sid, r0, r1, c0, c1), "mergeType": "MERGE_ALL"}}


_BORDER_SIDES = ("top", "bottom", "left", "right", "innerHorizontal", "innerVertical")


def _borders_req(sid: int, r0: int, r1: int, c0: int, c1: int) -> dict:
    return {
        "updateBorders": {
            "range": _grid(sid, r0, r1, c0, c1),
            **dict.fromkeys(_BORDER_SIDES, _PANEL_BORDER),
        }
    }


def side_summary_cells() -> list[list[str]]:
    """Значения панели «Зведення» (I1:J19): лейбл + формула/селектор (USER_ENTERED).

    Три секции (формулы, не статичные числа → пересчёт живьём при правке остатка
    ботом/приёмкой/руками; открытые диапазоны авто-захватывают новые строки):
      • Всього — позиції/одиниці/вартість по всьому листу;
      • За категорією — фільтр по ячейке-селектору J7 (дропдаун «Всі»+категорії);
      • За товаром — фільтр по ячейке-селектору J13 (дропдаун «Назва (Артикул)»:
        Google фільтрує список по будь-якій частині рядка → пошук і по назві, і по
        артикулу; артикул у дужках робить пункт унікальним ключем). Реальный ключ
        lookup — резолв-артикул в J14 (`REGEXEXTRACT` хвоста «(артикул)»).
    Книга в локали с запятой → разделитель аргументов «;».
    """
    cat = f"${_PANEL_VALUE_A1}$7"  # ячейка выбора категории (селектор)
    tov = f"${_PANEL_VALUE_A1}$13"  # ячейка выбора товара (комбинированная «Назва (Артикул)»)
    art = f"${_PANEL_VALUE_A1}$14"  # резолв-артикул из tov — ключ lookup
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
        ["Товар", ""],
        ["Артикул", rf'=IFERROR(REGEXEXTRACT({tov};"\(([^)]+)\)\s*$");"")'],
        ["Назва", f'=IFERROR(VLOOKUP({art};A2:E;2;0);"")'],
        ["Категорія", f'=IFERROR(VLOOKUP({art};A2:E;3;0);"")'],
        ["Кількість", f'=IF({art}="";"";SUMIF(A2:A;{art};D2:D))'],
        ["Ціна, ₴", f'=IFERROR(VLOOKUP({art};A2:E;5;0);"")'],
        [
            "Вартість, ₴",
            f'=IF({art}="";"";SUMIF(A2:A;{art};D2:D)*IFERROR(VLOOKUP({art};A2:E;5;0);0))',
        ],
    ]


def breakdown_headers() -> list[list[str]]:
    """Баннер и шапка разреза по категориям (две строки, I..L)."""
    return [
        ["📊 Розріз за категоріями", "", "", ""],
        ["Категорія", "Позицій", "Одиниць", "Вартість, ₴"],
    ]


def breakdown_formula() -> str:
    """Одна формула на весь разрез: категория + три метрики, спилл вниз по I..L.

    **Список категорий живой, а не зафиксированный на провижне.** В книге-зеркале
    он статичный (`readonly_summary_cells`) — та книга read-only и переоформляется
    привязкой. Здесь так нельзя: приёмка заводит категории каждый день, и застывший
    список молча показывал бы неправду в рабочей книге.

    **`SUMPRODUCT` с точным `=`, а не `COUNTIF`/`SUMIF`.** Критерий последних
    трактует `* ? ~` как шаблон, поэтому категория вида «USB*C» дала бы счётчик,
    не сходящийся с собственной вартістю. Та же причина расписана в
    `readonly_summary_cells`.

    **`IFERROR` снаружи** — у нового аккаунта категорий ещё нет, `FILTER` вернул бы
    `#N/A` на всю панель.

    Книга в локали с запятой → разделитель аргументов «;».
    """
    return (
        '=IFERROR(LET(cats;SORT(UNIQUE(FILTER(C2:C;C2:C<>"")));'
        "HSTACK(cats;"
        'MAP(cats;LAMBDA(c;SUMPRODUCT((C2:C=c)*(A2:A<>""))));'
        "MAP(cats;LAMBDA(c;SUMPRODUCT((C2:C=c)*D2:D)));"
        'MAP(cats;LAMBDA(c;SUMPRODUCT((C2:C=c)*D2:D*E2:E)))));"")'
    )


def write_side_summary(book: Any, ws: Any) -> None:
    """Панель «Зведення» СПРАВА вплотную к данным: всё с колонки I, H — разделитель.

    Справа (а не внизу) → строки растут вниз (`appendRow` приёмки/бота) и панель их
    не задевает: итог автоматический и никогда не «сползает», без правки Apps Script.

    Четыре блока сверху вниз: «Всього», «За категорією» (селектор J7), «За товаром»
    (селектор J13) и **разрез по категориям** (I..L). Последний заменил отдельный
    лист «📊 Зведення» — он строился по одному, первому непустому листу книги, то
    есть показывал разрез одного клиента под видом свода всей книги.

    Разрез стоит ПОСЛЕДНИМ не по вкусу: его формула спиллится вниз на столько строк,
    сколько у аккаунта категорий, и любая занятая ячейка под ней превратила бы весь
    блок в `#REF!`.

    Список товара — скрытая колонка-помощник N (`Назва (Артикул)`, ARRAYFORMULA),
    чтобы дропдаун искался и по назві, и по артикулу и авто-захватывал новые строки.
    Именно N, а не L: L теперь занята последней колонкой разреза.

    Бот читает A:E с `expected_headers`, лишние колонки справа чтение не ломают
    (см. app/sheets/client.py).
    """
    sid = ws.id
    lbl, val, end = PANEL_LABEL_COL, PANEL_VALUE_COL, PANEL_VALUE_COL + 1
    brk_end = BREAKDOWN_END_COL  # exclusive: разрез занимает I..L
    # N, а не M: одна пустая колонка между видимой панелью и служебным списком —
    # запас, чтобы вставка колонки в конец разреза не наехала на помощника.
    helper_col = BREAKDOWN_END_COL + 1
    helper_a1 = _col_a1(helper_col)
    cells = side_summary_cells()
    last = len(cells)  # число строк панели (exclusive-граница разделов, идущих до конца)
    panel_range = f"{_col_a1(lbl)}1:{_PANEL_VALUE_A1}{last}"
    # Пустая строка между интерактивной частью и разрезом — иначе баннер разреза
    # прилипает к «Вартість» блока «За товаром» и читается как его продолжение.
    brk_head = last + 2  # 21 — баннер разреза
    brk_first = brk_head + 2  # 23 — первая строка данных (спилл формулы)
    records = ws.get_all_records(default_blank="", expected_headers=STOCK_READ_HEADERS)
    cats = sorted({str(r.get("Категорія", "")).strip() for r in records if r.get("Категорія")})
    safe_title = ws.title.replace("'", "''")
    tovar_range = f"='{safe_title}'!${helper_a1}$2:${helper_a1}$1000"

    # Снимаем прежние merge баннеров ДО записи (иначе запись их «закрытых» ячеек упадёт
    # при повторном прогоне). unmergeCells на не-смерженном диапазоне — безопасный no-op.
    book.batch_update(
        {
            "requests": [
                *({"unmergeCells": {"range": _grid(sid, r, r + 1, lbl, end)}} for r in (0, 5, 11)),
                {"unmergeCells": {"range": _grid(sid, brk_head - 1, brk_head, lbl, brk_end)}},
            ]
        }
    )
    # Лист «Складу» создан ровно под A–J (10 колонок) → расширяем сетку под колонку N.
    if ws.col_count < helper_col + 1:
        ws.add_cols(helper_col + 1 - ws.col_count)
    # Прежний спилл разреза стираем ДО записи: если категорий стало меньше, хвост
    # старых строк остался бы висеть под новым блоком как настоящие данные.
    #
    # Заодно чистим K и L выше разреза. До переезда колонки-помощника там (в L2)
    # жила её ARRAYFORMULA: оставь её — и она упрётся в заголовки разреза, повиснув
    # `#REF!` прямо в панели. Замечено прогоном на живом листе, а не рассуждением.
    ws.batch_clear(
        [
            f"{_col_a1(val + 1)}1:{_col_a1(brk_end - 1)}{brk_head - 1}",
            f"{_col_a1(lbl)}{brk_head}:{_col_a1(brk_end - 1)}",
        ]
    )
    # Скрытая колонка-помощник N: список «Назва (Артикул)» для дропдауна товара.
    # ARRAYFORMULA по открытому A2:A → авто-захват новых строк; пустые строки → "".
    ws.update(
        values=[['=ARRAYFORMULA(IF(A2:A="";"";B2:B&" ("&A2:A&")"))']],
        range_name=f"{helper_a1}2",
        value_input_option=ValueInputOption.user_entered,
    )
    ws.update(
        values=breakdown_headers(),
        range_name=f"{_col_a1(lbl)}{brk_head}:{_col_a1(brk_end - 1)}{brk_head + 1}",
    )
    # Формула пишется ОДНОЙ ячейкой: заполни соседние J..L пустыми строками — и спилл
    # упрётся в них `#REF!`, потому что для Sheets пустая строка тоже занятая ячейка.
    ws.update(
        values=[[breakdown_formula()]],
        range_name=f"{_col_a1(lbl)}{brk_first}",
        value_input_option=ValueInputOption.user_entered,
    )
    # raw=True по умолчанию → формулы стали бы текстом; форсим USER_ENTERED.
    ws.update(
        values=cells,
        range_name=panel_range,
        value_input_option=ValueInputOption.user_entered,
    )

    def _align_left(r0: int, r1: int) -> dict:  # только для селекторов панели, не шарится
        return {
            "repeatCell": {
                "range": _grid(sid, r0, r1, val, end),
                "cell": {"userEnteredFormat": {"horizontalAlignment": "LEFT"}},
                "fields": "userEnteredFormat.horizontalAlignment",
            }
        }

    reqs = [
        _col_width_req(sid, PANEL_GAP_COL, 22),  # разрыв-разделитель (тонкий)
        _col_width_req(sid, lbl, 150),  # лейблы
        _col_width_req(sid, val, 124),  # значения/селекторы
        # K и L существуют только ради разреза — ширина под «Одиниць»/«Вартість, ₴»
        _col_width_req(sid, val + 1, 100, span=2),
        # база: лейблы bold слева, значения справа, всё по центру вертикали
        {
            "repeatCell": {
                "range": _grid(sid, 1, last, lbl, val),
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
                "range": _grid(sid, 1, last, val, end),
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
        _bg_req(sid, 1, 4, lbl, end, BAND2),
        _bg_req(sid, 7, 10, lbl, end, BAND2),
        _bg_req(sid, 13, last, lbl, end, BAND2),
        # merge баннера и подзаголовков секций
        _merge_req(sid, 0, 1, lbl, end),
        _merge_req(sid, 5, 6, lbl, end),
        _merge_req(sid, 11, 12, lbl, end),
        _banner_req(sid, 0, lbl, end, HEADER_BG, 11),
        _banner_req(sid, 5, lbl, end, SUBHEADER_BG, 10),
        _banner_req(sid, 11, lbl, end, SUBHEADER_BG, 10),
        # ячейки-селекторы (дропдауны): подсветка + значение по левому краю
        _bg_req(sid, 6, 7, lbl, end, PICK_BG),
        _bg_req(sid, 12, 13, lbl, end, PICK_BG),
        _align_left(6, 7),
        _align_left(12, 13),
        # форматы чисел: ціле (позиції/одиниці/кількість), валюта (вартість/ціна)
        _numfmt_req(sid, 1, 3, val, end, "NUMBER", _INT_FMT),
        _numfmt_req(sid, 3, 4, val, end, "CURRENCY", _CURRENCY_FMT),
        _numfmt_req(sid, 7, 9, val, end, "NUMBER", _INT_FMT),
        _numfmt_req(sid, 9, 10, val, end, "CURRENCY", _CURRENCY_FMT),
        _numfmt_req(sid, 16, 17, val, end, "NUMBER", _INT_FMT),
        _numfmt_req(sid, 17, last, val, end, "CURRENCY", _CURRENCY_FMT),
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
        # дропдаун товара: «Назва (Артикул)» з прихованої колонки-помічника N
        {
            "setDataValidation": {
                "range": _grid(sid, 12, 13, val, end),
                "rule": {
                    "condition": {
                        "type": "ONE_OF_RANGE",
                        "values": [{"userEnteredValue": tovar_range}],
                    },
                    "showCustomUi": True,
                    "strict": False,
                },
            }
        },
        # прячем колонку-помощник N (служебный список для дропдауна товара)
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sid,
                    "dimension": "COLUMNS",
                    "startIndex": helper_col,
                    "endIndex": helper_col + 1,
                },
                "properties": {"hiddenByUser": True},
                "fields": "hiddenByUser",
            }
        },
        _borders_req(sid, 0, last, lbl, end),
        # --- разрез по категориям: баннер, шапка, форматы чисел, границы ---
        # K и L могли быть скрыты: в L до переезда сидела колонка-помощник, а её
        # прячут. Не показать их обратно — и разрез отрисуется в невидимые колонки,
        # то есть «Одиниць» и «Вартість» просто не появятся на экране.
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sid,
                    "dimension": "COLUMNS",
                    "startIndex": val + 1,
                    "endIndex": brk_end,
                },
                "properties": {"hiddenByUser": False},
                "fields": "hiddenByUser",
            }
        },
        _merge_req(sid, brk_head - 1, brk_head, lbl, brk_end),
        _banner_req(sid, brk_head - 1, lbl, brk_end, SUBHEADER_BG, 10),
        {
            "repeatCell": {
                "range": _grid(sid, brk_head, brk_head + 1, lbl, brk_end),
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                        "backgroundColor": BAND2,
                        "verticalAlignment": "MIDDLE",
                    }
                },
                "fields": "userEnteredFormat(textFormat,backgroundColor,verticalAlignment)",
            }
        },
        # Форматы ставим с запасом вниз: формула спиллится сама, и подгонять диапазон
        # под сегодняшнее число категорий значило бы переоформлять лист после каждой
        # новой категории — то есть после каждой приёмки с новым товаром.
        _numfmt_req(
            sid, brk_first - 1, brk_first + BREAKDOWN_FORMAT_ROWS, val, val + 2, "NUMBER", _INT_FMT
        ),
        _numfmt_req(
            sid,
            brk_first - 1,
            brk_first + BREAKDOWN_FORMAT_ROWS,
            val + 2,
            brk_end,
            "CURRENCY",
            _CURRENCY_FMT,
        ),
        _borders_req(sid, brk_head - 1, brk_head + 1, lbl, brk_end),
    ]
    book.batch_update({"requests": reqs})


def readonly_summary_cells(cats: list[str]) -> list[list[str]]:
    """Значения read-only-панели «Зведення» для книги-зеркала (I1:L…, USER_ENTERED).

    Read-only-версия: без единого дропдауна (зритель их не меняет). Вместо
    интерактивных селекторов — статичная таблица разреза по категориям: строка на
    каждую категорию из `cats`. Значения — ЖИВЫЕ формулы (пересчёт при обновлении
    остатка рантайм-синком), фиксирован лишь СПИСОК категорий (на момент провижна/
    привязки; новая категория попадёт в разрез после ре-привязки `--attach-book`).
    Книга в локали с запятой → разделитель аргументов «;».
    """
    rows: list[list[str]] = [
        ["📊 Зведення", "", "", ""],
        ["Позицій", "=COUNTA(A2:A)", "", ""],
        ["Одиниць", "=SUM(D2:D)", "", ""],
        ["Вартість, ₴", "=SUMPRODUCT(D2:D;E2:E)", "", ""],
        ["", "", "", ""],
        ["За категорією", "", "", ""],
        ["Категорія", "Позицій", "Одиниць", "Вартість, ₴"],
    ]
    for cat in cats:
        c = cat.replace('"', '""')  # экранируем кавычки для литерала в формуле
        # Все три метрики — через SUMPRODUCT (точное сравнение `=`). НЕ COUNTIF/SUMIF:
        # их критерий трактует `* ? ~` как шаблон → для категории вида «USB*C» счётчик
        # разошёлся бы с точной вартістю и строка не билась бы с «Разом».
        rows.append(
            [
                cat,
                f'=SUMPRODUCT((C2:C="{c}")*(A2:A<>""))',
                f'=SUMPRODUCT((C2:C="{c}")*D2:D)',
                f'=SUMPRODUCT((C2:C="{c}")*D2:D*E2:E)',
            ]
        )
    rows.append(["Разом", "=COUNTA(A2:A)", "=SUM(D2:D)", "=SUMPRODUCT(D2:D;E2:E)"])
    return rows


def write_readonly_summary(book: Any, ws: Any) -> None:
    """Read-only-панель «Зведення» СПРАВА от данных (колонки I–L) для книги-зеркала.

    Отличие от `write_side_summary`: НИ ОДНОГО дропдауна (книга у клиента read-only —
    менять ячейки-селекторы он не может). Блок «Всього» — живые формулы; разрез
    «За категорією» — статичная таблица (строка на категорию, значения-формулы). Секции
    «За товаром» нет: лист A–G и так построчный per-товар, а поиск делает бот.
    Идемпотентно — как основная панель, снимаем прежние merge перед записью.
    """
    sid = ws.id
    lbl = PANEL_LABEL_COL  # I — категорія/лейбл
    val = PANEL_VALUE_COL  # J — значения «Всього» / Позицій таблицы
    end4 = lbl + 4  # правая граница таблицы (I,J,K,L)
    records = ws.get_all_records(default_blank="", expected_headers=STOCK_READ_HEADERS)
    cats = sorted({str(r.get("Категорія", "")).strip() for r in records if r.get("Категорія")})

    values = readonly_summary_cells(cats)
    last = len(values)  # число строк панели (exclusive-граница; включает «Разом»)
    tbl0 = 7  # первая строка данных таблицы категорий (0-based)
    panel_range = f"{_col_a1(lbl)}1:{_col_a1(end4 - 1)}{last}"

    # Таблица разреза занимает I–L → расширяем сетку, если колонок меньше.
    if ws.col_count < end4:
        ws.add_cols(end4 - ws.col_count)
    # Идемпотентность: полностью зачистить область панели ПЕРЕД записью — иначе при
    # ре-привязке остаётся «хвост» от прежней (более длинной/интерактивной) панели:
    # старые значения ниже нового «Разом», дропдауны-валидации и merge. Зачищаем все
    # колонки от I до конца сетки (данные листа — только A–G, панель их не трогает).
    # Границы берём по факту (`col_count`/`row_count`): и не выйти за пределы сетки
    # (иначе «exceeds grid limits» на листе <1000 строк), и накрыть «хвост» при росте.
    right, bottom = ws.col_count, ws.row_count
    wipe = _grid(sid, 0, bottom, lbl, right)
    ws.batch_clear([f"{_col_a1(lbl)}1:{_col_a1(right - 1)}{bottom}"])  # значения
    book.batch_update(
        {
            "requests": [
                {"unmergeCells": {"range": wipe}},
                {"setDataValidation": {"range": wipe}},  # без rule → снимает дропдауны
                # сброс форматирования (фон/шрифт/рамки/числоформат) — иначе от прежней
                # более длинной панели остаются пустые, но крашеные ячейки ниже «Разом».
                {
                    "repeatCell": {
                        "range": wipe,
                        "cell": {"userEnteredFormat": {}},
                        "fields": "userEnteredFormat",
                    }
                },
            ]
        }
    )
    ws.update(
        values=values,
        range_name=panel_range,
        value_input_option=ValueInputOption.user_entered,
    )

    def _bold_band(r0: int, r1: int) -> dict:  # bold + фон BAND2 (шапка таблицы, «Разом»)
        return {
            "repeatCell": {
                "range": _grid(sid, r0, r1, lbl, end4),
                "cell": {
                    "userEnteredFormat": {"textFormat": {"bold": True}, "backgroundColor": BAND2}
                },
                "fields": "userEnteredFormat(textFormat,backgroundColor)",
            }
        }

    reqs = [
        # ширины: разрыв, категорія, числа (J,K span=2), вартість
        _col_width_req(sid, PANEL_GAP_COL, 22),
        _col_width_req(sid, lbl, 150),
        _col_width_req(sid, val, 92, span=2),
        _col_width_req(sid, end4 - 1, 112),
        # база: всё по центру вертикали
        {
            "repeatCell": {
                "range": _grid(sid, 1, last, lbl, end4),
                "cell": {"userEnteredFormat": {"verticalAlignment": "MIDDLE"}},
                "fields": "userEnteredFormat.verticalAlignment",
            }
        },
        # «Всього»: лейблы bold слева, значения справа
        {
            "repeatCell": {
                "range": _grid(sid, 1, 4, lbl, val),
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                        "horizontalAlignment": "LEFT",
                    }
                },
                "fields": "userEnteredFormat(textFormat,horizontalAlignment)",
            }
        },
        {
            "repeatCell": {
                "range": _grid(sid, 1, 4, val, val + 1),
                "cell": {"userEnteredFormat": {"horizontalAlignment": "RIGHT"}},
                "fields": "userEnteredFormat.horizontalAlignment",
            }
        },
        _bg_req(sid, 1, 4, lbl, val + 1, BAND2),  # карточка-фон под «Всього»
        # баннер и подзаголовок секции (merge + тёмный фон)
        _merge_req(sid, 0, 1, lbl, end4),
        _merge_req(sid, 5, 6, lbl, end4),
        _banner_req(sid, 0, lbl, end4, HEADER_BG, 11),
        _banner_req(sid, 5, lbl, end4, SUBHEADER_BG, 10),
        _bold_band(6, 7),  # шапка таблицы категорий
        _bold_band(last - 1, last),  # строка «Разом»
        # числовые форматы «Всього» (J): позиції/одиниці ціле, вартість валюта
        _numfmt_req(sid, 1, 3, val, val + 1, "NUMBER", _INT_FMT),
        _numfmt_req(sid, 3, 4, val, val + 1, "CURRENCY", _CURRENCY_FMT),
        # таблица категорий: J,K ціле; L валюта (строки данных + «Разом»)
        _numfmt_req(sid, tbl0, last, val, val + 2, "NUMBER", _INT_FMT),
        _numfmt_req(sid, tbl0, last, end4 - 1, end4, "CURRENCY", _CURRENCY_FMT),
        _borders_req(sid, 0, last, lbl, end4),
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
        "--only-missing",
        action="store_true",
        help="трогать только те листы, которых ещё нет (новый клиент на живых книгах)",
    )
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
    parser.add_argument(
        "--env-file",
        default=None,
        help="файл окружения стенда (напр. .env.prod) — завести листы на боевых книгах",
    )
    args = parser.parse_args()

    # Окружение стенда — до первого `get_settings()` (он кеширован). Через dotenv,
    # а не `source .env.prod` в шелле: `GOOGLE_SA_JSON` там лежит инлайн-JSON, и
    # шелл срезает кавычки — ключ приезжает битым, а ошибка выглядит как «плохой
    # сервис-аккаунт».
    if args.env_file:
        from scripts.e2e.env import load_stand_env

        print(f"Окружение: {load_stand_env(args.env_file)}")

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
        account_id, label, source_tab = _run_db(_resolve_client(args.attach_for))
        url = attach_view_book(gc, book_id, source_tab)
        _run_db(_save_view_book_id(account_id, book_id))
        print(f"Привʼязано: {label} → {url}\nstock_view_book_id = {book_id}")
        return

    # --- Персональные книги-зеркала клиентов (read-only) ---
    if args.client_books:
        pending = _run_db(accounts_without_view_book())
        print(f"\nКниги-зеркала для аккаунтов без stock_view_book_id: {len(pending)}")
        if pending:
            created = _run_db(provision_client_view_books(gc, pending, emails))
            print(f"stock_view_book_id записан для {created} з {len(pending)} акаунтів.")
        # Наполнение строк «Товари» делает рантайм-синк при следующей операции клиента.
        return

    # --- «Склад» ---
    stock, _ = open_or_create(gc, settings.sheets_stock_book_id, STOCK_TITLE)
    ensure_locale(stock)  # «;»-формулы панели требуют comma-decimal локали (uk_UA)
    style_header(stock, ensure_worksheet(stock, TEMPLATE_TAB, STOCK_HEADERS), len(STOCK_HEADERS))
    # `--only-missing` — про живые книги: полный проход переписывает шапку и
    # переоформляет КАЖДЫЙ лист клиента, включая тот, где 1600 строк. Ради одного
    # нового клиента платить этим не нужно, а на боевой книге ещё и рискованно.
    # Отбор — по КАЖДОЙ книге отдельно: лист «Склад» у клиента может быть, а
    # «Приймання» нет, и общий фильтр молча пропустил бы второй.
    stock_tabs = tabs
    if args.only_missing:
        present = {ws.title for ws in stock.worksheets()}
        stock_tabs = [tab for tab in tabs if tab not in present]
        print(f"«Склад» only-missing: заводим {stock_tabs or '(нечего)'}")
    client_ws = [ensure_worksheet(stock, tab, STOCK_HEADERS) for tab in stock_tabs]
    _drop_empty_defaults(stock)
    print(
        f"«{HISTORY_TAB}»: попередження про ручну правку "
        f"{'поставлено' if protect_history(stock) else 'пропущено (листа ще немає)'}"
    )
    # одна выборка метаданных на книгу → идемпотентная чистка прежнего оформления
    meta_map = {s["properties"]["sheetId"]: s for s in stock.fetch_sheet_metadata()["sheets"]}
    for ws in client_ws:
        style_stock_worksheet(stock, ws, meta_map.get(ws.id, {}))
        write_available_formula(ws)  # Доступно (G) = Кількість − Резерв (ARRAYFORMULA)
        write_side_summary(stock, ws)  # панель с колонки I: итоги, фильтры, разрез
    # Отдельный лист сводки больше не нужен — разрез по категориям живёт в панели на
    # листе КАЖДОГО клиента. Прежний строился по одному, первому непустому листу
    # книги, то есть показывал разрез одного клиента под видом свода всей книги.
    with contextlib.suppress(gspread.WorksheetNotFound):
        stock.del_worksheet(stock.worksheet(SUMMARY_TITLE))
        print(f"лист «{SUMMARY_TITLE}» видалено — зведення тепер у панелі кожного листа")
    share(stock, emails)

    # --- «Приймання» ---
    intake, _ = open_or_create(gc, settings.sheets_intake_book_id, INTAKE_TITLE)
    tmpl_i = ensure_worksheet(intake, TEMPLATE_TAB, INTAKE_HEADERS)
    style_header(intake, tmpl_i, len(INTAKE_HEADERS))
    setup_intake_validation(intake, tmpl_i)
    intake_tabs = tabs
    if args.only_missing:
        present_i = {ws.title for ws in intake.worksheets()}
        intake_tabs = [tab for tab in tabs if tab not in present_i]
        print(f"«Приймання» only-missing: заводим {intake_tabs or '(нечего)'}")
    for tab in intake_tabs:
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
