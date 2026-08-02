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


def sla_verdict(
    *,
    scanned_at: datetime | None,
    previous_poll_at: datetime | None,
    detected_at: datetime,
    deadline: datetime | None,
) -> bool | None:
    """Успели ли: `True` / `False` / `None`, если честно не знаем.

    Раньше вердикт считался от `detected_at` — момента, когда МЫ заметили отправку.
    Это ставило нашу выручку в зависимость от задержки собственного опроса: посылка,
    уехавшая вовремя, но замеченная позже дедлайна, помечалась промахом и обнуляла
    комиссию (`fee_free`). Политика SLA при этом сознательно защищает старт отсчёта
    от манипуляций менеджера — логично защитить и финиш.

    Приоритет источников:

    1. `scanned_at` — время сканирования от НП. Единственное, которым не может
       управлять ни менеджер, ни мы. Есть — сравниваем прямо, вердикт окончательный.
    2. Нет времени от НП, но `detected_at <= deadline` — отправка произошла не позже
       обнаружения, значит успели заведомо. Тоже окончательно.
    3. Нет времени от НП, а `previous_poll_at > deadline` — на прошлом опросе посылка
       ещё не была отправлена, и было это уже после дедлайна. Значит промах заведомо.
    4. Иначе дедлайн попадает внутрь интервала «между прошлым опросом и этим», и мы
       не знаем, по какую он сторону. Возвращаем `None`: `fee_free` не ставится, но
       и «успели» не утверждается — случай уходит в лог на разбор человеком.

    Молчаливо выбрать любой край четвёртого случая означало бы либо дарить заказы за
    свою же задержку опроса, либо брать деньги за просрочку. Ни то ни другое не
    должно происходить незаметно.
    """
    if deadline is None:
        return None
    if scanned_at is not None:
        return scanned_at <= deadline
    if detected_at <= deadline:
        return True
    if previous_poll_at is not None and previous_poll_at > deadline:
        return False
    return None
