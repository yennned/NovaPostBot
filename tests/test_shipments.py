"""Тесты отправлений/резервов Фазы 3."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.db.models.enums import ShipmentStatus, UserRole, UserStatus
from app.db.repositories import (
    ClientAccountRepository,
    ShipmentItemDraft,
    ShipmentRepository,
    UserRepository,
)
from app.services import shipments
from app.services.exceptions import ShipmentActionForbidden
from app.sheets.inventory import StockRow
from sqlalchemy.ext.asyncio import AsyncSession


async def _active_client(session: AsyncSession, telegram_id: int = 100):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=f"+3800{telegram_id}",
        full_name=f"Client {telegram_id}",
        role=UserRole.client,
        status=UserStatus.active,
    )


async def test_shipment_persists_size_preset_and_weight(db_session: AsyncSession):
    client = await _active_client(db_session, telegram_id=140)
    repo = ShipmentRepository(db_session)
    created = await repo.create(
        client_id=client.id,
        recipient_name="Іван",
        ttn_number="TTN-W",
        size_preset="mala",
        weight=Decimal("2.5"),
        items=[ShipmentItemDraft(sku="SKU-1", name="Товар", quantity=1)],
    )
    fetched = await repo.get_by_id(created.id)
    assert fetched.size_preset == "mala"
    assert fetched.weight == Decimal("2.500")  # Numeric(8,3)


async def test_shipment_repository_reserves_only_open_shipments(db_session: AsyncSession):
    client = await _active_client(db_session)
    repo = ShipmentRepository(db_session)

    await repo.create(
        client_id=client.id,
        recipient_name="Іван",
        ttn_number="TTN-1",
        status=ShipmentStatus.created,
        items=[ShipmentItemDraft(sku="SKU-1", name="Товар 1", quantity=2)],
    )
    await repo.create(
        client_id=client.id,
        recipient_name="Петро",
        ttn_number="TTN-2",
        status=ShipmentStatus.confirmed,
        items=[ShipmentItemDraft(sku="SKU-1", name="Товар 1", quantity=1)],
    )
    await repo.create(
        client_id=client.id,
        recipient_name="Марія",
        ttn_number="TTN-3",
        status=ShipmentStatus.dispatched,
        items=[ShipmentItemDraft(sku="SKU-1", name="Товар 1", quantity=9)],
    )

    reserved = await repo.reserved_by_sku(client.id)

    assert reserved == {"SKU-1": 3}
    found = await repo.get_by_ttn_number("TTN-2")
    assert found is not None
    assert found.recipient_name == "Петро"


async def test_shipments_service_lists_bucket_and_card(db_session: AsyncSession):
    client = await _active_client(db_session)
    repo = ShipmentRepository(db_session)
    shipment = await repo.create(
        client_id=client.id,
        recipient_name="Іван",
        recipient_city="Київ",
        recipient_warehouse="Відділення 1",
        ttn_number="TTN-42",
        payment_method="cod",
        payer_type="recipient",
        cod_amount=Decimal("1200.00"),
        status=ShipmentStatus.created,
        items=[ShipmentItemDraft(sku="SKU-42", name="Товар 42", quantity=4)],
    )

    page = await shipments.list_shipments(db_session, client=client, bucket="created")
    assert page.total == 1
    assert page.items[0].ttn_number == "TTN-42"

    card = await shipments.get_shipment_card(db_session, client=client, shipment_id=shipment.id)
    assert card.recipient_city == "Київ"
    assert card.items[0].sku == "SKU-42"
    assert card.can_cancel is True


async def test_cancel_shipment_marks_status_and_blocks_repeat(db_session: AsyncSession):
    client = await _active_client(db_session, telegram_id=150)
    repo = ShipmentRepository(db_session)
    shipment = await repo.create(
        client_id=client.id,
        recipient_name="Іван",
        ttn_number="TTN-CANCEL",
        status=ShipmentStatus.created,
        items=[ShipmentItemDraft(sku="SKU-C", name="Товар", quantity=1)],
    )

    cancelled = await shipments.cancel_shipment(db_session, client=client, shipment_id=shipment.id)

    assert cancelled.status is ShipmentStatus.cancelled
    assert cancelled.can_cancel is False
    reloaded = await repo.get_by_id(shipment.id)
    assert reloaded is not None
    assert reloaded.status is ShipmentStatus.cancelled

    with pytest.raises(ShipmentActionForbidden):
        await shipments.cancel_shipment(db_session, client=client, shipment_id=shipment.id)


async def test_stats_snapshot_aggregates_by_status(db_session: AsyncSession, monkeypatch):
    from app.services import stats

    client = await _active_client(db_session, telegram_id=200)
    repo = ShipmentRepository(db_session)
    now = datetime.now(UTC)

    await repo.create(
        client_id=client.id,
        recipient_name="Іван",
        status=ShipmentStatus.dispatched,
        status_changed_at=now,
        items=[ShipmentItemDraft(sku="SKU-A", name="A", quantity=5)],
    )
    await repo.create(
        client_id=client.id,
        recipient_name="Петро",
        status=ShipmentStatus.returned,
        status_changed_at=now,
        items=[ShipmentItemDraft(sku="SKU-A", name="A", quantity=2)],
    )
    await repo.create(
        client_id=client.id,
        recipient_name="Марія",
        status=ShipmentStatus.lost,
        status_changed_at=now,
        items=[ShipmentItemDraft(sku="SKU-B", name="B", quantity=1)],
    )

    class FakeReader:
        def read_stock(self, client_key: str):
            return [
                StockRow(
                    sku="SKU-A",
                    name="A",
                    category=None,
                    quantity=10,
                    price=Decimal("50"),
                ),
                StockRow(
                    sku="SKU-B",
                    name="B",
                    category=None,
                    quantity=3,
                    price=Decimal("70"),
                ),
            ]

    snapshot = await stats.get_client_stats(
        db_session,
        client=client,
        period="today",
        reader=FakeReader(),
    )

    assert snapshot.shipped_qty == 5
    assert snapshot.returns_qty == 2
    assert snapshot.losses_qty == 1
    assert snapshot.net_sales_qty == 2
    assert snapshot.total_available == 13
    assert snapshot.top_skus[0].sku == "SKU-A"


async def test_date_search_works_on_account_scope(db_session: AsyncSession):
    # Регрессия: поиск отправлений был реализован ТРИЖДЫ, и account-ветка потеряла
    # разбор даты. Акаунт есть у каждого клиента, значит хендлер всегда передаёт
    # `account_id` → поиск по дате в «Мої відправлення» молча не находил ничего.
    client = await _active_client(db_session, telegram_id=9100)
    membership = await ClientAccountRepository(db_session).get_membership(user_id=client.id)
    assert membership is not None
    await ShipmentRepository(db_session).create(
        client_id=client.id,
        account_id=membership.account_id,
        recipient_name="Іван",
        ttn_number="TTN-DATE",
        items=[ShipmentItemDraft(sku="S1", name="Товар", quantity=1)],
    )
    today = datetime.now(UTC).strftime("%d.%m.%Y")

    page = await shipments.list_shipments(
        db_session, client=client, account_id=membership.account_id, query=today
    )
    assert page.total == 1

    # Легаси-путь (клиент без акаунта) не должен пострадать.
    legacy = await shipments.list_shipments(db_session, client=client, query=today)
    assert legacy.total == 1
