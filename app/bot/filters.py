"""Фильтры bot-layer."""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import Message

from app.bot.services import DevService


class IsDevFilter(BaseFilter):
    async def __call__(self, message: Message, dev_service: DevService) -> bool:
        if message.from_user is None:
            return False
        return dev_service.is_dev(message.from_user.id)
