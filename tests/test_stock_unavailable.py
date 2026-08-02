"""Недоступность источника остатков: трансляция APIError, ретраи, отсутствие подмены.

Ключевое: `StockSourceUnavailable` НЕ должен вырождаться в пустой список. Пустой
список рисует экран, где у всех товаров «0 шт» и `🚫` — то есть выглядит как
настоящий склад, и клиент принимает решения по правдоподобной неправде. Ровно так
владелец и описал симптом: «на першій сторінці немає товарів у наявності».
"""

from __future__ import annotations

import pytest
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import ClientAccountRepository, UserRepository
from app.services.inventory import get_inventory_snapshot, stock_totals
from app.sheets.client import SheetsClient, _api_error_status
from app.sheets.runtime import _retryable, run_sheets_read
from app.sheets.source import StockSheetNotFound, StockSourceUnavailable
from sqlalchemy.ext.asyncio import AsyncSession


class _ApiError(Exception):
    """Форма `gspread.exceptions.APIError`: несёт response со status_code."""

    def __init__(self, status: int) -> None:
        super().__init__(f"API error {status}")
        self.response = type("R", (), {"status_code": status})()


def test_api_error_status_extracted():
    assert _api_error_status(_ApiError(429)) == 429
    assert _api_error_status(RuntimeError("нет response")) is None


def test_read_rows_translates_api_error(monkeypatch):
    """Границу с gspread держим в `SheetsClient` — выше о нём знать не должны."""
    import gspread.exceptions

    monkeypatch.setattr(gspread.exceptions, "APIError", _ApiError)

    class _WS:
        def get_all_records(self, **kwargs):
            raise _ApiError(429)

    client = SheetsClient.__new__(SheetsClient)
    monkeypatch.setattr(client, "get_stock_worksheet", lambda key: _WS())

    with pytest.raises(StockSourceUnavailable) as excinfo:
        client.read_rows("Магазин")
    assert excinfo.value.status == 429
    assert excinfo.value.client_key == "Магазин"


@pytest.mark.parametrize("status", [429, 500, 502, 503, None])
def test_transient_statuses_are_retryable(status):
    assert _retryable(StockSourceUnavailable("Магазин", status)) is True


@pytest.mark.parametrize("status", [400, 403, 404])
def test_permanent_statuses_are_not_retryable(status):
    """403 (нет доступа к книге) ретраить бессмысленно — только жечь квоту."""
    assert _retryable(StockSourceUnavailable("Магазин", status)) is False


def test_missing_sheet_is_not_retryable():
    """«Листа нет» — ожидаемое состояние, а не сбой: ретраи тут не при чём."""
    assert _retryable(StockSheetNotFound("Магазин")) is False


async def test_read_retries_then_succeeds(monkeypatch):
    monkeypatch.setenv("SHEETS_RETRY_BACKOFF", "0")
    from app.config import get_settings

    get_settings.cache_clear()
    calls = {"n": 0}

    def flaky(key: str) -> list:
        calls["n"] += 1
        if calls["n"] < 3:
            raise StockSourceUnavailable(key, 429)
        return ["ok"]

    assert await run_sheets_read(flaky, "Магазин") == ["ok"]
    assert calls["n"] == 3


async def test_read_gives_up_after_attempts(monkeypatch):
    monkeypatch.setenv("SHEETS_RETRY_BACKOFF", "0")
    monkeypatch.setenv("SHEETS_RETRY_ATTEMPTS", "2")
    from app.config import get_settings

    get_settings.cache_clear()
    calls = {"n": 0}

    def always_429(key: str) -> list:
        calls["n"] += 1
        raise StockSourceUnavailable(key, 429)

    with pytest.raises(StockSourceUnavailable):
        await run_sheets_read(always_429, "Магазин")
    assert calls["n"] == 2  # не бесконечно


async def test_permanent_error_is_not_retried(monkeypatch):
    monkeypatch.setenv("SHEETS_RETRY_BACKOFF", "0")
    from app.config import get_settings

    get_settings.cache_clear()
    calls = {"n": 0}

    def forbidden(key: str) -> list:
        calls["n"] += 1
        raise StockSourceUnavailable(key, 403)

    with pytest.raises(StockSourceUnavailable):
        await run_sheets_read(forbidden, "Магазин")
    assert calls["n"] == 1


class _UnavailableSource:
    def read_stock(self, client_key: str):
        raise StockSourceUnavailable(client_key, 429)


async def _account_owner(session: AsyncSession, telegram_id: int):
    owner = await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=f"+38099000{telegram_id}",
        full_name="Магазин",
        role=UserRole.client,
        status=UserStatus.active,
        account_name="Магазин",
    )
    membership = await ClientAccountRepository(session).get_membership(user_id=owner.id)
    membership.account.stock_sheet_key = "Магазин"
    await session.flush()
    return owner, membership.account


async def test_snapshot_propagates_unavailable_instead_of_empty_stock(
    db_session: AsyncSession, monkeypatch
):
    """Главное: недоступность не превращается в «склад порожній»."""
    monkeypatch.setenv("SHEETS_RETRY_ATTEMPTS", "1")
    from app.config import get_settings

    get_settings.cache_clear()
    owner, account = await _account_owner(db_session, 8400)

    with pytest.raises(StockSourceUnavailable):
        await get_inventory_snapshot(
            db_session, client=owner, account=account, reader=_UnavailableSource()
        )


async def test_manager_summary_still_swallows_unavailable(db_session: AsyncSession, monkeypatch):
    """А вот сводка менеджера по всем аккаунтам падать целиком не должна —
    один недоступный лист даёт `None` в своей строке."""
    monkeypatch.setenv("SHEETS_RETRY_ATTEMPTS", "1")
    from app.config import get_settings

    get_settings.cache_clear()
    _, account = await _account_owner(db_session, 8401)

    assert await stock_totals(db_session, account, reader=_UnavailableSource()) is None
