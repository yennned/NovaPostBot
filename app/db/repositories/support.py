"""Репозиторий поддержки (`support_threads` / `support_messages`).

Тонкий слой доступа: создание/чтение тредов и реплик, инбокс дежурного и полный
лог для owner/dev. Транзакцией управляет вызывающий (middleware/сервис).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import Date, and_, cast, func, or_, select
from sqlalchemy.orm import aliased, joinedload

from app.db.models.enums import SupportThreadStatus
from app.db.models.support import SupportMessage, SupportThread
from app.db.models.user import User
from app.db.repositories.base import BaseRepository
from app.db.repositories.scope import resolve_account_scope

# Тред «в работе» — открыт диалог или ждёт в очереди (не закрыт).
ACTIVE_STATUSES = {SupportThreadStatus.open, SupportThreadStatus.waiting}


class SupportRepository(BaseRepository):
    async def create_thread(
        self,
        *,
        client_id: uuid.UUID | None = None,
        account_id: uuid.UUID | None = None,
        assigned_manager_id: uuid.UUID | None = None,
        shipment_id: uuid.UUID | None = None,
        status: SupportThreadStatus = SupportThreadStatus.open,
    ) -> SupportThread:
        client_id, account_id = await resolve_account_scope(
            self.session, client_id=client_id, account_id=account_id
        )
        thread = SupportThread(
            client_id=client_id,
            account_id=account_id,
            assigned_manager_id=assigned_manager_id,
            shipment_id=shipment_id,
            status=status,
        )
        await self._add(thread)
        return thread

    async def add_message(
        self,
        *,
        thread_id: uuid.UUID,
        sender_role: str,
        text: str,
        sender_user_id: uuid.UUID | None = None,
    ) -> SupportMessage:
        message = SupportMessage(
            thread_id=thread_id,
            sender_role=sender_role,
            sender_user_id=sender_user_id,
            text=text,
        )
        await self._add(message)
        return message

    async def get_active_thread_for_client(self, client_id: uuid.UUID) -> SupportThread | None:
        """Текущий незакрытый тред клиента (открытый или в очереди), самый свежий."""
        stmt = (
            select(SupportThread)
            .where(
                SupportThread.client_id == client_id,
                SupportThread.status.in_(tuple(ACTIVE_STATUSES)),
            )
            .order_by(SupportThread.created_at.desc())
            .limit(1)
        )
        return await self.session.scalar(stmt)

    async def get_active_thread_for_account(self, account_id: uuid.UUID) -> SupportThread | None:
        stmt = (
            select(SupportThread)
            .where(
                SupportThread.account_id == account_id,
                SupportThread.status.in_(tuple(ACTIVE_STATUSES)),
            )
            .order_by(SupportThread.created_at.desc())
            .limit(1)
        )
        return await self.session.scalar(stmt)

    async def get_with_messages(self, thread_id: uuid.UUID) -> SupportThread | None:
        stmt = (
            select(SupportThread)
            .options(
                joinedload(SupportThread.client),
                joinedload(SupportThread.assigned_manager),
                joinedload(SupportThread.shipment),
                joinedload(SupportThread.messages),
            )
            .where(SupportThread.id == thread_id)
        )
        result = await self.session.scalars(stmt)
        return result.unique().one_or_none()

    async def list_for_manager(
        self,
        manager_id: uuid.UUID,
        *,
        statuses: set[SupportThreadStatus] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[SupportThread], int]:
        conditions = [SupportThread.assigned_manager_id == manager_id]
        if statuses:
            conditions.append(SupportThread.status.in_(tuple(statuses)))
        total = await self.session.scalar(
            select(func.count()).select_from(SupportThread).where(*conditions)
        )
        rows = await self.session.scalars(
            select(SupportThread)
            .options(joinedload(SupportThread.client), joinedload(SupportThread.assigned_manager))
            .where(*conditions)
            .order_by(SupportThread.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows.unique()), int(total or 0)

    async def list_for_manager_inbox(
        self,
        manager_id: uuid.UUID,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[SupportThread], int]:
        """Инбокс дежурного: его открытые треды + вся очередь без дежурного (`waiting`).

        Так заступивший менеджер видит и разгребает очередь, накопленную, пока
        дежурного не было; ответ на `waiting`-тред назначает его себе.
        """
        condition = or_(
            and_(
                SupportThread.assigned_manager_id == manager_id,
                SupportThread.status == SupportThreadStatus.open,
            ),
            SupportThread.status == SupportThreadStatus.waiting,
        )
        total = await self.session.scalar(
            select(func.count()).select_from(SupportThread).where(condition)
        )
        rows = await self.session.scalars(
            select(SupportThread)
            .options(joinedload(SupportThread.client), joinedload(SupportThread.assigned_manager))
            .where(condition)
            .order_by(SupportThread.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows.unique()), int(total or 0)

    async def list_all(
        self,
        *,
        query: str | None = None,
        statuses: set[SupportThreadStatus] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[SupportThread], int]:
        """Полный лог обращений (owner/dev) с поиском по клиенту/менеджеру/дате."""
        client_u = aliased(User)
        manager_u = aliased(User)
        base = (
            select(SupportThread)
            .join(client_u, client_u.id == SupportThread.client_id)
            .join(manager_u, manager_u.id == SupportThread.assigned_manager_id, isouter=True)
        )
        count_stmt = (
            select(func.count())
            .select_from(SupportThread)
            .join(client_u, client_u.id == SupportThread.client_id)
            .join(manager_u, manager_u.id == SupportThread.assigned_manager_id, isouter=True)
        )
        conditions = []
        if statuses:
            conditions.append(SupportThread.status.in_(tuple(statuses)))
        if query:
            stripped = query.strip()
            pattern = f"%{stripped}%"
            text_filters = [
                client_u.full_name.ilike(pattern),
                client_u.phone.ilike(pattern),
                manager_u.full_name.ilike(pattern),
            ]
            parsed_date = _parse_query_date(stripped)
            if parsed_date is not None:
                text_filters.append(cast(SupportThread.created_at, Date) == parsed_date)
            conditions.append(or_(*text_filters))
        if conditions:
            base = base.where(*conditions)
            count_stmt = count_stmt.where(*conditions)

        total = await self.session.scalar(count_stmt)
        rows = await self.session.scalars(
            base.options(
                joinedload(SupportThread.client), joinedload(SupportThread.assigned_manager)
            )
            .order_by(SupportThread.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows.unique()), int(total or 0)

    async def assign_manager(
        self,
        thread: SupportThread,
        manager_id: uuid.UUID | None,
        *,
        status: SupportThreadStatus | None = None,
    ) -> SupportThread:
        thread.assigned_manager_id = manager_id
        if status is not None:
            thread.status = status
        await self.session.flush()
        return thread

    async def close_thread(self, thread: SupportThread) -> SupportThread:
        thread.status = SupportThreadStatus.closed
        thread.closed_at = datetime.now(UTC)
        await self.session.flush()
        return thread

    async def close_open_for_account(self, account_id: uuid.UUID) -> int:
        """Закрыть все незакрытые треды аккаунта (при физическом удалении клиента).

        Уникального индекса на активный тред нет — в проде осели старые дубли, —
        поэтому закрываем набор, а не один. FK `client_id` тредов уйдёт в NULL при
        удалении пользователей (SET NULL, этап 2), но статус `open`/`waiting`
        оставил бы «висящий» тред без клиента в инбоксе дежурного."""
        rows = list(
            await self.session.scalars(
                select(SupportThread).where(
                    SupportThread.account_id == account_id,
                    SupportThread.status.in_(tuple(ACTIVE_STATUSES)),
                )
            )
        )
        now = datetime.now(UTC)
        for thread in rows:
            thread.status = SupportThreadStatus.closed
            thread.closed_at = now
        await self.session.flush()
        return len(rows)

    async def unassign_open_for_manager(self, manager_id: uuid.UUID) -> int:
        """Снять назначение и вернуть в очередь открытые треды менеджера (снятие роли)."""
        rows = list(
            await self.session.scalars(
                select(SupportThread).where(
                    SupportThread.assigned_manager_id == manager_id,
                    SupportThread.status == SupportThreadStatus.open,
                )
            )
        )
        for thread in rows:
            thread.assigned_manager_id = None
            thread.status = SupportThreadStatus.waiting
        await self.session.flush()
        return len(rows)


def _parse_query_date(raw: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None
