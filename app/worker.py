"""Фоновый воркер (APScheduler).

Фаза 0 — каркас. Трекинг НП, продвижение статусов, SLA-таймер и low-stock
появятся в Фазе 5.
"""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.logging_config import configure_logging, get_logger


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    log = get_logger("worker")
    log.info("worker.start", timezone=settings.timezone)

    # TODO (Фаза 5): APScheduler — опрос трекинга НП, SLA, low-stock.
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
