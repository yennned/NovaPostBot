"""Репозиторий append-only движений склада."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Protocol

from sqlalchemy import select

from app.db.models.enums import StockMovementType
from app.db.models.stock_movement import StockMovement
from app.db.repositories.base import BaseRepository
from app.db.repositories.scope import resolve_account_scope


class _SkuQuantityItem(Protocol):
    """Позиция с артикулом и количеством — `ShipmentItem` или `ShipmentItemDraft`."""

    sku: str
    quantity: int


class StockMovementRepository(BaseRepository):
    async def create(
        self,
        *,
        client_id: uuid.UUID | None = None,
        account_id: uuid.UUID | None = None,
        sku: str,
        movement_type: StockMovementType,
        quantity_delta: int,
        quantity_before: int,
        quantity_after: int,
        shipment_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        comment: str | None = None,
    ) -> StockMovement:
        client_id, account_id = await resolve_account_scope(
            self.session, client_id=client_id, account_id=account_id
        )
        movement = StockMovement(
            client_id=client_id,
            account_id=account_id,
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
        client_id: uuid.UUID | None = None,
        account_id: uuid.UUID | None = None,
        shipment_id: uuid.UUID,
        items: Iterable[_SkuQuantityItem],
        movement_type: StockMovementType,
        sign: int,
        comment: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        """По движению на каждую позицию: `delta = sign * quantity` (sign ±1).

        `quantity_before`/`quantity_after` — заглушки `0`/`delta`: это движения
        БРОНИ (`ttn_reserve`/`ttn_cancel`), которые количество не двигают, а значит
        и running-баланса у них нет. Физические движения пишет
        `StockBalanceRepository.apply_movement` — там before/after честные.

        Один `add_all` + один flush, а не flush на позицию: у многопозиционной ТТН
        это была бы пачка round-trip'ов подряд внутри апдейта, при котором коннект
        и так удерживается через вызовы НП и Sheets.
        """
        # Скоуп резолвим ОДИН раз на пачку, а не на позицию: он одинаков для всех
        # строк одной ТТН, а `resolve_account_scope` — это запрос в БД.
        client_id, account_id = await resolve_account_scope(
            self.session, client_id=client_id, account_id=account_id
        )
        movements = [
            StockMovement(
                client_id=client_id,
                account_id=account_id,
                shipment_id=shipment_id,
                actor_user_id=actor_user_id,
                sku=item.sku,
                movement_type=movement_type,
                quantity_delta=sign * item.quantity,
                quantity_before=0,
                quantity_after=sign * item.quantity,
                comment=comment,
            )
            for item in items
        ]
        if not movements:
            return
        self.session.add_all(movements)
        await self.session.flush()

    async def list_for_shipment(self, shipment_id: uuid.UUID) -> list[StockMovement]:
        stmt = (
            select(StockMovement)
            .where(StockMovement.shipment_id == shipment_id)
            .order_by(StockMovement.created_at)
        )
        return list(await self.session.scalars(stmt))
