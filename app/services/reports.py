"""Отчёты и аналитика (Фаза 6) — доменная логика без aiogram.

- 📊 Звіти (менеджер с правом `can_view_reports`, владелец): сводка по периоду +
  разбивка по клиентам (відправлено / повернення / втрати / чисті продажі).
- 📈 Аналітика (только владелец): + финотчёт (сумма fee, опоздавшие ТТН) +
  поддержка по менеджерах.

Fee берём из persisted `shipments.fee_amount`/`fee_free` (воркер проставляет при
`dispatched`, бесплатно при промахе SLA). Preview-формула `20 + (units − 1)` —
единый `compute_shipment_fee` (app.services.shipment). Аттрибуция ТТН по менеджерам отложена
(нет `manager_id` у `shipments`) — здесь per-manager = метрики поддержки.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import permissions
from app.config import Settings, get_settings
from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User
from app.db.repositories import ReportsRepository, UserRepository
from app.services.exceptions import PermissionDenied
from app.services.stats import LOSS_STATUSES, RETURN_STATUSES, _bounds

PERIODS = ("today", "week", "month")


@dataclass(frozen=True, slots=True)
class ClientBreakdown:
    client_id: uuid.UUID
    client_name: str
    shipped: int
    returns: int
    losses: int

    @property
    def net(self) -> int:
        return self.shipped - self.returns - self.losses


@dataclass(frozen=True, slots=True)
class PeriodReport:
    period: str
    start: datetime
    end: datetime
    shipped: int
    returns: int
    losses: int
    clients: list[ClientBreakdown]

    @property
    def net(self) -> int:
        return self.shipped - self.returns - self.losses


@dataclass(frozen=True, slots=True)
class LateTtn:
    ttn_number: str | None
    client_name: str
    dispatched_at: datetime | None


@dataclass(frozen=True, slots=True)
class FinancialReport:
    period: str
    start: datetime
    end: datetime
    dispatched_count: int
    fee_total: Decimal
    free_count: int
    late: list[LateTtn]


@dataclass(frozen=True, slots=True)
class ManagerSupportStat:
    manager_id: uuid.UUID
    name: str
    open_count: int
    closed_count: int


def _require_reports(actor: User, settings: Settings | None) -> None:
    if permissions.is_dev(actor.telegram_id, settings):
        return
    if actor.status is not UserStatus.active:
        raise PermissionDenied("учётная запись неактивна")
    if actor.role is UserRole.owner:
        return
    if actor.role is UserRole.manager and permissions.has_permission(
        actor, permissions.CAN_VIEW_REPORTS, settings
    ):
        return
    raise PermissionDenied("звіти недоступні")


def _require_owner(actor: User, settings: Settings | None) -> None:
    if permissions.is_dev(actor.telegram_id, settings):
        return
    if actor.status is not UserStatus.active or actor.role is not UserRole.owner:
        raise PermissionDenied("аналітика доступна лише власнику")


async def period_report(
    session: AsyncSession,
    *,
    actor: User,
    period: str = "today",
    day: date | None = None,
    settings: Settings | None = None,
) -> PeriodReport:
    _require_reports(actor, settings)
    cfg = settings or get_settings()
    start, end = _bounds(period, day=day, settings=cfg)
    repo = ReportsRepository(session)
    # Три агрегата вместо трёх полных выгрузок периода. Считает Postgres: на
    # 15k ТТН/мес прежняя форма тянула в память десятки тысяч строк (joinedload
    # по позициям — декартово произведение) ради трёх чисел на экране.
    buckets = [
        ("shipped", await repo.dispatched_units_by_client(start=start, end=end)),
        (
            "returns",
            await repo.status_changed_units_by_client(
                start=start, end=end, statuses=RETURN_STATUSES
            ),
        ),
        (
            "losses",
            await repo.status_changed_units_by_client(start=start, end=end, statuses=LOSS_STATUSES),
        ),
    ]

    acc: dict[uuid.UUID | None, dict] = {}
    for bucket, rows in buckets:
        for row in rows:
            rec = acc.setdefault(
                row.client_id,
                {"shipped": 0, "returns": 0, "losses": 0, "name": row.client_name or "—"},
            )
            rec[bucket] += row.units

    clients = [
        ClientBreakdown(
            client_id=cid,
            client_name=rec["name"],
            shipped=rec["shipped"],
            returns=rec["returns"],
            losses=rec["losses"],
        )
        for cid, rec in acc.items()
    ]
    clients.sort(key=lambda c: c.net, reverse=True)
    return PeriodReport(
        period="day" if day is not None else period,
        start=start,
        end=end,
        shipped=sum(c.shipped for c in clients),
        returns=sum(c.returns for c in clients),
        losses=sum(c.losses for c in clients),
        clients=clients,
    )


async def financial_report(
    session: AsyncSession,
    *,
    actor: User,
    period: str = "today",
    day: date | None = None,
    settings: Settings | None = None,
) -> FinancialReport:
    _require_owner(actor, settings)
    cfg = settings or get_settings()
    start, end = _bounds(period, day=day, settings=cfg)
    repo = ReportsRepository(session)
    # Раньше эта функция выгружала период вторым разом — ровно те же строки, что
    # уже прочитал `period_report` на том же экране. Теперь оба считают агрегатами,
    # и вторая выгрузка исчезает вместе с первой.
    totals = await repo.dispatch_totals(start=start, end=end)
    late = [
        LateTtn(
            ttn_number=s.ttn_number,
            client_name=(s.client.full_name if s.client else None) or "—",
            dispatched_at=s.dispatched_at,
        )
        for s in await repo.late_dispatched(start=start, end=end)
    ]
    return FinancialReport(
        period="day" if day is not None else period,
        start=start,
        end=end,
        dispatched_count=totals.count,
        fee_total=totals.fee_total,
        free_count=totals.free_count,
        late=late,
    )


async def manager_support_stats(
    session: AsyncSession,
    *,
    actor: User,
    period: str = "today",
    day: date | None = None,
    settings: Settings | None = None,
) -> list[ManagerSupportStat]:
    _require_owner(actor, settings)
    cfg = settings or get_settings()
    start, end = _bounds(period, day=day, settings=cfg)
    repo = ReportsRepository(session)
    open_counts = await repo.open_thread_counts()
    closed_counts = await repo.closed_thread_counts(start=start, end=end)
    managers = await UserRepository(session).list_by_role(UserRole.manager)
    return [
        ManagerSupportStat(
            manager_id=m.id,
            name=m.full_name or str(m.telegram_id),
            open_count=open_counts.get(m.id, 0),
            closed_count=closed_counts.get(m.id, 0),
        )
        for m in managers
    ]
