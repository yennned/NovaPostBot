"""Трекинг НП, SLA-флаги и списание складских остатков."""

from __future__ import annotations

import asyncio
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
from app.services.client_sheet_sync import best_effort_sync, run_on_sheets_executor
from app.services.inventory import stock_sheet_key
from app.services.notifications import Notifier
from app.sheets import StockDelta, StockSource, build_stock_source
from app.utils.sla import sla_met

NONSTANDARD_STATUSES = {
    ShipmentStatus.returning,
    ShipmentStatus.returned,
    ShipmentStatus.lost,
    ShipmentStatus.damaged,
}

# Потолок конкурентных НП-чтений статусов за один поллинг (по одному httpx-клиенту).
_POLL_FETCH_CONCURRENCY = 8


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
    mutator: StockSource | None = None,
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

    # Спеки чтения: (api_key, batch, chunk из ≤100 номеров) на каждый ФОП.
    fetch_specs: list[tuple[str, list[Shipment], list[str]]] = []
    for api_key, batch in by_api_key.items():
        numbers = [shipment.ttn_number for shipment in batch if shipment.ttn_number]
        for chunk in _chunked(numbers, size=100):
            fetch_specs.append((api_key, batch, chunk))
    if not fetch_specs:
        return TrackingPollResult(checked=0, updated=0, notified=0)

    # Фаза чтения — независимые НП-вызовы конкурентно (общий httpx.AsyncClient
    # потокобезопасен), но с ограничителем, чтобы не завалить API при многих ФОП.
    # TaskGroup: при сбое одного чтения остальные отменяются структурно (без «висящих»
    # задач и «Task exception was never retrieved»).
    sem = asyncio.Semaphore(_POLL_FETCH_CONCURRENCY)

    async def _fetch(api_key: str, chunk: list[str]) -> list[TrackingStatus]:
        async with sem:
            return await methods.get_status_documents(np_client, api_key=api_key, numbers=chunk)

    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(_fetch(api_key, chunk)) for api_key, _, chunk in fetch_specs]
    fetched = [task.result() for task in tasks]

    # Фаза записи — последовательно на общей `AsyncSession` (не потокобезопасна).
    checked = 0
    updated = 0
    notified = 0
    for (_api_key, batch, chunk), rows in zip(fetch_specs, fetched, strict=True):
        checked += len(chunk)
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
    mutator: StockSource | None = None,
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
    mutator: StockSource | None = None,
) -> None:
    repo = ShipmentRepository(session)
    if await repo.movement_exists(shipment.id, StockMovementType.ttn_dispatch):
        return

    await run_on_sheets_executor(
        (mutator or build_stock_source()).apply_deltas,
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
    await StockMovementRepository(session).record_for_items(
        client_id=shipment.client_id,
        shipment_id=shipment.id,
        items=shipment.items,
        movement_type=StockMovementType.ttn_dispatch,
        sign=-1,
        comment=f"Списання по ТТН {shipment.ttn_number or '—'}",
    )
    await best_effort_sync(
        session,
        client=shipment.client,
        log_key="tracking_sheet_sync_failed",
        shipment_id=str(shipment.id),
    )


def _chunked(items: list[str], *, size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
