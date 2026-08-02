"""Тесты Phase 5: трекинг, списание и возвраты."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from app.db.models.enums import ShipmentStatus, StockMovementType, UserRole, UserStatus
from app.db.repositories import (
    SenderProfileRepository,
    ShipmentItemDraft,
    ShipmentRepository,
    UserRepository,
)
from app.novaposhta.schemas import TrackingStatus
from app.services.exceptions import InvalidReturnDecision
from app.services.notifications import Notifier
from app.services.returns import ReturnDecision, receive_returned_shipment
from app.services.tracking import apply_tracking_status
from sqlalchemy.ext.asyncio import AsyncSession


class FakeNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, telegram_id: int, text: str) -> None:
        self.sent.append((telegram_id, text))


class FakeMutator:
    def __init__(self) -> None:
        self.calls: list[list[tuple[str, int]]] = []

    def apply_deltas(self, client_key: str, deltas) -> None:
        self.calls.append([(delta.sku, delta.quantity_delta) for delta in deltas])


async def _active_client(session: AsyncSession, telegram_id: int = 900):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        full_name=f"Клієнт {telegram_id}",
        role=UserRole.client,
        status=UserStatus.active,
    )


async def test_apply_tracking_status_dispatches_and_marks_sla(
    db_session: AsyncSession,
):
    client = await _active_client(db_session)
    await SenderProfileRepository(db_session).create(
        client_id=client.id,
        name="ФОП",
        np_api_key="np-key",
        np_sender_ref="sender",
        np_contact_ref="contact",
        sender_phone="+380501112233",
        is_default=True,
    )
    created = await ShipmentRepository(db_session).create(
        client_id=client.id,
        recipient_name="Іван",
        ttn_number="59000999",
        status=ShipmentStatus.confirmed,
        items=[ShipmentItemDraft(sku="SKU-1", name="Кава", quantity=2, unit_price=Decimal("100"))],
    )
    created.sla_deadline = datetime.now(UTC) - timedelta(hours=1)
    created.fee_amount = Decimal("21.00")
    await db_session.flush()

    shipment = await ShipmentRepository(db_session).get_by_id(created.id)
    notifier = FakeNotifier()
    mutator = FakeMutator()

    # `DateScan` от НП — единственное, чем промах доказывается однозначно. Без него
    # вердикт был бы `None` («не знаем»), и это правильно: посылку ни разу не
    # опрашивали, поэтому доказать, что она уехала ПОСЛЕ дедлайна, нечем, а списывать
    # комиссию за собственную задержку опроса мы больше не хотим.
    scanned = (datetime.now(UTC) - timedelta(minutes=30)).astimezone(ZoneInfo("Europe/Kyiv"))

    changed, pushed = await apply_tracking_status(
        db_session,
        shipment=shipment,
        tracking=TrackingStatus(
            number="59000999",
            status="Відправлено",
            status_code="3",
            raw={"DateScan": scanned.strftime("%d.%m.%Y %H:%M:%S")},
        ),
        notifier=notifier,
        mutator=mutator,
    )

    assert changed is True
    assert pushed is True
    assert shipment.status is ShipmentStatus.dispatched
    assert shipment.sla_met is False
    assert shipment.fee_free is True
    assert shipment.fee_amount == 0
    assert mutator.calls == [[("SKU-1", -2)]]
    assert any("Оновлення статусу" in text for _, text in notifier.sent)
    assert await ShipmentRepository(db_session).movement_exists(
        shipment.id, StockMovementType.ttn_dispatch
    )


async def test_receive_returned_shipment_restocks_inventory(db_session: AsyncSession):
    client = await _active_client(db_session, telegram_id=901)
    created = await ShipmentRepository(db_session).create(
        client_id=client.id,
        recipient_name="Іван",
        ttn_number="59000888",
        status=ShipmentStatus.returning,
        items=[ShipmentItemDraft(sku="SKU-2", name="Чай", quantity=3, unit_price=Decimal("80"))],
    )
    mutator = FakeMutator()

    await receive_returned_shipment(
        db_session,
        shipment_id=created.id,
        mutator=mutator,
    )

    shipment = await ShipmentRepository(db_session).get_by_id(created.id)
    assert shipment is not None
    assert shipment.status is ShipmentStatus.returned
    assert mutator.calls == [[("SKU-2", 3)]]
    assert await ShipmentRepository(db_session).movement_exists(
        shipment.id, StockMovementType.ttn_return
    )


async def test_receive_returned_shipment_supports_per_item_inspection(db_session: AsyncSession):
    client = await _active_client(db_session, telegram_id=902)
    created = await ShipmentRepository(db_session).create(
        client_id=client.id,
        recipient_name="Іван",
        ttn_number="59000777",
        status=ShipmentStatus.returning,
        items=[
            ShipmentItemDraft(sku="SKU-GOOD", name="Кава", quantity=2, unit_price=Decimal("90")),
            ShipmentItemDraft(sku="SKU-BAD", name="Чай", quantity=1, unit_price=Decimal("80")),
        ],
    )
    mutator = FakeMutator()

    await receive_returned_shipment(
        db_session,
        shipment_id=created.id,
        decisions=[
            ReturnDecision(sku="SKU-GOOD", accepted_quantity=2, rejected_quantity=0),
            ReturnDecision(sku="SKU-BAD", accepted_quantity=0, rejected_quantity=1),
        ],
        mutator=mutator,
    )

    shipment = await ShipmentRepository(db_session).get_by_id(created.id)
    assert shipment is not None
    assert shipment.status is ShipmentStatus.returned
    assert mutator.calls == [[("SKU-GOOD", 2)]]


async def test_receive_returned_shipment_rejects_unknown_sku(db_session: AsyncSession):
    client = await _active_client(db_session, telegram_id=903)
    created = await ShipmentRepository(db_session).create(
        client_id=client.id,
        recipient_name="Іван",
        ttn_number="59000666",
        status=ShipmentStatus.returning,
        items=[ShipmentItemDraft(sku="SKU-2", name="Чай", quantity=3, unit_price=Decimal("80"))],
    )

    with pytest.raises(InvalidReturnDecision):
        await receive_returned_shipment(
            db_session,
            shipment_id=created.id,
            decisions=[ReturnDecision(sku="SKU-404", accepted_quantity=1, rejected_quantity=0)],
            mutator=FakeMutator(),
        )


async def test_receive_returned_shipment_rejects_overstocking_decisions(db_session: AsyncSession):
    client = await _active_client(db_session, telegram_id=904)
    created = await ShipmentRepository(db_session).create(
        client_id=client.id,
        recipient_name="Іван",
        ttn_number="59000555",
        status=ShipmentStatus.returning,
        items=[ShipmentItemDraft(sku="SKU-3", name="Кава", quantity=2, unit_price=Decimal("90"))],
    )

    with pytest.raises(InvalidReturnDecision):
        await receive_returned_shipment(
            db_session,
            shipment_id=created.id,
            decisions=[ReturnDecision(sku="SKU-3", accepted_quantity=2, rejected_quantity=1)],
            mutator=FakeMutator(),
        )


async def test_np_deleted_ttn_releases_reserve_in_ledger(db_session: AsyncSession):
    """ТТН удалили в кабинете НП — бронь снимается и в журнале.

    Все человеческие пути отмены пишут `ttn_cancel`; этот — единственный, где
    статус меняет НП, а не человек. Доступный остаток при этом верен всегда (он
    считается по СТАТУСУ ТТН), поэтому дефект был невидим: нашёлся живым прогоном
    по проду 2026-08-03 — ТТН 20451492663031 от 21.07 держала `ttn_reserve` −8 без
    парного движения. Журнал — то, по чему разбирают «куда делся товар».

    Мутация: убрать вызов `_release_reserve_in_ledger` — сумма движений станет −2.
    """
    from app.db.models.stock_movement import StockMovement
    from app.db.repositories import StockMovementRepository
    from sqlalchemy import select

    client = await _active_client(db_session, telegram_id=931)
    created = await ShipmentRepository(db_session).create(
        client_id=client.id,
        recipient_name="Іван",
        ttn_number="59000931",
        status=ShipmentStatus.confirmed,
        items=[ShipmentItemDraft(sku="SKU-1", name="Кава", quantity=2, unit_price=Decimal("100"))],
    )
    shipment = await ShipmentRepository(db_session).get_by_id(created.id)
    # Бронь, как её пишет создание ТТН.
    await StockMovementRepository(db_session).record_for_items(
        client_id=client.id,
        account_id=shipment.account_id,
        shipment_id=shipment.id,
        actor_user_id=client.id,
        items=shipment.items,
        movement_type=StockMovementType.ttn_reserve,
        sign=-1,
        comment="Резерв під ТТН 59000931",
    )

    deleted = TrackingStatus(number="59000931", status="Видалено", status_code="2", raw={})
    changed, _ = await apply_tracking_status(db_session, shipment=shipment, tracking=deleted)

    assert changed is True
    assert shipment.status is ShipmentStatus.cancelled
    rows = list(
        await db_session.scalars(
            select(StockMovement).where(StockMovement.shipment_id == created.id)
        )
    )
    assert sorted(row.movement_type.value for row in rows) == ["ttn_cancel", "ttn_reserve"]
    assert sum(row.quantity_delta for row in rows) == 0


async def test_np_deleted_ttn_does_not_double_release(db_session: AsyncSession):
    """Повторное «Видалено» второго `ttn_cancel` не пишет.

    Трекинг идемпотентен и видит один и тот же ответ НП сколько угодно раз; второй
    возврат брони завысил бы журнал ровно так же, как его отсутствие занижало.
    """
    from app.db.models.stock_movement import StockMovement
    from app.db.repositories import StockMovementRepository
    from app.services.tracking import _release_reserve_in_ledger
    from sqlalchemy import select

    client = await _active_client(db_session, telegram_id=932)
    created = await ShipmentRepository(db_session).create(
        client_id=client.id,
        recipient_name="Іван",
        ttn_number="59000932",
        status=ShipmentStatus.confirmed,
        items=[ShipmentItemDraft(sku="SKU-1", name="Кава", quantity=2, unit_price=Decimal("100"))],
    )
    shipment = await ShipmentRepository(db_session).get_by_id(created.id)
    # Бронь, как её пишет создание ТТН.
    await StockMovementRepository(db_session).record_for_items(
        client_id=client.id,
        account_id=shipment.account_id,
        shipment_id=shipment.id,
        actor_user_id=client.id,
        items=shipment.items,
        movement_type=StockMovementType.ttn_reserve,
        sign=-1,
        comment="Резерв",
    )

    await _release_reserve_in_ledger(db_session, shipment=shipment)
    await _release_reserve_in_ledger(db_session, shipment=shipment)

    rows = list(
        await db_session.scalars(
            select(StockMovement).where(StockMovement.shipment_id == created.id)
        )
    )
    assert sorted(row.movement_type.value for row in rows) == ["ttn_cancel", "ttn_reserve"]
