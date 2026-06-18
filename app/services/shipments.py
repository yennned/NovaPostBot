"""Read-only сервис отправлений клиента (Фаза 3)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import ShipmentStatus, UserRole, UserStatus
from app.db.models.shipment import Shipment, ShipmentItem
from app.db.models.user import User
from app.db.repositories import AuditRepository, ShipmentRepository
from app.services.exceptions import PermissionDenied, ShipmentActionForbidden, ShipmentNotFound

ACTIVE_CLIENT_SHIPMENT_STATUSES = {
    "created": {ShipmentStatus.created},
    "confirmed": {ShipmentStatus.confirmed},
    "returns": {ShipmentStatus.returning, ShipmentStatus.returned},
    "all": set(ShipmentStatus),
}
CANCELABLE_STATUSES = {ShipmentStatus.created, ShipmentStatus.confirmed}


@dataclass(frozen=True, slots=True)
class ShipmentListItemView:
    id: uuid.UUID
    ttn_number: str | None
    recipient_name: str
    status: ShipmentStatus
    created_at: datetime
    items_count: int


@dataclass(frozen=True, slots=True)
class ShipmentPage:
    items: list[ShipmentListItemView]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class ShipmentItemView:
    sku: str
    name: str
    category: str | None
    quantity: int
    unit_price: Decimal | None


@dataclass(frozen=True, slots=True)
class ShipmentCard:
    id: uuid.UUID
    ttn_number: str | None
    recipient_name: str
    recipient_phone: str | None
    recipient_city: str | None
    recipient_warehouse: str | None
    status: ShipmentStatus
    created_at: datetime
    status_changed_at: datetime
    payment_method: str | None
    payer_type: str | None
    cod_amount: Decimal | None
    insured_amount: Decimal | None
    items: list[ShipmentItemView]
    can_cancel: bool


def _require_active_client(client: User) -> None:
    if client.role is not UserRole.client:
        raise PermissionDenied("кабінет доступний тільки клієнту")
    if client.status is not UserStatus.active:
        raise PermissionDenied("кабінет клієнта доступний після підтвердження")


def statuses_for_bucket(bucket: str) -> set[ShipmentStatus]:
    return ACTIVE_CLIENT_SHIPMENT_STATUSES.get(bucket, ACTIVE_CLIENT_SHIPMENT_STATUSES["all"])


def _to_list_item(shipment: Shipment) -> ShipmentListItemView:
    return ShipmentListItemView(
        id=shipment.id,
        ttn_number=shipment.ttn_number,
        recipient_name=shipment.recipient_name,
        status=shipment.status,
        created_at=shipment.created_at,
        items_count=sum(item.quantity for item in shipment.items),
    )


def _to_item_view(item: ShipmentItem) -> ShipmentItemView:
    return ShipmentItemView(
        sku=item.sku,
        name=item.name,
        category=item.category,
        quantity=item.quantity,
        unit_price=item.unit_price,
    )


def _to_card(shipment: Shipment) -> ShipmentCard:
    return ShipmentCard(
        id=shipment.id,
        ttn_number=shipment.ttn_number,
        recipient_name=shipment.recipient_name,
        recipient_phone=shipment.recipient_phone,
        recipient_city=shipment.recipient_city,
        recipient_warehouse=shipment.recipient_warehouse,
        status=shipment.status,
        created_at=shipment.created_at,
        status_changed_at=shipment.status_changed_at,
        payment_method=shipment.payment_method,
        payer_type=shipment.payer_type,
        cod_amount=shipment.cod_amount,
        insured_amount=shipment.insured_amount,
        items=[_to_item_view(item) for item in shipment.items],
        can_cancel=shipment.status in CANCELABLE_STATUSES,
    )


async def list_shipments(
    session: AsyncSession,
    *,
    client: User,
    bucket: str = "created",
    query: str | None = None,
    limit: int = 8,
    offset: int = 0,
) -> ShipmentPage:
    _require_active_client(client)
    rows, total = await ShipmentRepository(session).get_by_client_and_status(
        client.id,
        statuses=statuses_for_bucket(bucket),
        query=query,
        limit=limit,
        offset=offset,
    )
    return ShipmentPage(
        items=[_to_list_item(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_shipment_card(
    session: AsyncSession, *, client: User, shipment_id: uuid.UUID
) -> ShipmentCard:
    _require_active_client(client)
    shipment = await ShipmentRepository(session).get_by_id(shipment_id)
    if shipment is None or shipment.client_id != client.id:
        raise ShipmentNotFound(str(shipment_id))
    return _to_card(shipment)


async def cancel_shipment(
    session: AsyncSession, *, client: User, shipment_id: uuid.UUID
) -> ShipmentCard:
    _require_active_client(client)
    repo = ShipmentRepository(session)
    shipment = await repo.get_by_id(shipment_id)
    if shipment is None or shipment.client_id != client.id:
        raise ShipmentNotFound(str(shipment_id))
    if shipment.status not in CANCELABLE_STATUSES:
        raise ShipmentActionForbidden("cancel", shipment.status)
    before = {"status": shipment.status}
    await repo.update_status(shipment, ShipmentStatus.cancelled)
    await AuditRepository(session).log(
        "shipment_cancelled_by_client",
        user_id=client.id,
        affected_entity=f"shipment:{shipment.id}",
        before=before,
        after={"status": shipment.status},
    )
    return _to_card(shipment)
