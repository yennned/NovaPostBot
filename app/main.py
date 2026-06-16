"""Точка входа бота (long polling).

Фаза 0 — каркас: загрузка конфигурации и логирования, запускаемый контейнер.
Сборка aiogram Dispatcher и `start_polling` появятся в Фазе 1.
"""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.logging_config import configure_logging, get_logger


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    log = get_logger("bot")
    log.info("bot.start", timezone=settings.timezone)

    # TODO (Фаза 1): Bot/Dispatcher, middlewares, роутеры, start_polling.
    await asyncio.Event().wait()  # держим процесс живым (каркас)


if __name__ == "__main__":
    asyncio.run(main())
