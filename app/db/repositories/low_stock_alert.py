"""Репозиторий persisted state для low-stock уведомлений."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select

from app.db.models.low_stock_alert import LowStockAlert
from app.db.repositories.base import BaseRepository


class LowStockAlertRepository(BaseRepository):
    async def list_for_client(self, client_id: uuid.UUID) -> list[LowStockAlert]:
        stmt = (
            select(LowStockAlert)
            .where(LowStockAlert.client_id == client_id)
            .order_by(LowStockAlert.sku.asc())
        )
        return list(await self.session.scalars(stmt))

    async def get_by_client_and_sku(self, client_id: uuid.UUID, sku: str) -> LowStockAlert | None:
        stmt = select(LowStockAlert).where(
            LowStockAlert.client_id == client_id,
            LowStockAlert.sku == sku,
        )
        return await self.session.scalar(stmt)

    async def upsert_state(
        self,
        *,
        client_id: uuid.UUID,
        sku: str,
        is_low: bool,
        last_available: int,
        last_notified_at: datetime | None = None,
    ) -> LowStockAlert:
        row = await self.get_by_client_and_sku(client_id, sku)
        if row is None:
            row = LowStockAlert(
                client_id=client_id,
                sku=sku,
                is_low=is_low,
                last_available=last_available,
                last_notified_at=last_notified_at,
            )
            await self._add(row)
            return row
        row.is_low = is_low
        row.last_available = last_available
        row.last_notified_at = last_notified_at
        await self.session.flush()
        return row
