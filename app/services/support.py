"""Поддержка (Фаза 6): релей-чат клиент↔дежурный менеджер через бота.

Доменная логика без aiogram. Маршрутизация обращения зависит от расписания и
наличия дежурного ([docs/10-support-duty.md](../../docs/10-support-duty.md)):

- рабочее время + есть дежурный → тред `open`, назначен дежурному (живой чат);
- рабочее время, дежурного нет → тред `waiting` + сигнал менеджерам (заступить);
- вне рабочего времени → тред `waiting`, ответ на следующий рабочий день.

Лог всех тредов/сообщений — в Postgres (виден dev), ничего не теряется.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.enums import SupportThreadStatus, UserRole, UserStatus
from app.db.models.support import SupportMessage, SupportThread
from app.db.models.user import User
from app.db.repositories import SupportRepository
from app.services import duty
from app.services.exceptions import PermissionDenied
from app.utils.timefmt import now_local
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
    notify_managers: bool  # очередь в рабочее время без дежурного → пинг менеджерам
    office_open: bool


async def get_duty_contact(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    account_id: uuid.UUID | None = None,
) -> DutyContact:
    cfg = settings or get_settings()
    moment = now_local(cfg, now)
    managers = await duty.current_duty_managers(session, settings=cfg, now=moment)
    return DutyContact(
        manager=managers[0] if managers else None,
        window=window_for_day(moment, cfg.work_schedule),
        office_open=is_open(moment, cfg.work_schedule),
    )


def ensure_can_open(client: User) -> None:
    """Право открыть обращение. Отдельно от `open_or_get_thread`, чтобы UI мог
    проверить доступ на входе в чат, не создавая тред."""
    if client.role is not UserRole.client:
        raise PermissionDenied("звернення до підтримки доступне лише клієнту")
    if client.status is not UserStatus.active:
        raise PermissionDenied("звернення доступне після підтвердження акаунта")


async def _lock_thread_scope(session: AsyncSession, scope_id: uuid.UUID) -> None:
    """Сериализовать get-or-create треда по клиенту/аккаунту на время транзакции.

    Апдейты из одного `getUpdates`-батча aiogram обрабатывает параллельными
    тасками (`handle_as_tasks=True` по умолчанию), у каждой своя сессия. Два
    первых сообщения подряд иначе оба не находят активный тред и оба его
    создают — у менеджера появляются два обращения вместо одного. Уникального
    индекса на активный тред нет (и его не навесить: в проде уже лежат старые
    дубли), поэтому берём advisory-lock, который снимается вместе с транзакцией.
    """
    key = int.from_bytes(
        hashlib.blake2b(scope_id.bytes, digest_size=8).digest(), "big", signed=True
    )
    await session.execute(select(func.pg_advisory_xact_lock(key)))


async def open_or_get_thread(
    session: AsyncSession,
    *,
    client: User,
    shipment_id: uuid.UUID | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    account_id: uuid.UUID | None = None,
) -> ThreadOpenResult:
    """Вернуть активный тред клиента или создать новый с маршрутизацией."""
    ensure_can_open(client)

    cfg = settings or get_settings()
    repo = SupportRepository(session)
    contact = await get_duty_contact(session, settings=cfg, now=now)

    await _lock_thread_scope(session, account_id or client.id)
    existing = (
        await repo.get_active_thread_for_client(client.id)
        if account_id is None
        else await repo.get_active_thread_for_account(account_id)
    )
    if existing is not None:
        return ThreadOpenResult(
            thread=existing,
            created=False,
            routed=existing.assigned_manager_id is not None,
            notify_managers=False,
            office_open=contact.office_open,
        )

    if contact.office_open and contact.manager is not None:
        thread = await repo.create_thread(
            client_id=client.id,
            account_id=account_id,
            assigned_manager_id=contact.manager.id,
            shipment_id=shipment_id,
            status=SupportThreadStatus.open,
        )
        return ThreadOpenResult(
            thread=thread, created=True, routed=True, notify_managers=False, office_open=True
        )

    thread = await repo.create_thread(
        client_id=client.id,
        account_id=account_id,
        shipment_id=shipment_id,
        status=SupportThreadStatus.waiting,
    )
    return ThreadOpenResult(
        thread=thread,
        created=True,
        routed=False,
        notify_managers=contact.office_open,  # рабочее время без дежурного → менеджерам
        office_open=contact.office_open,
    )


async def post_message(
    session: AsyncSession,
    *,
    thread: SupportThread,
    sender_role: str,
    text: str,
    sender_user_id: uuid.UUID | None = None,
) -> SupportMessage:
    """Добавить реплику в тред и поднять его в инбоксе (`updated_at`)."""
    message = await SupportRepository(session).add_message(
        thread_id=thread.id,
        sender_role=sender_role,
        sender_user_id=sender_user_id,
        text=text,
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
