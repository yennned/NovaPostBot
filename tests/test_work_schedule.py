"""Тесты оконной логики рабочего расписания (`app/utils/work_schedule.py`)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.utils.work_schedule import (
    current_window_end,
    is_open,
    is_open_or_recently_closed,
    next_window_start,
    window_for_day,
)

TZ = ZoneInfo("Europe/Kyiv")
# 2026-06-22 — понедельник (weekday 0), 2026-06-28 — воскресенье (weekday 6).
SCHEDULE = {0: ("08:00", "20:00"), 1: ("08:00", "20:00")}


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=TZ)


def test_is_open_within_window():
    assert is_open(_at(22, 10), SCHEDULE) is True


def test_is_open_before_open_and_after_close():
    assert is_open(_at(22, 7, 59), SCHEDULE) is False
    assert is_open(_at(22, 20), SCHEDULE) is False  # граница закрытия — уже закрыто
    assert is_open(_at(22, 23), SCHEDULE) is False


def test_is_open_on_day_off():
    assert is_open(_at(28, 12), SCHEDULE) is False  # воскресенье вне расписания


def test_current_window_end_when_open():
    assert current_window_end(_at(22, 10), SCHEDULE) == _at(22, 20)


def test_current_window_end_when_closed_or_day_off():
    assert current_window_end(_at(22, 21), SCHEDULE) is None
    assert current_window_end(_at(28, 12), SCHEDULE) is None


def test_window_for_day_returns_none_on_day_off():
    assert window_for_day(_at(28, 12), SCHEDULE) is None


def test_window_for_day_rejects_inverted_window():
    with pytest.raises(ValueError):
        window_for_day(_at(22, 10), {0: ("20:00", "08:00")})


def test_next_window_start_skips_to_next_working_day():
    # Воскресенье днём → ближайшее открытие в понедельник 08:00.
    assert next_window_start(_at(28, 12), SCHEDULE) == _at(29, 8)


def test_next_window_start_returns_now_when_open():
    assert next_window_start(_at(22, 10), SCHEDULE) == _at(22, 10)


GRACE = timedelta(minutes=10)


def test_recently_closed_true_while_open():
    assert is_open_or_recently_closed(_at(22, 10), SCHEDULE, GRACE) is True


def test_recently_closed_true_within_grace_after_close():
    assert is_open_or_recently_closed(_at(22, 20), SCHEDULE, GRACE) is True  # ровно закрытие
    assert is_open_or_recently_closed(_at(22, 20, 9), SCHEDULE, GRACE) is True


def test_recently_closed_false_past_grace():
    assert is_open_or_recently_closed(_at(22, 20, 10), SCHEDULE, GRACE) is False
    assert is_open_or_recently_closed(_at(22, 23), SCHEDULE, GRACE) is False


def test_recently_closed_false_before_open_and_on_day_off():
    assert is_open_or_recently_closed(_at(22, 7), SCHEDULE, GRACE) is False
    assert is_open_or_recently_closed(_at(28, 12), SCHEDULE, GRACE) is False
