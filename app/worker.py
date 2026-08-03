"""Фоновый воркер Phase 5: трекинг НП и low-stock."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from redis.asyncio import from_url as redis_from_url

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
    stock_reconcile_job,
)
from app.logging_config import configure_logging, get_logger
from app.novaposhta.client import NovaPoshtaClient
from app.sheets import build_stock_source
from app.utils.heartbeat import run_heartbeat
from app.utils.work_schedule import is_open, is_open_or_recently_closed, schedule_summary

_log = get_logger("worker")


def _now(settings: Settings) -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def _should_run_daytime(settings: Settings, at: datetime) -> bool:
    """Дневные задачи (трекинг, low-stock) — только в рабочие часы.

    **Экономим квоту Google и НП, а не Neon.** Прежнее обоснование — «чтобы ночью
    Neon (scale-to-zero) засыпал» — не работает ни при какой конфигурации:
    `stock_hold_sweep_job` ниже не гейтится вовсе и ходит в БД каждые 120 с
    круглосуточно, то есть пятиминутный простой (`suspend_timeout_seconds = 300`
    на боевом endpoint, проверено 2026-08-03) не набирается никогда. Что гейт
    действительно даёт: ночью не тратятся вызовы `TrackingDocument.getStatusDocuments`
    и чтения листа склада — а вот они лимитированы жёстко (60 read/min на
    service-account) и ночью бесполезны: статусы НП ночью не меняются, а на низкий
    остаток всё равно некому реагировать.

    Пустое расписание = гейт выключен (деградируем к «поллим всегда», а не «молчим
    вечно» — безопасный дефолт при мисконфиге).
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


async def stock_ingest_tick(*, notifier, settings: Settings, now: datetime | None = None):
    """Ингест приёмки — круглосуточно, БЕЗ гейта рабочих часов.

    Гейт здесь был, и обоснование звучало разумно: «приёмку вносят работники склада,
    а они вне окна не работают». Оно оказалось предположением о людях, а цена ошибки
    несимметрична. Раз чтение остатка переключено на PG (`INVENTORY_SOURCE=pg`),
    приёмка, внесённая в 20:30, до утра не видна **никому**: клиент видит вчерашний
    остаток, а гейт от oversell отказывает в ТТН на товар, который физически на складе
    лежит. Двенадцать часов такой слепоты неотличимы от «бот потерял приёмку» — ровно
    та жалоба, ради которой всё это чинится.

    Экономия, которую гейт давал, при этом мнимая: проход стоит ОДНО чтение Google в
    минуту, то есть ~1.7 % минутной квоты service-account (60 read/min), и суточных
    лимитов у Sheets API нет. Дорогие ночью — трекинг НП и зеркало (чтение + запись на
    каждый аккаунт); они под гейтом и остаются.

    `now` больше не влияет ни на что и оставлен ради единообразия сигнатур джоб.
    """
    return await stock_ingest_job(notifier=notifier, settings=settings)


async def stock_mirror_gated(*, notifier, settings: Settings, now: datetime | None = None):
    """Зеркало склада — в рабочие часы: ночью в листе никто ничего не правит, а
    проход стоит чтение и запись Google на каждый аккаунт."""
    at = now or _now(settings)
    if not _should_run_daytime(settings, at):
        _log.debug("worker.skip", job="stock_mirror", reason="closed")
        return None
    return await stock_mirror_job(notifier=notifier, settings=settings)


async def stock_reconcile_gated(*, notifier, settings: Settings, now: datetime | None = None):
    """Сверка — в рабочие часы: ночью расхождение всё равно некому разбирать."""
    at = now or _now(settings)
    if not _should_run_daytime(settings, at):
        _log.debug("worker.skip", job="stock_reconcile", reason="closed")
        return None
    return await stock_reconcile_job(notifier=notifier, settings=settings)


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
    redis_client = redis_from_url(settings.redis_url)
    mutator = build_stock_source(settings)
    bot = Bot(token=settings.bot_token) if settings.bot_token else None
    notifier = BotNotifier(bot) if bot is not None else None

    # Джобы обёрнуты в *_gated: вне рабочих часов не тратят квоту НП и Google.
    # Про Neon см. _should_run_daytime — засыпать ему всё равно не дают.
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
            stock_ingest_tick,
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
    if settings.stock_reconcile_enabled:
        scheduler.add_job(
            stock_reconcile_gated,
            trigger="interval",
            seconds=settings.stock_reconcile_seconds,
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
    # Пульс живости — обычной asyncio-задачей, а НЕ джобой планировщика:
    # умерший планировщик обязан быть виден снаружи, а джоба, которая
    # рапортует о собственном планировщике, при его смерти просто замолчит
    # вместе с ним и ничего не сообщит. Гейта рабочих часов здесь нет:
    # иначе воркер каждую ночь объявлял бы себя мёртвым.
    heartbeat = asyncio.create_task(run_heartbeat(redis_client, "worker"))
    try:
        await asyncio.Event().wait()
    finally:
        heartbeat.cancel()
        scheduler.shutdown(wait=False)
        await np_client.aclose()
        await redis_client.aclose()
        if bot is not None:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
