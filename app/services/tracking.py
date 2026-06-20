"""Трекинг НП, SLA-флаги и списание складских остатков."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models.enums import ShipmentStatus, StockMovementType
from app.db.models.shipment import Shipment
from app.db.repositories import AuditRepository, ShipmentRepository, StockMovementRepository
from app.novaposhta import methods
from app.novaposhta.client import NovaPoshtaClient
from app.novaposhta.schemas import TrackingStatus
from app.novaposhta.tracking import map_tracking_status
from app.services import notifications
from app.services.inventory import stock_sheet_key
from app.services.notifications import Notifier
from app.sheets.inventory import InventorySheetMutator, StockDelta
from app.utils.sla import sla_met

NONSTANDARD_STATUSES = {
    ShipmentStatus.returning,
    ShipmentStatus.returned,
    ShipmentStatus.lost,
    ShipmentStatus.damaged,
}


@dataclass(frozen=True, slots=True)
class TrackingPollResult:
    checked: int
    updated: int
    notified: int


async def poll_shipments(
    session: AsyncSession,
    *,
    np_client: NovaPoshtaClient,
    notifier: Notifier | None = None,
    mutator: InventorySheetMutator | None = None,
    settings: Settings | None = None,
) -> TrackingPollResult:
    repo = ShipmentRepository(session)
    shipments = await repo.list_for_tracking()
    if not shipments:
        return TrackingPollResult(checked=0, updated=0, notified=0)

    by_api_key: dict[str, list[Shipment]] = defaultdict(list)
    for shipment in shipments:
        if shipment.sender_profile is None:
            continue
        by_api_key[shipment.sender_profile.np_api_key].append(shipment)

    checked = 0
    updated = 0
    notified = 0
    for api_key, batch in by_api_key.items():
        numbers = [shipment.ttn_number for shipment in batch if shipment.ttn_number]
        if not numbers:
            continue
        for chunk in _chunked(numbers, size=100):
            checked += len(chunk)
            rows = await methods.get_status_documents(np_client, api_key=api_key, numbers=chunk)
            by_number = {row.number: row for row in rows}
            for shipment in batch:
                if shipment.ttn_number not in by_number:
                    continue
                changed, pushed = await apply_tracking_status(
                    session,
                    shipment=shipment,
                    tracking=by_number[shipment.ttn_number],
                    notifier=notifier,
                    mutator=mutator,
                )
                updated += int(changed)
                notified += int(pushed)
    return TrackingPollResult(checked=checked, updated=updated, notified=notified)


async def apply_tracking_status(
    session: AsyncSession,
    *,
    shipment: Shipment,
    tracking: TrackingStatus,
    notifier: Notifier | None = None,
    mutator: InventorySheetMutator | None = None,
) -> tuple[bool, bool]:
    target_status = map_tracking_status(tracking)
    shipment.tracking_updated_at = datetime.now(UTC)
    if target_status is None or target_status is shipment.status:
        await session.flush()
        return False, False

    repo = ShipmentRepository(session)
    before_status = shipment.status
    await repo.update_status(shipment, target_status)

    if target_status is ShipmentStatus.dispatched:
        shipment.dispatched_at = datetime.now(UTC)
        shipment.sla_met = sla_met(
            dispatched_at=shipment.dispatched_at, deadline=shipment.sla_deadline
        )
        if shipment.sla_met is False:
            shipment.fee_free = True
            shipment.fee_amount = 0
        await _apply_dispatch_stock(session, shipment=shipment, mutator=mutator)

    await AuditRepository(session).log(
        "shipment_tracking_status_updated",
        affected_entity=f"shipment:{shipment.id}",
        before={"status": before_status.value if before_status else None},
        after={
            "status": target_status.value,
            "np_status": tracking.status,
            "np_status_code": tracking.status_code,
        },
    )

    pushed = False
    if notifier is not None:
        await notifications.notify_shipment_status_changed(
            session,
            notifier,
            client=shipment.client,
            shipment=shipment,
        )
        pushed = True
        if target_status in NONSTANDARD_STATUSES:
            await notifications.notify_nonstandard_shipment(
                session,
                notifier,
                client=shipment.client,
                shipment=shipment,
                note=tracking.status,
            )
    return True, pushed


async def _apply_dispatch_stock(
    session: AsyncSession,
    *,
    shipment: Shipment,
    mutator: InventorySheetMutator | None = None,
) -> None:
    repo = ShipmentRepository(session)
    if await repo.movement_exists(shipment.id, StockMovementType.ttn_dispatch):
        return

    (mutator or InventorySheetMutator()).apply_deltas(
        stock_sheet_key(shipment.client),
        [
            StockDelta(
                sku=item.sku,
                quantity_delta=-item.quantity,
                name=item.name,
                category=item.category,
                price=item.unit_price,
            )
            for item in shipment.items
        ],
    )
    movements = StockMovementRepository(session)
    for item in shipment.items:
        await movements.create(
            client_id=shipment.client_id,
            shipment_id=shipment.id,
            sku=item.sku,
            movement_type=StockMovementType.ttn_dispatch,
            quantity_delta=-item.quantity,
            quantity_before=0,
            quantity_after=-item.quantity,
            comment=f"Списання по ТТН {shipment.ttn_number or '—'}",
        )


def _chunked(items: list[str], *, size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
