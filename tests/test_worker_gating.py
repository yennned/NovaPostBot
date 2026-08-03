"""Гейт воркера по рабочему расписанию: что ночью спит, а что обязано не спать.

Гейт экономит квоту Google и НП, а не «сон Neon» (это обоснование разобрано в
`worker._should_run_daytime`). Отсюда и граница: дорогие ночью проходы — трекинг,
зеркало, сверка — под гейтом; ингест приёмки, который стоит одно чтение в минуту и
без которого остаток до утра слепнет, — нет.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app import worker

TZ = ZoneInfo("Europe/Kyiv")
# Расписание по умолчанию — Пн-Пт 08:00-20:00; 2026-06-22 понедельник, 28 — воскресенье.
SETTINGS = SimpleNamespace(
    work_schedule=dict.fromkeys(range(5), ("08:00", "20:00")),
    duty_check_seconds=300,  # grace = 2*300s = 10 мин после закрытия
    timezone="Europe/Kyiv",
)
EMPTY = SimpleNamespace(work_schedule={}, duty_check_seconds=300, timezone="Europe/Kyiv")


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=TZ)


def test_daytime_gate_open_vs_closed():
    assert worker._should_run_daytime(SETTINGS, _at(22, 10)) is True
    assert worker._should_run_daytime(SETTINGS, _at(22, 23)) is False  # ночь
    assert worker._should_run_daytime(SETTINGS, _at(28, 12)) is False  # выходной


def test_daytime_gate_disabled_on_empty_schedule():
    # Мисконфиг (пустое расписание) → поллим всегда, а не молчим вечно.
    assert worker._should_run_daytime(EMPTY, _at(22, 23)) is True


def test_duty_gate_runs_during_and_shortly_after_close():
    assert worker._should_run_duty(SETTINGS, _at(22, 10)) is True  # открыто
    assert worker._should_run_duty(SETTINGS, _at(22, 20, 5)) is True  # в grace после закрытия
    assert worker._should_run_duty(SETTINGS, _at(22, 20, 30)) is False  # grace прошёл
    assert worker._should_run_duty(SETTINGS, _at(22, 3)) is False  # глубокая ночь


async def test_poll_tracking_gated_skips_when_closed(monkeypatch):
    called = False

    async def fake_job(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(worker, "poll_tracking_job", fake_job)
    result = await worker.poll_tracking_gated(
        np_client=object(),
        notifier=None,
        mutator=None,
        settings=SETTINGS,
        now=_at(22, 23),
    )
    assert result is None
    assert called is False


async def test_poll_tracking_gated_runs_when_open(monkeypatch):
    called = False

    async def fake_job(**kwargs):
        nonlocal called
        called = True
        return "ran"

    monkeypatch.setattr(worker, "poll_tracking_job", fake_job)
    result = await worker.poll_tracking_gated(
        np_client=object(),
        notifier=None,
        mutator=None,
        settings=SETTINGS,
        now=_at(22, 10),
    )
    assert result == "ran"
    assert called is True


async def test_stock_ingest_runs_at_night_and_on_weekends(monkeypatch):
    """Ингест приёмки НЕ под гейтом рабочих часов — и это проверяется явно.

    Пока он там был, приёмка, внесённая в 20:30, доезжала в Postgres только к утру:
    клиент видел вчерашний остаток, а гейт от oversell отказывал в ТТН на товар,
    который лежит на складе. Зеркало и трекинг ночью спят по-прежнему — они стоят
    чтение и запись на аккаунт, а ингест стоит одно чтение в минуту.
    """
    calls = 0

    async def fake_job(**kwargs):
        nonlocal calls
        calls += 1
        return "ran"

    monkeypatch.setattr(worker, "stock_ingest_job", fake_job)
    assert (
        await worker.stock_ingest_tick(notifier=None, settings=SETTINGS, now=_at(22, 23)) == "ran"
    )
    assert await worker.stock_ingest_tick(notifier=None, settings=SETTINGS, now=_at(28, 3)) == "ran"
    assert calls == 2

    # Зеркало для контраста осталось под гейтом: тест обязан падать, если кто-то
    # снимет гейт заодно и с него, не подумав про цену прохода.
    monkeypatch.setattr(worker, "stock_mirror_job", fake_job)
    assert (
        await worker.stock_mirror_gated(notifier=None, settings=SETTINGS, now=_at(22, 23)) is None
    )
    assert calls == 2


async def test_clear_expired_duty_gated_runs_after_close(monkeypatch):
    called = False

    async def fake_job(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(worker, "clear_expired_duty_job", fake_job)
    await worker.clear_expired_duty_gated(notifier=None, settings=SETTINGS, now=_at(22, 20, 5))
    assert called is True
