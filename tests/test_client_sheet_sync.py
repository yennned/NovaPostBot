"""Тесты best-effort синка клиентских Sheets (PR #49 follow-up).

Главный инвариант: `stock_sheet_key` в PG продвигается на новое имя ТОЛЬКО когда
переименование вкладки в «Складі» подтверждено. Если `update_title` упал — ключ
остаётся старым, иначе снапшот искал бы лист с именем, которого нет → пустой склад.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import ClientAccountRepository, UserRepository
from app.services.client_sheet_sync import (
    _VIEW_HEADERS,
    ViewRow,
    _view_data_row,
    sync_client_sheets,
)
from app.sheets.client import SheetsClient
from sqlalchemy.ext.asyncio import AsyncSession


class _FakeReader:
    """Пустой источник остатков — снапшот не нужен для проверки гейта."""

    def read_stock(self, client_key: str):
        return []

    def apply_deltas(self, client_key: str, deltas):  # pragma: no cover - не вызывается
        raise AssertionError("apply_deltas не должен вызываться в синке")


class _FakeWorksheet:
    def __init__(self, title: str, *, fail: bool) -> None:
        self.title = title
        self._fail = fail

    def update_title(self, new_title: str) -> None:
        if self._fail:
            raise RuntimeError("APIError: A sheet with the name already exists")
        self.title = new_title


class _FakeBook:
    def __init__(self, worksheet: _FakeWorksheet) -> None:
        self._ws = worksheet

    def worksheets(self):
        return [self._ws]

    def worksheet(self, title: str) -> _FakeWorksheet:
        return self._ws


class _FakeGc:
    def __init__(self, *, fail_rename: bool) -> None:
        self.fail_rename = fail_rename

    def open_by_key(self, book_id: str) -> _FakeBook:
        # Каждая книга («Склад»/«Приёмка») держит вкладку под старым именем.
        return _FakeBook(_FakeWorksheet("old_key", fail=self.fail_rename))

    def create(self, title: str):  # pragma: no cover - рантайм не создаёт книги
        raise AssertionError("gc.create() не должен вызываться в рантайм-синке")


async def _active_client(session: AsyncSession, telegram_id: int) -> object:
    client = await UserRepository(session).create(
        telegram_id=telegram_id,
        full_name="Нове Імʼя",
        role=UserRole.client,
        status=UserStatus.active,
    )
    client.stock_sheet_key = "old_key"
    await session.flush()
    return client


def _sheets_settings():
    from app.config import get_settings

    return get_settings().model_copy(
        update={
            "google_sa_json": '{"type": "service_account"}',
            "sheets_stock_book_id": "STOCK",
            "sheets_intake_book_id": "INTAKE",
        }
    )


async def test_stock_sheet_key_advances_on_successful_rename(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await _active_client(db_session, telegram_id=900)
    monkeypatch.setattr(SheetsClient, "_authorize", lambda self: _FakeGc(fail_rename=False))

    await sync_client_sheets(
        db_session, client=client, reader=_FakeReader(), settings=_sheets_settings()
    )

    assert client.stock_sheet_key == "Нове Імʼя"


async def test_stock_sheet_key_unchanged_when_rename_fails(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await _active_client(db_session, telegram_id=901)
    monkeypatch.setattr(SheetsClient, "_authorize", lambda self: _FakeGc(fail_rename=True))

    await sync_client_sheets(
        db_session, client=client, reader=_FakeReader(), settings=_sheets_settings()
    )

    # Переименование упало → ключ остаётся прежним, чтобы PG указывал на реальный лист.
    assert client.stock_sheet_key == "old_key"


async def test_account_sync_ignores_user_scoped_previous_key(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Регрессия: `_sync_client_sheets_sync` переименовывает вкладку
    # `previous_sheet_key` → `target_key`. `update_self_profile` передаёт туда ключ
    # ПОЛЬЗОВАТЕЛЯ, а `target_key` в account-ветке — имя АКАУНТА. Правка ПІБ
    # работником увела бы вкладку с его именем в имя общего склада.
    import app.services.client_sheet_sync as css

    client = await _active_client(db_session, telegram_id=902)
    account = await ClientAccountRepository(db_session).get_membership(user_id=client.id)
    account = account.account
    account.name = "Магазин"
    account.stock_sheet_key = "Магазин"
    await db_session.flush()
    seen: dict = {}

    def fake_rename(gc, settings, source, target):
        seen["source"] = source
        seen["target"] = target
        return True

    monkeypatch.setattr(css, "_rename_main_worksheets", fake_rename)
    monkeypatch.setattr(SheetsClient, "_authorize", lambda self: _FakeGc(fail_rename=False))

    await sync_client_sheets(
        db_session,
        client=client,
        account=account,
        previous_sheet_key="Іван Працівник",  # ключ работника — не должен участвовать
        reader=_FakeReader(),
        settings=_sheets_settings(),
    )

    assert seen["source"] == "Магазин"  # не «Іван Працівник»
    assert seen["target"] == "Магазин"
    assert account.stock_sheet_key == "Магазин"


async def test_account_sync_whitespace_name_falls_back_to_id(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `account.name or str(account.id)` пропускал имя из пробелов в имя вкладки.
    import app.services.client_sheet_sync as css

    client = await _active_client(db_session, telegram_id=903)
    membership = await ClientAccountRepository(db_session).get_membership(user_id=client.id)
    account = membership.account
    account.name = "   "
    account.stock_sheet_key = None
    await db_session.flush()
    seen: dict = {}

    monkeypatch.setattr(
        css,
        "_rename_main_worksheets",
        lambda gc, s, source, target: seen.update(target=target) or True,
    )
    monkeypatch.setattr(SheetsClient, "_authorize", lambda self: _FakeGc(fail_rename=False))

    await sync_client_sheets(
        db_session,
        client=client,
        account=account,
        reader=_FakeReader(),
        settings=_sheets_settings(),
    )

    assert seen["target"] == str(account.id)


async def test_view_book_not_created_at_runtime(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await _active_client(db_session, telegram_id=902)
    monkeypatch.setattr(SheetsClient, "_authorize", lambda self: _FakeGc(fail_rename=False))

    # _FakeGc.create() ассертит, если рантайм попытается создать книгу.
    await sync_client_sheets(
        db_session, client=client, reader=_FakeReader(), settings=_sheets_settings()
    )

    assert client.stock_view_book_id is None


def _view_row(**over) -> ViewRow:
    base = {
        "sku": "SKU-1",
        "name": "Кава",
        "category": "Напої",
        "price": Decimal("125.50"),
        "stock": 7,
        "reserved": 2,
        "available": 5,
    }
    return ViewRow(**{**base, **over})


def test_view_headers_match_sklad_order():
    # Порядок = как в основной книге «Склад»: D=Кількість, E=Ціна, F=Резерв, G=Доступно.
    # От этого зависит переиспользование форматирования/pivot провижна (build_view_summary,
    # style_stock_worksheet зашиты под этот порядок).
    assert _VIEW_HEADERS == [
        "Артикул",
        "Назва",
        "Категорія",
        "Кількість",
        "Ціна",
        "Резерв",
        "Доступно",
    ]


def test_view_data_row_order_and_types():
    # A–F: Артикул, Назва, Категорія, Кількість, Ціна, Резерв. «Доступно» (G) — формула.
    assert _view_data_row(_view_row()) == ["SKU-1", "Кава", "Напої", 7, 125.5, 2]
    # Цена — число (float), не строка: comma-локаль книги иначе исказит "125.50".
    assert isinstance(_view_data_row(_view_row())[4], float)


def test_view_data_row_handles_missing_price_and_category():
    assert _view_data_row(_view_row(price=None, category=None)) == ["SKU-1", "Кава", "", 7, "", 2]
