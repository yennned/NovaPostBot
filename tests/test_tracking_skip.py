"""Пропущенный `dispatched`: НП отдала сразу более поздний статус.

Мы опрашиваем НП не непрерывно, а НП обновляет трек с задержкой. Воркер может
лежать, а до правки расписания он ещё и молчал два дня из семи. За такой перерыв
посылка успевает и уехать, и доехать — и следующий опрос видит сразу `in_transit`
или `arrived`, а `dispatched` не видит никогда.

Прежнее условие `if target_status is ShipmentStatus.dispatched` на такой скачок
не срабатывало вовсе. Последствия — три, и все тихие:

1. **остаток не списывался** (`_apply_dispatch_stock` в той же ветке) — товар
   уехал, а склад считает его лежащим на полке;
2. `dispatched_at` не проставлялся, и ТТН выпадала из отчётов целиком;
3. вердикт SLA не выносился вообще.

Держалось это на legacy-ветке отчётов `OR (dispatched_at IS NULL AND status IN
(…))`: она подбирала такие строки по `status_changed_at` и тем прятала первые
два следствия. Ветка снята вместе с бэкфилом — дыра стала видна.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.db.models.enums import ShipmentStatus, StockMovementType, UserRole, UserStatus
from app.db.repositories import ShipmentItemDraft, ShipmentRepository, UserRepository
from app.novaposhta.schemas import TrackingStatus
from app.services.tracking import apply_tracking_status
from sqlalchemy.ext.asyncio import AsyncSession


class _Mutator:
    def __init__(self) -> None:
        self.calls: list[list[tuple[str, int]]] = []

    def apply_deltas(self, client_key: str, deltas) -> None:
        self.calls.append([(delta.sku, delta.quantity_delta) for delta in deltas])


async def _confirmed(session: AsyncSession, telegram_id: int, ttn: str):
    client = await UserRepository(session).create(
        telegram_id=telegram_id,
        full_name=f"Клієнт {telegram_id}",
        role=UserRole.client,
        status=UserStatus.active,
    )
    created = await ShipmentRepository(session).create(
        client_id=client.id,
        recipient_name="Іван",
        ttn_number=ttn,
        status=ShipmentStatus.confirmed,
        items=[ShipmentItemDraft(sku="SKU-1", name="Кава", quantity=2, unit_price=Decimal("100"))],
    )
    created.sla_deadline = datetime.now(UTC) + timedelta(hours=1)
    await session.flush()
    return await ShipmentRepository(session).get_by_id(created.id)


@pytest.mark.parametrize(
    ("np_status", "np_code", "expected"),
    [
        ("В дорозі", "4", ShipmentStatus.in_transit),
        ("Прибув у відділення", "5", ShipmentStatus.arrived),
        ("Вручено", "8", ShipmentStatus.delivered),
    ],
)
async def test_status_jump_past_dispatched_still_counts_as_dispatch(
    db_session: AsyncSession, np_status: str, np_code: str, expected: ShipmentStatus
):
    """Скачок мимо `dispatched` обязан списать остаток и проставить время отправки."""
    shipment = await _confirmed(db_session, 4000 + int(np_code), f"5900{np_code}00")
    mutator = _Mutator()

    changed, _ = await apply_tracking_status(
        db_session,
        shipment=shipment,
        tracking=TrackingStatus(number=shipment.ttn_number, status=np_status, status_code=np_code),
        notifier=None,
        mutator=mutator,
    )

    assert changed is True
    assert shipment.status is expected
    assert shipment.dispatched_at is not None, (
        "ТТН уехала, но времени отправки нет: она выпадает из всех отчётов по периоду"
    )
    assert mutator.calls == [[("SKU-1", -2)]], (
        "остаток не списан: товар уехал, а склад считает его лежащим на полке"
    )
    assert await ShipmentRepository(db_session).movement_exists(
        shipment.id, StockMovementType.ttn_dispatch
    )


async def test_dispatch_side_effects_happen_once(db_session: AsyncSession):
    """Повторный переход между пост-отправочными статусами не списывает второй раз.

    Маркер «ещё не отправляли» — `dispatched_at is None`, а не конкретный статус:
    иначе документ, качнувшийся `dispatched` → `in_transit`, списал бы остаток
    дважды.
    """
    shipment = await _confirmed(db_session, 4100, "59004100")
    mutator = _Mutator()

    for status, code in (("Відправлено", "3"), ("В дорозі", "4"), ("Вручено", "8")):
        await apply_tracking_status(
            db_session,
            shipment=shipment,
            tracking=TrackingStatus(number=shipment.ttn_number, status=status, status_code=code),
            notifier=None,
            mutator=mutator,
        )

    assert shipment.status is ShipmentStatus.delivered
    assert mutator.calls == [[("SKU-1", -2)]], f"остаток списан {len(mutator.calls)} раз(а)"
