"""Репозиторий отчётов (Фаза 6): кросс-клиентские агрегаты по периоду.

Только чтение поверх `shipments`/`support_threads`. Бизнес-агрегация (чисті
продажі, fee-итоги) — в [services/reports.py](../../services/reports.py).

**Считает SQL, а не Python.** Раньше отчёт выгружал все строки периода с
`joinedload(client, items)` и складывал их `Counter`-ом. На 15 000 ТТН/мес это
15 000 отправлений × ~3 позиции через декартово произведение joinedload — то есть
десятки тысяч строк в память процесса на каждый тап кнопки периода, ради трёх
чисел на экране. `GROUP BY` отдаёт ровно те числа, которые показываются.

Построчно грузится единственное, что построчно и показывается: список опоздавших
ТТН, и он ограничен `LIMIT`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import joinedload

from app.db.models.enums import SupportThreadStatus
from app.db.models.shipment import Shipment, ShipmentItem
from app.db.models.support import SupportThread
from app.db.models.user import User
from app.db.repositories.base import BaseRepository

#: Сколько опоздавших ТТН показывать в финотчёте. Список читается человеком —
#: за этой границей он перестаёт быть списком и становится выгрузкой, а грузить
#: в память десять тысяч строк ради экрана незачем.
LATE_TTN_LIMIT = 200


@dataclass(frozen=True, slots=True)
class ClientUnits:
    """Единицы товара по клиенту за период.

    `client_id` может быть `None`: история переживает удаление человека
    (`shipments.client_id` → `SET NULL`), и такие ТТН собираются в одну группу —
    ровно так же, как это делала прежняя агрегация в Python.
    """

    client_id: uuid.UUID | None
    client_name: str | None
    units: int


@dataclass(frozen=True, slots=True)
class DispatchTotals:
    """Финансовые итоги по отправленным за период — три числа, три агрегата."""

    count: int
    fee_total: Decimal
    free_count: int


class ReportsRepository(BaseRepository):
    def _units_by_client(self, *conditions):
        """Каркас агрегата «единицы товара на клиента».

        `outerjoin` к позициям, а не `join`: ТТН без позиций не должна выкидывать
        клиента из разбивки — прежняя реализация давала ему строку с нулём, и это
        поведение сохраняется. `outerjoin` к `User` — по той же причине, что и
        `None` в `client_id`: клиента могли удалить, а отправления остались.
        """
        return (
            select(
                Shipment.client_id,
                User.full_name,
                func.coalesce(func.sum(ShipmentItem.quantity), 0),
            )
            .select_from(Shipment)
            .outerjoin(ShipmentItem, ShipmentItem.shipment_id == Shipment.id)
            .outerjoin(User, User.id == Shipment.client_id)
            .where(*conditions)
            .group_by(Shipment.client_id, User.full_name)
        )

    async def dispatched_units_by_client(
        self, *, start: datetime, end: datetime
    ) -> list[ClientUnits]:
        """Отправленные за период, единиц на клиента.

        Условие — чистый диапазон по `dispatched_at`, и это важно: прежняя форма
        с `OR (dispatched_at IS NULL AND status IN (…) AND status_changed_at …)`
        была legacy-фолбэком для строк, заведённых до появления поля, и делала
        запрос неиндексируемым независимо от того, какие индексы стоят. Поле
        разово забэкфилено миграцией `e5f8a1b2c3d4`, фолбэк снят.
        """
        rows = await self.session.execute(
            self._units_by_client(Shipment.dispatched_at >= start, Shipment.dispatched_at < end)
        )
        return [ClientUnits(cid, name, int(units)) for cid, name, units in rows]

    async def status_changed_units_by_client(
        self, *, start: datetime, end: datetime, statuses: set
    ) -> list[ClientUnits]:
        """Сменившие статус за период (возвраты, потери), единиц на клиента."""
        rows = await self.session.execute(
            self._units_by_client(
                Shipment.status_changed_at >= start,
                Shipment.status_changed_at < end,
                Shipment.status.in_(tuple(statuses)),
            )
        )
        return [ClientUnits(cid, name, int(units)) for cid, name, units in rows]

    async def dispatch_totals(self, *, start: datetime, end: datetime) -> DispatchTotals:
        """Количество, сумма fee и число бесплатных — одним запросом.

        `fee_amount` суммируется только по платным: `fee_free` означает промах
        SLA, и сумма по нему в выручку не идёт.
        """
        row = (
            await self.session.execute(
                select(
                    func.count(),
                    func.coalesce(
                        func.sum(
                            case((Shipment.fee_free.is_(False), Shipment.fee_amount), else_=None)
                        ),
                        0,
                    ),
                    func.count().filter(Shipment.fee_free.is_(True)),
                ).where(Shipment.dispatched_at >= start, Shipment.dispatched_at < end)
            )
        ).one()
        return DispatchTotals(count=int(row[0]), fee_total=Decimal(row[1]), free_count=int(row[2]))

    async def late_dispatched(
        self, *, start: datetime, end: datetime, limit: int = LATE_TTN_LIMIT
    ) -> list[Shipment]:
        """Опоздавшие по SLA за период — единственная построчная выборка отчёта."""
        stmt = (
            select(Shipment)
            .options(joinedload(Shipment.client))
            .where(
                Shipment.dispatched_at >= start,
                Shipment.dispatched_at < end,
                Shipment.sla_met.is_(False),
            )
            .order_by(Shipment.dispatched_at.desc())
            .limit(limit)
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
