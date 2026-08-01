"""Тесты inventory-сервиса Фазы 3."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.db.models.enums import ShipmentStatus, UserRole, UserStatus
from app.db.repositories import ShipmentItemDraft, ShipmentRepository, UserRepository
from app.services import inventory
from app.sheets.inventory import StockRow
from app.sheets.source import StockSheetNotFound
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import account_of


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
        account=await account_of(db_session, client),
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
        account=await account_of(db_session, client),
        query="чай",
        reader=FakeReader(),
    )
    assert [item.sku for item in by_query.items] == ["TEA-1"]

    by_category = await inventory.list_inventory(
        db_session,
        client=client,
        account=await account_of(db_session, client),
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
        account=await account_of(db_session, client),
        reader=MissingSheetReader(),
    )
    assert page.total == 0
    assert page.items == []
    assert page.categories == []


def test_stock_view_book_url_none_until_provisioned():
    from types import SimpleNamespace

    account = SimpleNamespace(stock_view_book_id=None)
    assert inventory.stock_view_book_url(account) is None
    account = SimpleNamespace(stock_view_book_id="BOOK123")
    assert inventory.stock_view_book_url(account) == (
        "https://docs.google.com/spreadsheets/d/BOOK123"
    )


async def test_blocked_account_employee_is_told_so_not_shown_empty_stock(
    db_session: AsyncSession,
):
    """Работник заблокированного аккаунта получает отказ, а не лживый пустой склад.

    `clients._transition` гасит `account.status` и статус ВЛАДЕЛЬЦА, но статус
    работника остаётся `active`. Проверки только пользователя не хватало: работник
    проскакивал, `account` приходил `None`, и человек видел «склад порожній» —
    хотя на деле аккаунт заблокирован. Это же и держало User-ветку резолвера
    живой, мешая снести `users.stock_sheet_key`.
    """
    from app.bot.types import ClientAccountContext
    from app.db.repositories import ClientAccountRepository
    from app.services import account_team, clients
    from app.services.exceptions import PermissionDenied

    owner = await _active_client(db_session, telegram_id=380)
    membership = await ClientAccountRepository(db_session).get_membership(user_id=owner.id)
    invited = await account_team.invite_employee(
        db_session,
        context=ClientAccountContext(user=owner, account=membership.account, membership=membership),
        phone="0990000401",
    )
    employee = await UserRepository(db_session).get_by_id(invited.user_id)
    await account_team.activate_employee_contact(
        db_session, user=employee, telegram_id=381, full_name="Працівник"
    )
    manager = await UserRepository(db_session).create(
        telegram_id=382, role=UserRole.manager, status=UserStatus.active
    )
    await clients.block_client(db_session, actor=manager, client_id=owner.id, reason="тест")

    # Мидлварь не отдаёт контекст неактивного аккаунта → account=None.
    assert await ClientAccountRepository(db_session).get_context_for_user(employee.id) is None
    assert employee.status is UserStatus.active, "статус работника блокировка не трогает"

    with pytest.raises(PermissionDenied, match="заблоковано"):
        await inventory.list_inventory(
            db_session, client=employee, account=None, reader=FakeReader()
        )
