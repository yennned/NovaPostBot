"""Тесты SLA-хелперов рабочих минут."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Settings
from app.utils.sla import add_working_minutes, shipment_sla_deadline


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
