"""Дежурство менеджера (Фаза 6): открытие смены и авто-снятие.

Смену открывает кнопка «🟢 Я на зв'язку» (не `/start`) — это утренняя авторизация
менеджера на день; все обращения поддержки маршрутизируются текущему дежурному.
Кнопки выключения нет: смена снимается воркером при закрытии отделения по
dev-расписанию (Europe/Kyiv). См. [docs/10-support-duty.md](../../docs/10-support-duty.md).

Доменный слой без aiogram: функции принимают `AsyncSession`, внутри строят
репозитории и пишут аудит; транзакцией управляет вызывающий (middleware/воркер).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User
from app.db.repositories import AuditRepository, UserRepository
from app.services.exceptions import OfficeClosed, PermissionDenied
from app.utils.timefmt import now_local
from app.utils.work_schedule import current_window_end, is_open, next_window_start

# Дежурят только менеджеры.
DUTY_ROLES = frozenset({UserRole.manager})


@dataclass(frozen=True, slots=True)
class DutyResult:
    user: User
    window_end: datetime  # конец текущего рабочего окна (Europe/Kyiv)


async def go_on_duty(
    session: AsyncSession,
    *,
    user: User,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> DutyResult:
    """Открыть смену: `on_duty=True`, `duty_date=today`, `duty_since=now` + аудит.

    Вне рабочих часов смену открыть нельзя (`OfficeClosed`) — иначе воркер сразу
    бы её снял; маршрутизация поддержки и так идёт только в рабочее время.
    """
    cfg = settings or get_settings()
    if user.role not in DUTY_ROLES:
        raise PermissionDenied("чергування доступне лише менеджеру")
    if user.status is not UserStatus.active:
        raise PermissionDenied("обліковий запис неактивний")

    moment = now_local(cfg, now)
    schedule = cfg.work_schedule
    window_end = current_window_end(moment, schedule)
    if window_end is None:
        raise OfficeClosed(next_open=next_window_start(moment, schedule))

    await UserRepository(session).set_duty(
        user, on_duty=True, duty_date=moment.date(), duty_since=moment
    )
    await AuditRepository(session).log(
        "duty_started",
        user_id=user.id,
        affected_entity=f"user:{user.id}",
        notes=f"on_duty until {window_end.isoformat()}",
    )
    return DutyResult(user=user, window_end=window_end)


async def current_duty_managers(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> list[User]:
    """Дежурные сейчас (только менеджеры — `DUTY_ROLES`), вставший последним — первый.

    Владелец дежурным быть не может: `DUTY_ROLES = {manager}`, поэтому обращение без
    живого дежурного уходит в очередь + пинг менеджерам (см. `open_or_get_thread`),
    а не владельцу. Поддержка (Фаза 6c) маршрутизирует новый тред на `[0]`. Фильтр по
    `duty_date` отсекает «зависшие» смены прошлого дня, ещё не снятые воркером.
    """
    cfg = settings or get_settings()
    today = now_local(cfg, now).date()
    stmt = (
        select(User)
        .where(
            User.role.in_(tuple(DUTY_ROLES)),
            User.status == UserStatus.active,
            User.on_duty.is_(True),
            User.duty_date == today,
        )
        .order_by(User.duty_since.desc())
    )
    return list(await session.scalars(stmt))


async def clear_expired_duty(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> list[User]:
    """Снять дежурство у тех, чья смена истекла (отделение закрылось / новый день).

    Возвращает снятых пользователей — воркер опц. шлёт им «зміну завершено».
    """
    cfg = settings or get_settings()
    moment = now_local(cfg, now)
    keep_open = is_open(moment, cfg.work_schedule)
    today = moment.date()

    users = list(
        await session.scalars(
            select(User).where(User.on_duty.is_(True), User.role.in_(tuple(DUTY_ROLES)))
        )
    )
    repo = UserRepository(session)
    audit = AuditRepository(session)
    cleared: list[User] = []
    for user in users:
        if keep_open and user.duty_date == today:
            continue  # смена ещё активна
        await repo.set_duty(user, on_duty=False, duty_date=user.duty_date, duty_since=None)
        await audit.log(
            "duty_ended",
            user_id=user.id,
            affected_entity=f"user:{user.id}",
            notes="auto: office closed",
        )
        cleared.append(user)
    return cleared
