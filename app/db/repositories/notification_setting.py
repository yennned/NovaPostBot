"""Репозиторий пользовательских настроек уведомлений."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

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

    async def map_for_users(
        self, user_ids: Sequence[uuid.UUID], keys: Sequence[str]
    ) -> dict[tuple[uuid.UUID, str], bool]:
        """Настройки сразу по всем получателям веера — одним запросом.

        Статус-пуш обходил участников аккаунта циклом и на каждого делал один-два
        `get_by_user_and_key`. Это `1 + (1..2)×N` запросов **подряд**, то есть
        задержка росла как N × RTT до Neon, а не как max(RTT). На аккаунте с
        владельцем и пятью работниками — до тринадцати round-trip'ов ради одного
        уведомления, и всё это внутри прохода трекинга, который таких вееров
        выдаёт сотню за раз.
        """
        if not user_ids or not keys:
            return {}
        rows = await self.session.scalars(
            select(NotificationSetting).where(
                NotificationSetting.user_id.in_(tuple(user_ids)),
                NotificationSetting.key.in_(tuple(keys)),
            )
        )
        return {(row.user_id, row.key): row.enabled for row in rows}
