"""Тесты гейта воркера по рабочему расписанию (ночью не будим Neon)."""

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


async def test_clear_expired_duty_gated_runs_after_close(monkeypatch):
    called = False

    async def fake_job(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(worker, "clear_expired_duty_job", fake_job)
    await worker.clear_expired_duty_gated(notifier=None, settings=SETTINGS, now=_at(22, 20, 5))
    assert called is True
