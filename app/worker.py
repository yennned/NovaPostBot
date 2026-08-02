"""Фоновый воркер Phase 5: трекинг НП и low-stock."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot.notify import BotNotifier
from app.config import Settings, get_settings
from app.jobs import (
    clear_expired_duty_job,
    low_stock_job,
    poll_returns_job,
    poll_tracking_job,
    stock_hold_sweep_job,
    stock_ingest_job,
    stock_mirror_job,
)
from app.logging_config import configure_logging, get_logger
from app.novaposhta.client import NovaPoshtaClient
from app.sheets import build_stock_source
from app.utils.work_schedule import is_open, is_open_or_recently_closed, schedule_summary

_log = get_logger("worker")


def _now(settings: Settings) -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def _should_run_daytime(settings: Settings, at: datetime) -> bool:
    """Дневные задачи (трекинг, low-stock) — только в рабочие часы.

    Вне окна ничего не делаем и НЕ трогаем БД, чтобы ночью Neon (scale-to-zero)
    засыпал. Пустое расписание = гейт выключен (деградируем к «поллим всегда»,
    а не «молчим вечно» — безопасный дефолт при мисконфиге).
    """
    schedule = settings.work_schedule
    return not schedule or is_open(at, schedule)


def _should_run_duty(settings: Settings, at: datetime) -> bool:
    """Авто-снятие дежурства — в рабочие часы и короткое время после закрытия.

    Grace ≥ интервала джобы, чтобы хотя бы один тик гарантированно попал в окно
    после закрытия и снял дежурство; дальше — тишина до следующего открытия.
    """
    schedule = settings.work_schedule
    if not schedule:
        return True
    grace = timedelta(seconds=2 * settings.duty_check_seconds)
    return is_open_or_recently_closed(at, schedule, grace)


async def poll_tracking_gated(
    *, np_client, notifier, mutator, settings: Settings, now: datetime | None = None
):
    at = now or _now(settings)
    if not _should_run_daytime(settings, at):
        _log.debug("worker.skip", job="poll_tracking", reason="closed")
        return None
    return await poll_tracking_job(
        np_client=np_client, notifier=notifier, mutator=mutator, settings=settings
    )


async def poll_returns_gated(
    *, np_client, notifier, mutator, settings: Settings, now: datetime | None = None
):
    at = now or _now(settings)
    if not _should_run_daytime(settings, at):
        _log.debug("worker.skip", job="poll_returns", reason="closed")
        return None
    return await poll_returns_job(
        np_client=np_client, notifier=notifier, mutator=mutator, settings=settings
    )


async def low_stock_gated(*, notifier, settings: Settings, now: datetime | None = None):
    at = now or _now(settings)
    if not _should_run_daytime(settings, at):
        _log.debug("worker.skip", job="low_stock", reason="closed")
        return None
    return await low_stock_job(notifier=notifier, settings=settings)


async def stock_ingest_gated(*, notifier, settings: Settings, now: datetime | None = None):
    """Ингест приёмки — в рабочие часы: приёмку вносят работники склада, а они
    вне окна не работают. Ночью это была бы минута за минутой пустого чтения Google."""
    at = now or _now(settings)
    if not _should_run_daytime(settings, at):
        _log.debug("worker.skip", job="stock_ingest", reason="closed")
        return None
    return await stock_ingest_job(notifier=notifier, settings=settings)


async def stock_mirror_gated(*, notifier, settings: Settings, now: datetime | None = None):
    """Зеркало склада — в рабочие часы: ночью в листе никто ничего не правит, а
    проход стоит чтение и запись Google на каждый аккаунт."""
    at = now or _now(settings)
    if not _should_run_daytime(settings, at):
        _log.debug("worker.skip", job="stock_mirror", reason="closed")
        return None
    return await stock_mirror_job(notifier=notifier, settings=settings)


async def clear_expired_duty_gated(*, notifier, settings: Settings, now: datetime | None = None):
    at = now or _now(settings)
    if not _should_run_duty(settings, at):
        _log.debug("worker.skip", job="clear_expired_duty", reason="closed")
        return None
    return await clear_expired_duty_job(notifier=notifier, settings=settings)


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
    # Для воркера это критичнее, чем для бота: день, отсутствующий в расписании, —
    # это день без трекинга и без low-stock, и снаружи он выглядит как тишина.
    log.info("work_schedule.effective", **schedule_summary(settings.work_schedule))
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    np_client = NovaPoshtaClient(settings=settings)
    mutator = build_stock_source(settings)
    bot = Bot(token=settings.bot_token) if settings.bot_token else None
    notifier = BotNotifier(bot) if bot is not None else None

    # Джобы обёрнуты в *_gated: вне рабочих часов не трогают БД, чтобы Neon
    # (scale-to-zero) засыпал ночью. См. _should_run_daytime / _should_run_duty.
    scheduler.add_job(
        poll_tracking_gated,
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
    # Возвраты — отдельной джобой и на порядки реже трекинга: окно опроса узкое
    # (`dispatched_at` за 3–21 день), каждый документ проверяется не чаще раза в
    # сутки, поэтому проход стоит единицы запросов к НП.
    scheduler.add_job(
        poll_returns_gated,
        trigger="interval",
        seconds=settings.returns_poll_seconds,
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
            low_stock_gated,
            trigger="interval",
            seconds=settings.low_stock_poll_seconds,
            kwargs={"notifier": notifier, "settings": settings},
            max_instances=1,
            coalesce=True,
        )
    # Ингест приёмки выключен по умолчанию (`STOCK_INGEST_ENABLED`): порядок
    # выкатки — backfill балансов, потом ингест на наблюдении, и только потом
    # переключение чтения на PG. Джоба, включённая до backfill'а, построила бы
    # баланс из одних приходов, без стартового остатка.
    if settings.stock_ingest_enabled:
        scheduler.add_job(
            stock_ingest_gated,
            trigger="interval",
            seconds=settings.stock_ingest_seconds,
            kwargs={"notifier": notifier, "settings": settings},
            max_instances=1,
            coalesce=True,
        )
    # Зеркало — после ингеста и реже него: приёмка, попавшая между ними, доедет
    # следующим циклом, а обратный порядок писал бы в лист остаток, ещё не знающий
    # о только что внесённой приёмке.
    if settings.stock_mirror_enabled:
        scheduler.add_job(
            stock_mirror_gated,
            trigger="interval",
            seconds=settings.stock_mirror_seconds,
            kwargs={"notifier": notifier, "settings": settings},
            max_instances=1,
            coalesce=True,
        )
    # Дворник броней НЕ гейтится рабочими часами: бронь, оставшаяся от падения в
    # 19:59, иначе провисела бы до утра и всё это время занижала доступный остаток.
    scheduler.add_job(
        stock_hold_sweep_job,
        trigger="interval",
        seconds=settings.stock_hold_sweep_seconds,
        kwargs={"settings": settings},
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        clear_expired_duty_gated,
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
