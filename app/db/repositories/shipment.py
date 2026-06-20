"""Репозиторий отправлений клиента (`shipments`)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Date, cast, func, or_, select
from sqlalchemy.orm import joinedload

from app.db.models.enums import ShipmentStatus, StockMovementType
from app.db.models.shipment import Shipment, ShipmentItem
from app.db.models.stock_movement import StockMovement
from app.db.models.user import User
from app.db.repositories.base import BaseRepository

RESERVING_STATUSES = {ShipmentStatus.created, ShipmentStatus.confirmed}
TRACKABLE_STATUSES = {
    ShipmentStatus.created,
    ShipmentStatus.confirmed,
    ShipmentStatus.dispatched,
    ShipmentStatus.in_transit,
    ShipmentStatus.arrived,
    ShipmentStatus.returning,
}


@dataclass(frozen=True, slots=True)
class ShipmentItemDraft:
    sku: str
    name: str
    quantity: int
    category: str | None = None
    unit_price: Decimal | None = None


class ShipmentRepository(BaseRepository):
    async def get_by_id(self, shipment_id: uuid.UUID) -> Shipment | None:
        stmt = (
            select(Shipment)
            .options(
                joinedload(Shipment.client),
                joinedload(Shipment.items),
                joinedload(Shipment.sender_profile),
                joinedload(Shipment.stock_movements),
            )
            .where(Shipment.id == shipment_id)
        )
        return await self.session.scalar(stmt)

    async def get_by_ttn_number(self, ttn_number: str) -> Shipment | None:
        stmt = (
            select(Shipment)
            .options(
                joinedload(Shipment.client),
                joinedload(Shipment.items),
                joinedload(Shipment.sender_profile),
                joinedload(Shipment.stock_movements),
            )
            .where(Shipment.ttn_number == ttn_number)
        )
        return await self.session.scalar(stmt)

    async def create(
        self,
        *,
        client_id: uuid.UUID,
        recipient_name: str,
        items: list[ShipmentItemDraft],
        sender_profile_id: uuid.UUID | None = None,
        ttn_number: str | None = None,
        np_ref: str | None = None,
        recipient_phone: str | None = None,
        recipient_city: str | None = None,
        recipient_warehouse: str | None = None,
        recipient_kind: str = "person",
        payer_type: str | None = None,
        payment_method: str | None = None,
        cod_amount: Decimal | None = None,
        insured_amount: Decimal | None = None,
        size_preset: str | None = None,
        weight: Decimal | None = None,
        status: ShipmentStatus = ShipmentStatus.created,
        description: str | None = None,
        created_at: datetime | None = None,
        status_changed_at: datetime | None = None,
    ) -> Shipment:
        shipment = Shipment(
            client_id=client_id,
            sender_profile_id=sender_profile_id,
            ttn_number=ttn_number,
            np_ref=np_ref,
            recipient_name=recipient_name,
            recipient_phone=recipient_phone,
            recipient_city=recipient_city,
            recipient_warehouse=recipient_warehouse,
            recipient_kind=recipient_kind,
            payer_type=payer_type,
            payment_method=payment_method,
            cod_amount=cod_amount,
            insured_amount=insured_amount,
            size_preset=size_preset,
            weight=weight,
            status=status,
            description=description,
        )
        if created_at is not None:
            shipment.created_at = created_at
        if status_changed_at is not None:
            shipment.status_changed_at = status_changed_at
        await self._add(shipment)
        for item in items:
            self.session.add(
                ShipmentItem(
                    shipment_id=shipment.id,
                    sku=item.sku,
                    name=item.name,
                    category=item.category,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                )
            )
        await self.session.flush()
        return shipment

    async def get_by_client_and_status(
        self,
        client_id: uuid.UUID,
        *,
        statuses: set[ShipmentStatus] | None = None,
        query: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Shipment], int]:
        conditions = [Shipment.client_id == client_id]
        if statuses:
            conditions.append(Shipment.status.in_(tuple(statuses)))
        if query:
            stripped = query.strip()
            text_filters = []
            pattern = f"%{stripped}%"
            text_filters.append(Shipment.ttn_number.ilike(pattern))
            text_filters.append(Shipment.recipient_name.ilike(pattern))
            parsed_date = _parse_query_date(stripped)
            if parsed_date is not None:
                text_filters.append(cast(Shipment.created_at, Date) == parsed_date)
            conditions.append(or_(*text_filters))

        total = await self.session.scalar(
            select(func.count()).select_from(Shipment).where(*conditions)
        )
        rows = await self.session.scalars(
            select(Shipment)
            .options(
                joinedload(Shipment.client),
                joinedload(Shipment.items),
                joinedload(Shipment.sender_profile),
            )
            .where(*conditions)
            .order_by(Shipment.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows.unique()), int(total or 0)

    async def list_status_changed_between(
        self,
        client_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
    ) -> list[Shipment]:
        stmt = (
            select(Shipment)
            .options(
                joinedload(Shipment.client),
                joinedload(Shipment.items),
                joinedload(Shipment.sender_profile),
            )
            .where(
                Shipment.client_id == client_id,
                Shipment.status_changed_at >= start,
                Shipment.status_changed_at < end,
            )
            .order_by(Shipment.status_changed_at.desc())
        )
        rows = await self.session.scalars(stmt)
        return list(rows.unique())

    async def reserved_by_sku(self, client_id: uuid.UUID) -> dict[str, int]:
        stmt = (
            select(ShipmentItem.sku, func.coalesce(func.sum(ShipmentItem.quantity), 0))
            .join(Shipment, Shipment.id == ShipmentItem.shipment_id)
            .where(
                Shipment.client_id == client_id,
                Shipment.status.in_(tuple(RESERVING_STATUSES)),
            )
            .group_by(ShipmentItem.sku)
        )
        rows = await self.session.execute(stmt)
        return {sku: int(total) for sku, total in rows}

    async def list_for_tracking(self, *, limit: int = 200) -> list[Shipment]:
        stmt = (
            select(Shipment)
            .options(
                joinedload(Shipment.client),
                joinedload(Shipment.items),
                joinedload(Shipment.sender_profile),
                joinedload(Shipment.stock_movements),
            )
            .where(
                Shipment.ttn_number.is_not(None),
                Shipment.sender_profile_id.is_not(None),
                Shipment.status.in_(tuple(TRACKABLE_STATUSES)),
            )
            .order_by(Shipment.status_changed_at.asc())
            .limit(limit)
        )
        rows = await self.session.scalars(stmt)
        return list(rows.unique())

    async def list_for_staff(
        self,
        *,
        statuses: set[ShipmentStatus] | None = None,
        query: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Shipment], int]:
        conditions = []
        if statuses:
            conditions.append(Shipment.status.in_(tuple(statuses)))
        stmt = select(Shipment).join(User, User.id == Shipment.client_id)
        count_stmt = (
            select(func.count()).select_from(Shipment).join(User, User.id == Shipment.client_id)
        )
        if query:
            stripped = query.strip()
            pattern = f"%{stripped}%"
            text_filters = [
                Shipment.ttn_number.ilike(pattern),
                Shipment.recipient_name.ilike(pattern),
                User.full_name.ilike(pattern),
                User.phone.ilike(pattern),
            ]
            parsed_date = _parse_query_date(stripped)
            if parsed_date is not None:
                text_filters.append(cast(Shipment.created_at, Date) == parsed_date)
            conditions.append(or_(*text_filters))
        if conditions:
            stmt = stmt.where(*conditions)
            count_stmt = count_stmt.where(*conditions)
        total = await self.session.scalar(count_stmt)
        rows = await self.session.scalars(
            stmt.options(
                joinedload(Shipment.client),
                joinedload(Shipment.items),
                joinedload(Shipment.sender_profile),
                joinedload(Shipment.stock_movements),
            )
            .order_by(Shipment.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows.unique()), int(total or 0)

    async def count_by_statuses(self, statuses: set[ShipmentStatus]) -> int:
        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(Shipment)
                .where(Shipment.status.in_(tuple(statuses)))
            )
            or 0
        )

    async def movement_exists(
        self,
        shipment_id: uuid.UUID,
        movement_type: StockMovementType,
    ) -> bool:
        stmt = (
            select(func.count())
            .select_from(StockMovement)
            .where(
                StockMovement.shipment_id == shipment_id,
                StockMovement.movement_type == movement_type,
            )
        )
        return bool(await self.session.scalar(stmt))

    async def update_status(self, shipment: Shipment, status: ShipmentStatus) -> Shipment:
        shipment.status = status
        shipment.status_changed_at = datetime.now(UTC)
        await self.session.flush()
        return shipment


def _parse_query_date(raw: str) -> date | None:
    formats = ("%Y-%m-%d", "%d.%m.%Y")
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None
