"""Тесты единого форматтера времени (UTC → Europe/Kyiv для отображения)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.utils.timefmt import fmt_dt, to_local


def test_fmt_dt_utc_to_kyiv_summer():
    # 25.06 — лето: Europe/Kyiv = UTC+3 → 05:30 UTC = 08:30 (тот самый SLA-баг).
    value = datetime(2026, 6, 25, 5, 30, tzinfo=UTC)
    assert fmt_dt(value) == "25.06 08:30"
    assert fmt_dt(value, "%d.%m.%Y %H:%M") == "25.06.2026 08:30"


def test_fmt_dt_naive_treated_as_utc():
    # naive трактуем как UTC (а не как локаль контейнера).
    assert fmt_dt(datetime(2026, 6, 25, 5, 30)) == "25.06 08:30"


def test_to_local_winter_offset():
    # Январь — зима: Europe/Kyiv = UTC+2.
    assert to_local(datetime(2026, 1, 15, 5, 30, tzinfo=UTC)).hour == 7
