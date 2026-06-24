"""Тесты inventory-сервиса Фазы 3."""

from __future__ import annotations

from decimal import Decimal

from app.db.models.enums import ShipmentStatus, UserRole, UserStatus
from app.db.repositories import ShipmentItemDraft, ShipmentRepository, UserRepository
from app.services import inventory
from app.sheets.inventory import StockRow
from app.sheets.source import StockSheetNotFound
from sqlalchemy.ext.asyncio import AsyncSession


class FakeReader:
    def read_stock(self, client_key: str):
        return [
            StockRow(
                sku="COF-1",
                name="Кава",
                category="Кава",
                quantity=10,
                price=Decimal("100"),
            ),
            StockRow(
                sku="TEA-1",
                name="Чай",
                category="Чай",
                quantity=5,
                price=Decimal("80"),
            ),
        ]


async def _active_client(session: AsyncSession, telegram_id: int = 300):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        full_name="Вася",
        role=UserRole.client,
        status=UserStatus.active,
    )


async def test_inventory_available_equals_stock_minus_reserved(db_session: AsyncSession):
    client = await _active_client(db_session)
    await ShipmentRepository(db_session).create(
        client_id=client.id,
        recipient_name="Іван",
        status=ShipmentStatus.created,
        items=[ShipmentItemDraft(sku="COF-1", name="Кава", quantity=3)],
    )

    page = await inventory.list_inventory(
        db_session,
        client=client,
        reader=FakeReader(),
    )

    assert page.total == 2
    coffee = next(item for item in page.items if item.sku == "COF-1")
    assert coffee.stock == 10
    assert coffee.reserved == 3
    assert coffee.available == 7


async def test_inventory_search_and_category_filter(db_session: AsyncSession):
    client = await _active_client(db_session, telegram_id=301)

    by_query = await inventory.list_inventory(
        db_session,
        client=client,
        query="чай",
        reader=FakeReader(),
    )
    assert [item.sku for item in by_query.items] == ["TEA-1"]

    by_category = await inventory.list_inventory(
        db_session,
        client=client,
        category="Кава",
        reader=FakeReader(),
    )
    assert [item.sku for item in by_category.items] == ["COF-1"]


class MissingSheetReader:
    """Лист склада клиента отсутствует (ещё не заведён/переименован)."""

    def read_stock(self, client_key: str):
        raise StockSheetNotFound(client_key)


async def test_inventory_missing_sheet_degrades_to_empty(db_session: AsyncSession):
    # Нет листа склада → пустой остаток, а не падение хендлера створення ТТН.
    client = await _active_client(db_session, telegram_id=302)
    page = await inventory.list_inventory(
        db_session,
        client=client,
        reader=MissingSheetReader(),
    )
    assert page.total == 0
    assert page.items == []
    assert page.categories == []
