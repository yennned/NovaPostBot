"""Репозиторий отчётов (Фаза 6): кросс-клиентские агрегаты по периоду.

Только чтение поверх `shipments`/`support_threads`. Бизнес-агрегация (чисті
продажі, fee-итоги) — в [services/reports.py](../../services/reports.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app.db.models.enums import SupportThreadStatus
from app.db.models.shipment import Shipment
from app.db.models.support import SupportThread
from app.db.repositories.base import BaseRepository


class ReportsRepository(BaseRepository):
    async def shipments_status_changed(
        self,
        *,
        start: datetime,
        end: datetime,
        statuses: set | None = None,
    ) -> list[Shipment]:
        """Все ТТН (по всем клиентам) с изменением статуса в окне — для сводок."""
        conditions = [Shipment.status_changed_at >= start, Shipment.status_changed_at < end]
        if statuses:
            conditions.append(Shipment.status.in_(tuple(statuses)))
        stmt = (
            select(Shipment)
            .options(joinedload(Shipment.client), joinedload(Shipment.items))
            .where(*conditions)
            .order_by(Shipment.status_changed_at.desc())
        )
        rows = await self.session.scalars(stmt)
        return list(rows.unique())

    async def shipments_dispatched(self, *, start: datetime, end: datetime) -> list[Shipment]:
        """ТТН, отправленные в окне (по `dispatched_at`) — для fee и опоздавших."""
        stmt = (
            select(Shipment)
            .options(joinedload(Shipment.client))
            .where(
                Shipment.dispatched_at.is_not(None),
                Shipment.dispatched_at >= start,
                Shipment.dispatched_at < end,
            )
            .order_by(Shipment.dispatched_at.desc())
        )
        rows = await self.session.scalars(stmt)
        return list(rows.unique())

    async def open_thread_counts(self) -> dict[uuid.UUID, int]:
        stmt = (
            select(SupportThread.assigned_manager_id, func.count())
            .where(
                SupportThread.status == SupportThreadStatus.open,
                SupportThread.assigned_manager_id.is_not(None),
            )
            .group_by(SupportThread.assigned_manager_id)
        )
        rows = await self.session.execute(stmt)
        return {manager_id: int(count) for manager_id, count in rows}

    async def closed_thread_counts(self, *, start: datetime, end: datetime) -> dict[uuid.UUID, int]:
        stmt = (
            select(SupportThread.assigned_manager_id, func.count())
            .where(
                SupportThread.status == SupportThreadStatus.closed,
                SupportThread.assigned_manager_id.is_not(None),
                SupportThread.closed_at >= start,
                SupportThread.closed_at < end,
            )
            .group_by(SupportThread.assigned_manager_id)
        )
        rows = await self.session.execute(stmt)
        return {manager_id: int(count) for manager_id, count in rows}
