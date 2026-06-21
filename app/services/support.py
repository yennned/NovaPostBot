"""Поддержка (Фаза 6): релей-чат клиент↔дежурный менеджер через бота.

Доменная логика без aiogram. Маршрутизация обращения зависит от расписания и
наличия дежурного ([docs/10-support-duty.md](../../docs/10-support-duty.md)):

- рабочее время + есть дежурный → тред `open`, назначен дежурному (живой чат);
- рабочее время, дежурного нет → тред `waiting` + сигнал владельцу;
- вне рабочего времени → тред `waiting`, ответ на следующий рабочий день.

Лог всех тредов/сообщений — в Postgres (виден owner/dev), ничего не теряется.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.enums import SupportThreadStatus, UserRole, UserStatus
from app.db.models.support import SupportMessage, SupportThread
from app.db.models.user import User
from app.db.repositories import SupportRepository
from app.services import duty
from app.services.exceptions import PermissionDenied
from app.utils.work_schedule import is_open, window_for_day


@dataclass(frozen=True, slots=True)
class DutyContact:
    """Снимок дежурства для клиента: кто на связи и когда работает отделение."""

    manager: User | None
    window: tuple[datetime, datetime] | None  # рабочее окно сегодня (или None — выходной)
    office_open: bool


@dataclass(frozen=True, slots=True)
class ThreadOpenResult:
    thread: SupportThread
    created: bool
    routed: bool  # назначен живому дежурному (релей идёт сразу)
    notify_owner: bool  # очередь в рабочее время без дежурного → пинг владельцу
    office_open: bool


def _now_local(settings: Settings, now: datetime | None) -> datetime:
    tz = ZoneInfo(settings.timezone)
    return datetime.now(tz) if now is None else now.astimezone(tz)


async def get_duty_contact(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> DutyContact:
    cfg = settings or get_settings()
    moment = _now_local(cfg, now)
    managers = await duty.current_duty_managers(session, settings=cfg, now=moment)
    return DutyContact(
        manager=managers[0] if managers else None,
        window=window_for_day(moment, cfg.work_schedule),
        office_open=is_open(moment, cfg.work_schedule),
    )


async def open_or_get_thread(
    session: AsyncSession,
    *,
    client: User,
    shipment_id: uuid.UUID | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> ThreadOpenResult:
    """Вернуть активный тред клиента или создать новый с маршрутизацией."""
    if client.role is not UserRole.client:
        raise PermissionDenied("звернення до підтримки доступне лише клієнту")
    if client.status is not UserStatus.active:
        raise PermissionDenied("звернення доступне після підтвердження акаунта")

    cfg = settings or get_settings()
    repo = SupportRepository(session)
    contact = await get_duty_contact(session, settings=cfg, now=now)

    existing = await repo.get_active_thread_for_client(client.id)
    if existing is not None:
        return ThreadOpenResult(
            thread=existing,
            created=False,
            routed=existing.assigned_manager_id is not None,
            notify_owner=False,
            office_open=contact.office_open,
        )

    if contact.office_open and contact.manager is not None:
        thread = await repo.create_thread(
            client_id=client.id,
            assigned_manager_id=contact.manager.id,
            shipment_id=shipment_id,
            status=SupportThreadStatus.open,
        )
        return ThreadOpenResult(
            thread=thread, created=True, routed=True, notify_owner=False, office_open=True
        )

    thread = await repo.create_thread(
        client_id=client.id,
        shipment_id=shipment_id,
        status=SupportThreadStatus.waiting,
    )
    return ThreadOpenResult(
        thread=thread,
        created=True,
        routed=False,
        notify_owner=contact.office_open,  # рабочее время без дежурного → владельцу
        office_open=contact.office_open,
    )


async def post_message(
    session: AsyncSession,
    *,
    thread: SupportThread,
    sender_role: str,
    text: str,
) -> SupportMessage:
    """Добавить реплику в тред и поднять его в инбоксе (`updated_at`)."""
    message = await SupportRepository(session).add_message(
        thread_id=thread.id, sender_role=sender_role, text=text
    )
    thread.updated_at = datetime.now(UTC)  # bump для сортировки инбокса по активности
    await session.flush()
    return message


async def claim_if_waiting(
    session: AsyncSession,
    *,
    thread: SupportThread,
    manager: User,
) -> SupportThread:
    """Если тред ещё в очереди — назначить его ответившему менеджеру (статус `open`)."""
    if thread.assigned_manager_id is None and thread.status is SupportThreadStatus.waiting:
        return await SupportRepository(session).assign_manager(
            thread, manager.id, status=SupportThreadStatus.open
        )
    return thread


async def close_thread(session: AsyncSession, *, thread: SupportThread) -> SupportThread:
    return await SupportRepository(session).close_thread(thread)
