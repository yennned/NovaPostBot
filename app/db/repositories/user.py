"""Репозиторий пользователей (`users`)."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.models.enums import UserRole, UserStatus
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
        telegram_id: int,
        phone: str | None = None,
        full_name: str | None = None,
        role: UserRole = UserRole.client,
        status: UserStatus = UserStatus.pending,
        permissions: dict | None = None,
    ) -> User:
        user = User(
            telegram_id=telegram_id,
            phone=phone,
            full_name=full_name,
            role=role,
            status=status,
            permissions=permissions or {},
        )
        await self._add(user)
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

    async def set_duty(self, user: User, *, on_duty: bool, duty_date=None) -> User:
        user.on_duty = on_duty
        user.duty_date = duty_date
        await self.session.flush()
        return user

    async def list_by_role(self, role: UserRole) -> list[User]:
        stmt = select(User).where(User.role == role).order_by(User.created_at)
        return list(await self.session.scalars(stmt))
