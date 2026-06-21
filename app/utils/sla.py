"""SLA-хелперы для «30 рабочих минут» в часовом поясе отделения.

Оконная логика расписания вынесена в [app/utils/work_schedule.py](work_schedule.py)
(единый источник правды, общий с дежурством Фазы 6).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import Settings, get_settings
from app.utils.work_schedule import WorkingSchedule, next_window_start, window_for_day


def add_working_minutes(
    start: datetime,
    minutes: int,
    schedule: WorkingSchedule,
) -> datetime:
    """Добавить рабочие минуты, пропуская выходные и нерабочие часы."""
    if minutes < 0:
        raise ValueError("minutes must be >= 0")
    if minutes == 0:
        return start

    remaining = timedelta(minutes=minutes)
    cursor = next_window_start(start, schedule)
    while remaining > timedelta():
        window = window_for_day(cursor, schedule)
        if window is None:
            cursor = next_window_start(cursor + timedelta(days=1), schedule)
            continue
        _, end = window
        available = end - cursor
        if remaining <= available:
            return cursor + remaining
        remaining -= available
        cursor = next_window_start(end + timedelta(seconds=1), schedule)
    return cursor


def shipment_sla_deadline(
    created_at: datetime,
    *,
    settings: Settings | None = None,
    minutes: int = 30,
) -> datetime:
    current_settings = settings or get_settings()
    tz = ZoneInfo(current_settings.timezone)
    start = created_at.astimezone(tz)
    return add_working_minutes(start, minutes, current_settings.work_schedule)


def sla_met(
    *,
    dispatched_at: datetime,
    deadline: datetime | None,
) -> bool | None:
    if deadline is None:
        return None
    return dispatched_at <= deadline
