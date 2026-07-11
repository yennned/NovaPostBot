"""Репозиторий пользователей (`users`)."""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select

from app.db.models.client_account import ClientAccountMembership
from app.db.models.enums import MembershipRole, UserRole, UserStatus
from app.db.models.user import User
from app.db.repositories.base import BaseRepository


class UserRepository(BaseRepository):
    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        stmt = select(User).where(User.telegram_id == telegram_id)
        return await self.session.scalar(stmt)

    async def get_by_phone(self, phone: str) -> User | None:
        stmt = select(User).where(User.phone == phone)
        return await self.session.scalar(stmt)

    async def create(
        self,
        *,
        telegram_id: int | None = None,
        phone: str | None = None,
        full_name: str | None = None,
        stock_sheet_key: str | None = None,
        stock_view_book_id: str | None = None,
        role: UserRole = UserRole.client,
        status: UserStatus = UserStatus.pending,
        permissions: dict | None = None,
        create_account: bool = True,
        account_name: str | None = None,
    ) -> User:
        user = User(
            telegram_id=telegram_id,
            phone=phone,
            full_name=full_name,
            stock_sheet_key=stock_sheet_key or full_name or phone or str(telegram_id),
            stock_view_book_id=stock_view_book_id,
            role=role,
            status=status,
            permissions=permissions or {},
        )
        await self._add(user)
        if create_account and role is UserRole.client:
            from app.db.repositories.client_account import ClientAccountRepository

            await ClientAccountRepository(self.session).create_for_owner(
                user, name=account_name, stock_sheet_key=user.stock_sheet_key
            )
        return user

    async def update_status(self, user: User, status: UserStatus) -> User:
        user.status = status
        await self.session.flush()
        return user

    async def update_role(self, user: User, role: UserRole) -> User:
        user.role = role
        await self.session.flush()
        return user

    async def set_permissions(self, user: User, permissions: dict) -> User:
        user.permissions = permissions
        await self.session.flush()
        return user

    async def set_duty(self, user: User, *, on_duty: bool, duty_date=None, duty_since=None) -> User:
        user.on_duty = on_duty
        user.duty_date = duty_date
        user.duty_since = duty_since
        await self.session.flush()
        return user

    async def list_by_role(self, role: UserRole) -> list[User]:
        stmt = select(User).where(User.role == role).order_by(User.created_at)
        return list(await self.session.scalars(stmt))

    async def list_by_status(
        self,
        *,
        role: UserRole = UserRole.client,
        status: UserStatus | None = None,
        query: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[User], int]:
        """Страница пользователей с фильтром по статусу и поиском по ПІБ/телефону.

        Возвращает `(строки, total)` — total для пагинации (без limit/offset).
        По умолчанию роль `client` (управление клиентами); поиск — подстрокой
        (регистронезависимо) по `full_name`/`phone`.
        """
        conditions = [User.role == role]
        include = select(User)
        count_from = select(func.count()).select_from(User)
        if role is UserRole.client:
            include = include.outerjoin(
                ClientAccountMembership, ClientAccountMembership.user_id == User.id
            )
            count_from = count_from.outerjoin(
                ClientAccountMembership, ClientAccountMembership.user_id == User.id
            )
            conditions.append(
                or_(
                    ClientAccountMembership.id.is_(None),
                    ClientAccountMembership.role == MembershipRole.account_owner,
                )
            )
        if status is not None:
            conditions.append(User.status == status)
        if query:
            pattern = f"%{query.strip()}%"
            conditions.append(or_(User.full_name.ilike(pattern), User.phone.ilike(pattern)))

        total = await self.session.scalar(count_from.where(*conditions))
        rows = await self.session.scalars(
            include.where(*conditions).order_by(User.created_at.desc()).limit(limit).offset(offset)
        )
        return list(rows), int(total or 0)

    async def count_by_status(self, *, role: UserRole = UserRole.client) -> dict[UserStatus, int]:
        """Счётчики по статусам для роли (вкладки/бейджи). Нулевые статусы — 0."""
        stmt = select(User.status, func.count()).where(User.role == role)
        if role is UserRole.client:
            stmt = stmt.outerjoin(
                ClientAccountMembership, ClientAccountMembership.user_id == User.id
            ).where(
                or_(
                    ClientAccountMembership.id.is_(None),
                    ClientAccountMembership.role == MembershipRole.account_owner,
                )
            )
        stmt = stmt.group_by(User.status)
        rows = await self.session.execute(stmt)
        counts: dict[UserStatus, int] = dict.fromkeys(UserStatus, 0)
        for status, count in rows:
            counts[status] = int(count)
        return counts
