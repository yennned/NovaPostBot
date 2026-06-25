"""Тесты best-effort синка клиентских Sheets (PR #49 follow-up).

Главный инвариант: `stock_sheet_key` в PG продвигается на новое имя ТОЛЬКО когда
переименование вкладки в «Складі» подтверждено. Если `update_title` упал — ключ
остаётся старым, иначе снапшот искал бы лист с именем, которого нет → пустой склад.
"""

from __future__ import annotations

import pytest
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import UserRepository
from app.services.client_sheet_sync import sync_client_sheets
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
