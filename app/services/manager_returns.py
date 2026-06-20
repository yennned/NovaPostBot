"""Manager-side read/write сценарии возвратных отправлений клиента."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import ShipmentStatus, StockMovementType, UserRole
from app.db.repositories import ShipmentRepository, UserRepository
from app.services import clients, shipments
from app.services.exceptions import ClientNotFound, ShipmentNotFound
from app.services.returns import receive_returned_shipment

RETURN_STATUSES = {ShipmentStatus.returning, ShipmentStatus.returned}


@dataclass(frozen=True, slots=True)
class ManagerReturnListItem:
    id: uuid.UUID
    ttn_number: str | None
    recipient_name: str
    status: ShipmentStatus
    items_count: int
    can_receive: bool


@dataclass(frozen=True, slots=True)
class ManagerReturnPage:
    client_id: uuid.UUID
    client_name: str | None
    items: list[ManagerReturnListItem]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class ManagerReturnCard:
    client_id: uuid.UUID
    client_name: str | None
    shipment: shipments.ShipmentCard
    can_receive: bool


async def list_client_returns(
    session: AsyncSession,
    *,
    actor,
    client_id: uuid.UUID,
    limit: int = 8,
    offset: int = 0,
) -> ManagerReturnPage:
    client = await _client_for_staff(session, actor=actor, client_id=client_id)
    rows, total = await ShipmentRepository(session).get_by_client_and_status(
        client.id,
        statuses=RETURN_STATUSES,
        limit=limit,
        offset=offset,
    )
    repo = ShipmentRepository(session)
    items = [
        ManagerReturnListItem(
            id=row.id,
            ttn_number=row.ttn_number,
            recipient_name=row.recipient_name,
            status=row.status,
            items_count=sum(item.quantity for item in row.items),
            can_receive=not await repo.movement_exists(row.id, StockMovementType.ttn_return),
        )
        for row in rows
    ]
    return ManagerReturnPage(
        client_id=client.id,
        client_name=client.full_name,
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_return_card(
    session: AsyncSession,
    *,
    actor,
    shipment_id: uuid.UUID,
) -> ManagerReturnCard:
    clients._require_staff(actor, settings=None)
    repo = ShipmentRepository(session)
    shipment = await repo.get_by_id(shipment_id)
    if shipment is None or shipment.status not in RETURN_STATUSES:
        raise ShipmentNotFound(str(shipment_id))
    client = await _client_for_staff(session, actor=actor, client_id=shipment.client_id)
    return ManagerReturnCard(
        client_id=client.id,
        client_name=client.full_name,
        shipment=shipments._to_card(shipment),
        can_receive=not await repo.movement_exists(shipment.id, StockMovementType.ttn_return),
    )


async def mark_return_received(
    session: AsyncSession,
    *,
    actor,
    shipment_id: uuid.UUID,
) -> ManagerReturnCard:
    repo = ShipmentRepository(session)
    shipment = await repo.get_by_id(shipment_id)
    if shipment is None:
        raise ShipmentNotFound(str(shipment_id))
    await _client_for_staff(
        session,
        actor=actor,
        client_id=shipment.client_id,
        require_manage=True,
    )
    await receive_returned_shipment(
        session,
        shipment_id=shipment_id,
        actor_user_id=actor.id,
    )
    return await get_return_card(session, actor=actor, shipment_id=shipment_id)


async def _client_for_staff(
    session: AsyncSession,
    *,
    actor,
    client_id: uuid.UUID,
    require_manage: bool = False,
):
    clients._require_staff(actor, settings=None)
    user = await UserRepository(session).get_by_id(client_id)
    if user is None or user.role is not UserRole.client:
        raise ClientNotFound(str(client_id))
    if require_manage:
        clients._require_can_manage(actor, user, clients.CAN_MANAGE_CLIENTS, settings=None)
    return user
