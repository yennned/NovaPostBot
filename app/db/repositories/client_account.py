"""Репозиторій бізнес-акаунтів і членств."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload

from app.db.models.client_account import ClientAccount, ClientAccountMembership
from app.db.models.enums import ClientAccountStatus, MembershipRole, MembershipStatus
from app.db.models.user import User
from app.db.repositories.base import BaseRepository


class ClientAccountRepository(BaseRepository):
    async def get_by_id(self, account_id: uuid.UUID) -> ClientAccount | None:
        return await self.session.get(ClientAccount, account_id)

    async def get_membership(
        self, *, user_id: uuid.UUID, account_id: uuid.UUID | None = None
    ) -> ClientAccountMembership | None:
        conditions = [ClientAccountMembership.user_id == user_id]
        if account_id is not None:
            conditions.append(ClientAccountMembership.account_id == account_id)
        stmt = (
            select(ClientAccountMembership)
            .options(
                joinedload(ClientAccountMembership.account),
                joinedload(ClientAccountMembership.user),
            )
            .where(*conditions)
        )
        return await self.session.scalar(stmt)

    async def get_context_for_user(
        self, user_id: uuid.UUID
    ) -> tuple[ClientAccount, ClientAccountMembership] | None:
        """Активный бизнес-контекст пользователя или `None`.

        Статус аккаунта проверяем наравне с членством: при блокировке клиента
        `clients._transition` гасит `account.status`, но членства его работников
        остаются active. Без этой проверки работники заблокированного клиента
        сохраняли бы полный доступ к складу/ФОП/ТТН акаунта.
        """
        membership = await self.get_membership(user_id=user_id)
        if membership is None or membership.status is not MembershipStatus.active:
            return None
        if membership.account.status is not ClientAccountStatus.active:
            return None
        return membership.account, membership

    async def create_for_owner(
        self,
        owner: User,
        *,
        name: str | None = None,
        account_id: uuid.UUID | None = None,
        stock_sheet_key: str | None = None,
        stock_view_book_id: str | None = None,
    ) -> tuple[ClientAccount, ClientAccountMembership]:
        account = ClientAccount(
            # Keep the legacy identity stable for the first rollout: the owner
            # UUID is also the account UUID.  New account-scoped code never
            # relies on this coincidence, but it lets old read paths coexist
            # while the migration is rolled out.
            id=account_id or owner.id,
            name=name or owner.full_name or owner.phone or f"Клієнт {owner.id}",
            status=ClientAccountStatus.active,
            stock_sheet_key=stock_sheet_key or owner.stock_sheet_key,
            stock_view_book_id=stock_view_book_id or owner.stock_view_book_id,
        )
        self.session.add(account)
        await self.session.flush()
        membership = ClientAccountMembership(
            account_id=account.id,
            user_id=owner.id,
            role=MembershipRole.account_owner,
            status=MembershipStatus.active,
            joined_at=datetime.now(UTC),
        )
        await self._add(membership)
        return account, membership

    async def list_members(
        self,
        account_id: uuid.UUID,
        *,
        status: MembershipStatus | None = None,
        query: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[ClientAccountMembership], int]:
        conditions = [ClientAccountMembership.account_id == account_id]
        if status is not None:
            conditions.append(ClientAccountMembership.status == status)
        if query:
            pattern = f"%{query.strip()}%"
            conditions.append(or_(User.full_name.ilike(pattern), User.phone.ilike(pattern)))
        base = select(ClientAccountMembership).join(
            User, User.id == ClientAccountMembership.user_id
        )
        count = (
            select(func.count())
            .select_from(ClientAccountMembership)
            .join(User, User.id == ClientAccountMembership.user_id)
            .where(*conditions)
        )
        total = int(await self.session.scalar(count) or 0)
        rows = await self.session.scalars(
            base.options(
                joinedload(ClientAccountMembership.user),
                joinedload(ClientAccountMembership.account),
            )
            .where(*conditions)
            .order_by(ClientAccountMembership.created_at)
            .limit(limit)
            .offset(offset)
        )
        return list(rows.unique()), total

    async def create_invited_membership(
        self,
        *,
        account_id: uuid.UUID,
        user: User,
        invited_by_user_id: uuid.UUID,
    ) -> ClientAccountMembership:
        membership = ClientAccountMembership(
            account_id=account_id,
            user_id=user.id,
            user=user,
            role=MembershipRole.employee,
            status=MembershipStatus.invited,
            invited_by_user_id=invited_by_user_id,
        )
        await self._add(membership)
        return membership

    async def set_membership_status(
        self,
        membership: ClientAccountMembership,
        status: MembershipStatus,
    ) -> ClientAccountMembership:
        membership.status = status
        now = datetime.now(UTC)
        if status is MembershipStatus.active and membership.joined_at is None:
            membership.joined_at = now
        membership.blocked_at = now if status is MembershipStatus.blocked else None
        await self.session.flush()
        return membership

    async def count_active_owners(self, account_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(
            ClientAccountMembership.account_id == account_id,
            ClientAccountMembership.role == MembershipRole.account_owner,
            ClientAccountMembership.status == MembershipStatus.active,
        )
        return int(await self.session.scalar(stmt) or 0)
