"""Manager-side очередь отправлений: список, карточка, действия."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import permissions
from app.db.models.enums import ShipmentStatus, StockMovementType, UserRole
from app.db.models.shipment import Shipment
from app.db.repositories import (
    AuditRepository,
    ShipmentRepository,
    StockMovementRepository,
    UserRepository,
)
from app.novaposhta import methods
from app.novaposhta.client import NovaPoshtaClient
from app.novaposhta.exceptions import NovaPoshtaError, NovaPoshtaNotFound
from app.services import notifications, shipments
from app.services.client_sheet_sync import best_effort_sync
from app.services.exceptions import ShipmentActionForbidden, ShipmentNotFound, TtnCancelFailed
from app.services.notifications import Notifier
from app.services.returns import ReturnDecision, receive_returned_shipment

QUEUE_BUCKETS = {
    "created": {ShipmentStatus.created},
    "confirmed": {ShipmentStatus.confirmed},
    "returns": shipments.RETURN_STATUSES,
}
NONSTANDARD_SOURCE_STATUSES = {
    ShipmentStatus.dispatched,
    ShipmentStatus.in_transit,
    ShipmentStatus.arrived,
    ShipmentStatus.returning,
}
NONSTANDARD_TARGET_STATUSES = {ShipmentStatus.lost, ShipmentStatus.damaged}


@dataclass(frozen=True, slots=True)
class ManagerShipmentListItem:
    id: uuid.UUID
    ttn_number: str | None
    client_name: str | None
    recipient_name: str
    status: ShipmentStatus
    created_at: datetime
    sla_deadline: datetime | None
    sla_state: str
    author_name: str | None = None


@dataclass(frozen=True, slots=True)
class ManagerShipmentPage:
    items: list[ManagerShipmentListItem]
    total: int
    limit: int
    offset: int
    bucket: str
    query: str | None
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class ManagerShipmentCard:
    client_name: str | None
    sender_profile_name: str | None
    shipment: shipments.ShipmentCard
    can_confirm: bool
    can_cancel: bool
    can_receive_return: bool
    can_mark_lost: bool
    can_mark_damaged: bool


def _bucket_statuses(bucket: str) -> set[ShipmentStatus]:
    return QUEUE_BUCKETS.get(bucket, set(ShipmentStatus))


def _sla_state(shipment: Shipment) -> str:
    if shipment.sla_met is True:
        return "вчасно"
    if shipment.sla_met is False:
        return "прострочено"
    if shipment.sla_deadline is None:
        return "—"
    return (
        "прострочено" if datetime.now(UTC) > shipment.sla_deadline.astimezone(UTC) else "встигаємо"
    )


def _to_list_item(shipment: Shipment) -> ManagerShipmentListItem:
    return ManagerShipmentListItem(
        id=shipment.id,
        ttn_number=shipment.ttn_number,
        client_name=shipment.client.full_name if shipment.client else None,
        author_name=shipment.created_by_user.full_name if shipment.created_by_user else None,
        recipient_name=shipment.recipient_name,
        status=shipment.status,
        created_at=shipment.created_at,
        sla_deadline=shipment.sla_deadline,
        sla_state=_sla_state(shipment),
    )


def _to_card(shipment: Shipment) -> ManagerShipmentCard:
    can_mark_nonstandard = shipment.status in NONSTANDARD_SOURCE_STATUSES
    return ManagerShipmentCard(
        client_name=shipment.client.full_name if shipment.client else None,
        sender_profile_name=shipment.sender_profile.name if shipment.sender_profile else None,
        shipment=shipments._to_card(shipment),
        can_confirm=shipment.status is ShipmentStatus.created,
        can_cancel=shipment.status in shipments.CANCELABLE_STATUSES,
        can_receive_return=shipment.status in {ShipmentStatus.returning, ShipmentStatus.returned}
        and not any(
            movement.movement_type == StockMovementType.ttn_return
            for movement in shipment.stock_movements
        ),
        can_mark_lost=can_mark_nonstandard,
        can_mark_damaged=can_mark_nonstandard,
    )


async def list_queue(
    session: AsyncSession,
    *,
    actor,
    bucket: str = "created",
    query: str | None = None,
    limit: int = 8,
    offset: int = 0,
) -> ManagerShipmentPage:
    permissions.require_staff(actor, settings=None)
    repo = ShipmentRepository(session)
    rows, total = await repo.list_for_staff(
        statuses=_bucket_statuses(bucket),
        query=query,
        limit=limit,
        offset=offset,
    )
    counts = await repo.count_by_status_groups(QUEUE_BUCKETS)
    return ManagerShipmentPage(
        items=[_to_list_item(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
        bucket=bucket,
        query=query,
        counts=counts,
    )


async def get_card(
    session: AsyncSession,
    *,
    actor,
    shipment_id: uuid.UUID,
) -> ManagerShipmentCard:
    permissions.require_staff(actor, settings=None)
    shipment = await ShipmentRepository(session).get_by_id(shipment_id)
    if shipment is None:
        raise ShipmentNotFound(str(shipment_id))
    return _to_card(shipment)


async def confirm_shipment(
    session: AsyncSession,
    *,
    actor,
    shipment_id: uuid.UUID,
) -> ManagerShipmentCard:
    permissions.require_staff(actor, settings=None)
    repo = ShipmentRepository(session)
    shipment = await repo.get_by_id(shipment_id)
    if shipment is None:
        raise ShipmentNotFound(str(shipment_id))
    if shipment.status is not ShipmentStatus.created:
        raise ShipmentActionForbidden("confirm", shipment.status)
    before = {"status": shipment.status.value}
    await repo.update_status(shipment, ShipmentStatus.confirmed)
    await AuditRepository(session).log(
        "shipment_confirmed_by_staff",
        user_id=actor.id,
        affected_entity=f"shipment:{shipment.id}",
        before=before,
        after={"status": shipment.status.value},
    )
    return _to_card(shipment)


async def cancel_shipment(
    session: AsyncSession,
    *,
    actor,
    shipment_id: uuid.UUID,
    np_client: NovaPoshtaClient,
) -> ManagerShipmentCard:
    permissions.require_staff(actor, settings=None)
    repo = ShipmentRepository(session)
    shipment = await repo.get_by_id(shipment_id)
    if shipment is None:
        raise ShipmentNotFound(str(shipment_id))
    if shipment.status not in shipments.CANCELABLE_STATUSES:
        raise ShipmentActionForbidden("cancel", shipment.status)
    if shipment.np_ref and shipment.sender_profile is not None:
        try:
            await methods.delete_ttn(
                np_client,
                api_key=shipment.sender_profile.np_api_key,
                doc_ref=shipment.np_ref,
            )
        except NovaPoshtaNotFound:
            pass
        except NovaPoshtaError as exc:
            raise TtnCancelFailed(str(exc)) from exc
    before = {"status": shipment.status.value}
    await repo.update_status(shipment, ShipmentStatus.cancelled)
    await StockMovementRepository(session).record_for_items(
        client_id=shipment.client_id,
        account_id=shipment.account_id,
        shipment_id=shipment.id,
        actor_user_id=actor.id,
        items=shipment.items,
        movement_type=StockMovementType.ttn_cancel,
        sign=1,
        comment=f"Скасування менеджером ТТН {shipment.ttn_number or '—'}",
    )
    await AuditRepository(session).log(
        "shipment_cancelled_by_staff",
        user_id=actor.id,
        affected_entity=f"shipment:{shipment.id}",
        before=before,
        after={"status": shipment.status.value},
    )
    await best_effort_sync(
        session,
        client=shipment.client,
        account=shipment.account,
        log_key="manager_cancel_sheet_sync_failed",
        shipment_id=str(shipment.id),
    )
    return _to_card(shipment)


async def receive_return(
    session: AsyncSession,
    *,
    actor,
    shipment_id: uuid.UUID,
    decisions: list[ReturnDecision] | None = None,
) -> ManagerShipmentCard:
    repo = ShipmentRepository(session)
    shipment = await repo.get_by_id(shipment_id)
    if shipment is None:
        raise ShipmentNotFound(str(shipment_id))
    user = await UserRepository(session).get_by_id(shipment.client_id)
    if user is None or user.role is not UserRole.client:
        raise ShipmentNotFound(str(shipment_id))
    permissions.require_can_manage(actor, user, permissions.CAN_MANAGE_CLIENTS, settings=None)
    await receive_returned_shipment(
        session,
        shipment_id=shipment_id,
        actor_user_id=actor.id,
        decisions=decisions,
    )
    shipment = await repo.get_by_id(shipment_id)
    if shipment is None:
        raise ShipmentNotFound(str(shipment_id))
    return _to_card(shipment)


async def mark_nonstandard(
    session: AsyncSession,
    *,
    actor,
    shipment_id: uuid.UUID,
    status: ShipmentStatus,
) -> ManagerShipmentCard:
    permissions.require_staff(actor, settings=None)
    if status not in NONSTANDARD_TARGET_STATUSES:
        raise ShipmentActionForbidden("mark_nonstandard", status)
    repo = ShipmentRepository(session)
    shipment = await repo.get_by_id(shipment_id)
    if shipment is None:
        raise ShipmentNotFound(str(shipment_id))
    if shipment.status not in NONSTANDARD_SOURCE_STATUSES:
        raise ShipmentActionForbidden(f"mark_{status.value}", shipment.status)
    before = {"status": shipment.status.value}
    await repo.update_status(shipment, status)
    await AuditRepository(session).log(
        "shipment_marked_nonstandard_by_staff",
        user_id=actor.id,
        affected_entity=f"shipment:{shipment.id}",
        before=before,
        after={"status": shipment.status.value},
        notes=f"manual_status={status.value}",
    )
    return _to_card(shipment)


async def notify_client_about_status(
    session: AsyncSession,
    notifier: Notifier,
    *,
    shipment_id: uuid.UUID,
) -> None:
    shipment = await ShipmentRepository(session).get_by_id(shipment_id)
    if shipment is None or shipment.client is None:
        raise ShipmentNotFound(str(shipment_id))
    await notifications.notify_shipment_status_changed(
        session,
        notifier,
        client=shipment.client,
        shipment=shipment,
    )


async def notify_client_about_nonstandard(
    session: AsyncSession,
    notifier: Notifier,
    *,
    shipment_id: uuid.UUID,
) -> None:
    shipment = await ShipmentRepository(session).get_by_id(shipment_id)
    if shipment is None or shipment.client is None:
        raise ShipmentNotFound(str(shipment_id))
    note = {
        ShipmentStatus.lost: "Менеджер позначив відправлення як втрачене.",
        ShipmentStatus.damaged: "Менеджер позначив відправлення як пошкоджене.",
    }.get(shipment.status)
    await notifications.notify_nonstandard_shipment(
        session,
        notifier,
        client=shipment.client,
        shipment=shipment,
        note=note,
    )
