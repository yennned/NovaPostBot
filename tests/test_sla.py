"""Тесты SLA-хелперов рабочих минут."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from app.config import Settings
from app.utils.sla import add_working_minutes, shipment_sla_deadline


@pytest.fixture(autouse=True)
def _no_work_schedule_env(monkeypatch):
    """Убрать `WORK_SCHEDULE` из окружения на время тестов дефолта.

    `Settings(_env_file=None)` отключает только чтение `.env`, но НЕ переменные
    окружения — они по приоритету выше дефолта поля. Без этой фикстуры тесты ниже
    утверждали бы «проверяем дефолт», а на машине с экспортированной переменной
    проверяли бы чужое расписание. Тот же приём, что в `test_settings_defaults`.
    """
    monkeypatch.delenv("WORK_SCHEDULE", raising=False)


def test_add_working_minutes_skips_night():
    tz = ZoneInfo("Europe/Kyiv")
    start = datetime(2026, 6, 22, 21, 0, tzinfo=tz)
    deadline = add_working_minutes(
        start,
        30,
        {0: ("08:00", "20:00"), 1: ("08:00", "20:00")},
    )
    assert deadline == datetime(2026, 6, 23, 8, 30, tzinfo=tz)


def test_shipment_sla_deadline_uses_settings_schedule():
    settings = Settings(_env_file=None)
    settings.work_schedule_raw = '{"0": ["08:00", "20:00"], "1": ["08:00", "20:00"]}'
    start = datetime(2026, 6, 22, 19, 50, tzinfo=ZoneInfo("Europe/Kyiv"))
    deadline = shipment_sla_deadline(start, settings=settings, minutes=30)
    assert deadline == datetime(2026, 6, 23, 8, 20, tzinfo=ZoneInfo("Europe/Kyiv"))


def test_saturday_deadline_stays_on_saturday_by_default():
    """Склад и НП работают 7 дней — субботняя ТТН не имеет права уезжать на понедельник.

    На дефолте `range(0, 5)` этот дедлайн был 2026-06-22 08:30 (понедельник), то есть
    SLA на субботу и воскресенье не значил ничего, а комиссия бралась полная.
    """
    tz = ZoneInfo("Europe/Kyiv")
    settings = Settings(_env_file=None)  # без WORK_SCHEDULE — проверяем именно дефолт
    start = datetime(2026, 6, 20, 10, 0, tzinfo=tz)  # суббота
    assert start.weekday() == 5
    assert shipment_sla_deadline(start, settings=settings, minutes=30) == datetime(
        2026, 6, 20, 10, 30, tzinfo=tz
    )


def test_sunday_deadline_stays_on_sunday_by_default():
    tz = ZoneInfo("Europe/Kyiv")
    settings = Settings(_env_file=None)
    start = datetime(2026, 6, 21, 12, 0, tzinfo=tz)  # воскресенье
    assert start.weekday() == 6
    assert shipment_sla_deadline(start, settings=settings, minutes=30) == datetime(
        2026, 6, 21, 12, 30, tzinfo=tz
    )


def test_after_hours_deadline_moves_to_next_morning():
    """Созданная вне окна 08:00–20:00 — дедлайн 30 минут после открытия следующего дня."""
    tz = ZoneInfo("Europe/Kyiv")
    settings = Settings(_env_file=None)
    start = datetime(2026, 6, 20, 22, 0, tzinfo=tz)  # суббота, после закрытия
    assert shipment_sla_deadline(start, settings=settings, minutes=30) == datetime(
        2026, 6, 21, 8, 30, tzinfo=tz
    )


def test_deadline_spills_over_closing_time():
    """Создана в 19:50 — 10 минут сегодня, остальные 20 добираются наутро."""
    tz = ZoneInfo("Europe/Kyiv")
    settings = Settings(_env_file=None)
    start = datetime(2026, 6, 20, 19, 50, tzinfo=tz)
    assert shipment_sla_deadline(start, settings=settings, minutes=30) == datetime(
        2026, 6, 21, 8, 20, tzinfo=tz
    )
