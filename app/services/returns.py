"""Сервис возвратов и проблемных отправлений."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import ShipmentStatus, StockMovementType
from app.db.models.shipment import Shipment
from app.db.repositories import AuditRepository, ShipmentRepository, StockMovementRepository
from app.services.client_sheet_sync import best_effort_sync, run_on_sheets_executor
from app.services.exceptions import InvalidReturnDecision, ShipmentActionForbidden, ShipmentNotFound
from app.services.inventory import stock_sheet_key
from app.sheets import StockDelta, StockSource, build_stock_source


@dataclass(frozen=True, slots=True)
class ReturnDecision:
    sku: str
    accepted_quantity: int
    rejected_quantity: int = 0
    comment: str | None = None


def _normalize_return_decisions(
    shipment: Shipment,
    decisions: list[ReturnDecision] | None,
) -> list[ReturnDecision]:
    if decisions is None:
        return [
            ReturnDecision(sku=item.sku, accepted_quantity=item.quantity) for item in shipment.items
        ]

    by_sku = {item.sku: item for item in shipment.items}
    aggregated: dict[str, ReturnDecision] = {}
    for decision in decisions:
        if decision.sku not in by_sku:
            raise InvalidReturnDecision(f"невідомий SKU у поверненні: {decision.sku}")
        if decision.accepted_quantity < 0 or decision.rejected_quantity < 0:
            raise InvalidReturnDecision(f"кількість не може бути відʼємною: {decision.sku}")
        current = aggregated.get(decision.sku)
        if current is None:
            aggregated[decision.sku] = decision
            continue
        aggregated[decision.sku] = ReturnDecision(
            sku=decision.sku,
            accepted_quantity=current.accepted_quantity + decision.accepted_quantity,
            rejected_quantity=current.rejected_quantity + decision.rejected_quantity,
            comment=decision.comment or current.comment,
        )

    normalized = list(aggregated.values())
    for decision in normalized:
        shipped_qty = by_sku[decision.sku].quantity
        if decision.accepted_quantity + decision.rejected_quantity > shipped_qty:
            raise InvalidReturnDecision(
                f"повернення {decision.sku} перевищує кількість у ТТН: {shipped_qty}"
            )
    return normalized


async def receive_returned_shipment(
    session: AsyncSession,
    *,
    shipment_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
    decisions: list[ReturnDecision] | None = None,
    mutator: StockSource | None = None,
) -> None:
    repo = ShipmentRepository(session)
    shipment = await repo.get_by_id(shipment_id)
    if shipment is None:
        raise ShipmentNotFound(str(shipment_id))
    if shipment.status not in {ShipmentStatus.returning, ShipmentStatus.returned}:
        raise ShipmentActionForbidden("return_receive", shipment.status)
    if await repo.movement_exists(shipment.id, StockMovementType.ttn_return):
        return

    by_sku = {item.sku: item for item in shipment.items}
    actual = _normalize_return_decisions(shipment, decisions)
    deltas: list[StockDelta] = []
    for decision in actual:
        item = by_sku.get(decision.sku)
        if item is None or decision.accepted_quantity <= 0:
            continue
        deltas.append(
            StockDelta(
                sku=item.sku,
                quantity_delta=decision.accepted_quantity,
                name=item.name,
                category=item.category,
                price=item.unit_price,
            )
        )
    await run_on_sheets_executor(
        (mutator or build_stock_source()).apply_deltas,
        stock_sheet_key(shipment.account or shipment.client),
        deltas,
    )

    movements = StockMovementRepository(session)
    accepted_total = 0
    rejected_total = 0
    for decision in actual:
        item = by_sku.get(decision.sku)
        if item is None:
            continue
        accepted_total += max(decision.accepted_quantity, 0)
        rejected_total += max(decision.rejected_quantity, 0)
        if decision.accepted_quantity <= 0:
            continue
        await movements.create(
            client_id=shipment.client_id,
            account_id=shipment.account_id,
            shipment_id=shipment.id,
            actor_user_id=actor_user_id,
            sku=item.sku,
            movement_type=StockMovementType.ttn_return,
            quantity_delta=decision.accepted_quantity,
            quantity_before=0,
            quantity_after=decision.accepted_quantity,
            comment=decision.comment or f"Повернення по ТТН {shipment.ttn_number or '—'}",
        )

    before = {"status": shipment.status.value}
    shipment.status = ShipmentStatus.returned
    await session.flush()
    await AuditRepository(session).log(
        "shipment_return_received",
        user_id=actor_user_id,
        affected_entity=f"shipment:{shipment.id}",
        before=before,
        after={
            "status": shipment.status.value,
            "items": len(actual),
            "accepted_quantity": accepted_total,
            "rejected_quantity": rejected_total,
        },
    )
    await best_effort_sync(
        session,
        client=shipment.client,
        account=shipment.account,
        log_key="return_sheet_sync_failed",
        shipment_id=str(shipment.id),
    )
