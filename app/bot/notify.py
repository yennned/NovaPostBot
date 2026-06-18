"""Адаптер `Notifier` поверх aiogram `Bot` (бот-слой).

Доменный `app/services/notifications.py` принимает `Notifier`-протокол; здесь
конкретная реализация, которая шлёт сообщения и **глотает** ошибки доставки
отдельным получателям (сбой одного не должен валить флоу регистрации/подтверждения).
"""

from __future__ import annotations

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

logger = structlog.get_logger(__name__)


class BotNotifier:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send_message(self, telegram_id: int, text: str) -> None:
        try:
            await self.bot.send_message(telegram_id, text, parse_mode="HTML")
        except TelegramAPIError as exc:
            logger.warning("notify_failed", telegram_id=telegram_id, error=str(exc))
