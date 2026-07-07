"""Рабочее расписание отделения — окна по дням недели (Europe/Kyiv).

Источник правды — dev-конфиг `WORK_SCHEDULE` (`Settings.work_schedule`), нигде в
UI не настраивается ([docs/10-support-duty.md](../../docs/10-support-duty.md)).
Здесь — чистые функции над расписанием: окно дня, ближайшее открытие, «открыто ли
сейчас» и «когда закроется текущее окно». Используются и SLA-таймером
([app/utils/sla.py](sla.py)), и дежурством Фазы 6 (маршрутизация поддержки,
авто-снятие смены при закрытии отделения).

Все `datetime`-аргументы должны быть tz-aware в часовом поясе отделения — окна
строятся на дате аргумента с его `tzinfo`.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

WorkingSchedule = dict[int, tuple[str, str]]


def window_for_day(value: datetime, schedule: WorkingSchedule) -> tuple[datetime, datetime] | None:
    """Рабочее окно `(start, end)` для дня `value` или `None`, если день выходной."""
    raw = schedule.get(value.weekday())
    if raw is None:
        return None
    start_raw, end_raw = raw
    start = datetime.combine(value.date(), time.fromisoformat(start_raw), tzinfo=value.tzinfo)
    end = datetime.combine(value.date(), time.fromisoformat(end_raw), tzinfo=value.tzinfo)
    if end <= start:
        raise ValueError(f"invalid work window for weekday={value.weekday()}: {raw!r}")
    return start, end


def next_window_start(value: datetime, schedule: WorkingSchedule) -> datetime:
    """Момент ближайшего открытия отделения в `value` или после него."""
    cursor = value
    for _ in range(8):
        window = window_for_day(cursor, schedule)
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


def is_open(at: datetime, schedule: WorkingSchedule) -> bool:
    """Открыто ли отделение в момент `at` (попадает в рабочее окно своего дня)."""
    window = window_for_day(at, schedule)
    if window is None:
        return False
    start, end = window
    return start <= at < end


def is_open_or_recently_closed(at: datetime, schedule: WorkingSchedule, grace: timedelta) -> bool:
    """Открыто сейчас ИЛИ окно дня закрылось не более `grace` назад.

    Для пост-закрывающих задач воркера (авто-снятие дежурства): им нужно отработать
    один раз после закрытия отделения, но молчать всю ночь — чтобы БД (Neon со
    scale-to-zero) успевала уснуть и не тарифицировалась круглосуточно.
    """
    if is_open(at, schedule):
        return True
    window = window_for_day(at, schedule)
    if window is None:
        return False
    _, end = window
    return end <= at < end + grace


def current_window_end(at: datetime, schedule: WorkingSchedule) -> datetime | None:
    """Конец текущего рабочего окна, если в `at` открыто; иначе `None`."""
    window = window_for_day(at, schedule)
    if window is None:
        return None
    start, end = window
    if start <= at < end:
        return end
    return None
