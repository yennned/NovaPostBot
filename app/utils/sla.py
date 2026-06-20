"""SLA-хелперы для «30 рабочих минут» в часовом поясе отделения."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.config import Settings, get_settings

WorkingSchedule = dict[int, tuple[str, str]]


def _window_for_day(value: datetime, schedule: WorkingSchedule) -> tuple[datetime, datetime] | None:
    raw = schedule.get(value.weekday())
    if raw is None:
        return None
    start_raw, end_raw = raw
    start = datetime.combine(value.date(), time.fromisoformat(start_raw), tzinfo=value.tzinfo)
    end = datetime.combine(value.date(), time.fromisoformat(end_raw), tzinfo=value.tzinfo)
    if end <= start:
        raise ValueError(f"invalid work window for weekday={value.weekday()}: {raw!r}")
    return start, end


def _next_window_start(value: datetime, schedule: WorkingSchedule) -> datetime:
    cursor = value
    for _ in range(8):
        window = _window_for_day(cursor, schedule)
        if window is not None:
            start, end = window
            if cursor <= start:
                return start
            if start <= cursor < end:
                return cursor
        cursor = datetime.combine(
            cursor.date() + timedelta(days=1),
            time(0, 0),
            tzinfo=cursor.tzinfo,
        )
    raise ValueError("work schedule has no working days")


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
    cursor = _next_window_start(start, schedule)
    while remaining > timedelta():
        window = _window_for_day(cursor, schedule)
        if window is None:
            cursor = _next_window_start(cursor + timedelta(days=1), schedule)
            continue
        _, end = window
        available = end - cursor
        if remaining <= available:
            return cursor + remaining
        remaining -= available
        cursor = _next_window_start(end + timedelta(seconds=1), schedule)
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
