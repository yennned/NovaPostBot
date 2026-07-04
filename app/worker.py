"""Фоновый воркер Phase 5: трекинг НП и low-stock."""

from __future__ import annotations

import asyncio

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot.notify import BotNotifier
from app.config import get_settings
from app.jobs import clear_expired_duty_job, low_stock_job, poll_tracking_job
from app.logging_config import configure_logging, get_logger
from app.novaposhta.client import NovaPoshtaClient
from app.sheets import build_stock_source


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    log = get_logger("worker")
    log.info(
        "worker.start",
        version=settings.app_version,
        environment=settings.environment,
        timezone=settings.timezone,
    )
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    np_client = NovaPoshtaClient(settings=settings)
    mutator = build_stock_source(settings)
    bot = Bot(token=settings.bot_token) if settings.bot_token else None
    notifier = BotNotifier(bot) if bot is not None else None

    scheduler.add_job(
        poll_tracking_job,
        trigger="interval",
        seconds=settings.tracking_poll_seconds,
        kwargs={
            "np_client": np_client,
            "notifier": notifier,
            "mutator": mutator,
            "settings": settings,
        },
        max_instances=1,
        coalesce=True,
    )
    if notifier is not None:
        scheduler.add_job(
            low_stock_job,
            trigger="interval",
            seconds=settings.low_stock_poll_seconds,
            kwargs={"notifier": notifier, "settings": settings},
            max_instances=1,
            coalesce=True,
        )
    scheduler.add_job(
        clear_expired_duty_job,
        trigger="interval",
        seconds=settings.duty_check_seconds,
        kwargs={"notifier": notifier, "settings": settings},
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)
        await np_client.aclose()
        if bot is not None:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
