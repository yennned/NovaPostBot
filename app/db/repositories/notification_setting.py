"""Репозиторий пользовательских настроек уведомлений."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.models.notification_setting import NotificationSetting
from app.db.repositories.base import BaseRepository


class NotificationSettingRepository(BaseRepository):
    async def list_for_user(self, user_id: uuid.UUID) -> list[NotificationSetting]:
        stmt = (
            select(NotificationSetting)
            .where(NotificationSetting.user_id == user_id)
            .order_by(NotificationSetting.created_at)
        )
        return list(await self.session.scalars(stmt))

    async def get_by_user_and_key(self, user_id: uuid.UUID, key: str) -> NotificationSetting | None:
        stmt = select(NotificationSetting).where(
            NotificationSetting.user_id == user_id,
            NotificationSetting.key == key,
        )
        return await self.session.scalar(stmt)

    async def set_enabled(
        self, *, user_id: uuid.UUID, key: str, enabled: bool
    ) -> NotificationSetting:
        setting = await self.get_by_user_and_key(user_id, key)
        if setting is None:
            setting = NotificationSetting(user_id=user_id, key=key, enabled=enabled)
            await self._add(setting)
            return setting
        setting.enabled = enabled
        await self.session.flush()
        return setting
