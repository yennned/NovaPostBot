"""Репозиторий append-only движений склада."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Protocol

from sqlalchemy import select

from app.db.models.enums import StockMovementType
from app.db.models.stock_movement import StockMovement
from app.db.repositories.base import BaseRepository


class _SkuQuantityItem(Protocol):
    """Позиция с артикулом и количеством — `ShipmentItem` или `ShipmentItemDraft`."""

    sku: str
    quantity: int


class StockMovementRepository(BaseRepository):
    async def create(
        self,
        *,
        client_id: uuid.UUID,
        sku: str,
        movement_type: StockMovementType,
        quantity_delta: int,
        quantity_before: int,
        quantity_after: int,
        shipment_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        comment: str | None = None,
    ) -> StockMovement:
        movement = StockMovement(
            client_id=client_id,
            shipment_id=shipment_id,
            actor_user_id=actor_user_id,
            sku=sku,
            movement_type=movement_type,
            quantity_delta=quantity_delta,
            quantity_before=quantity_before,
            quantity_after=quantity_after,
            comment=comment,
        )
        await self._add(movement)
        return movement

    async def record_for_items(
        self,
        *,
        client_id: uuid.UUID,
        shipment_id: uuid.UUID,
        items: Iterable[_SkuQuantityItem],
        movement_type: StockMovementType,
        sign: int,
        comment: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        """По движению на каждую позицию: `delta = sign * quantity` (sign ±1).

        `quantity_before`/`quantity_after` — заглушки `0`/`delta`: реального running-
        баланса не ведём (источник правды по остатку — «Склад» в Sheets, резерв —
        сумма движений в PG). Единая точка этой конвенции для всех write-путей ТТН.
        """
        for item in items:
            delta = sign * item.quantity
            await self.create(
                client_id=client_id,
                shipment_id=shipment_id,
                actor_user_id=actor_user_id,
                sku=item.sku,
                movement_type=movement_type,
                quantity_delta=delta,
                quantity_before=0,
                quantity_after=delta,
                comment=comment,
            )

    async def list_for_shipment(self, shipment_id: uuid.UUID) -> list[StockMovement]:
        stmt = (
            select(StockMovement)
            .where(StockMovement.shipment_id == shipment_id)
            .order_by(StockMovement.created_at)
        )
        return list(await self.session.scalars(stmt))
