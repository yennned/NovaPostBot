"""Тесты парсера пользовательских дат (`app/utils/dates.py`)."""

from __future__ import annotations

from datetime import date

from app.utils.dates import parse_user_date


def test_parse_dotted_format():
    assert parse_user_date("20.06.2026") == date(2026, 6, 20)


def test_parse_iso_format():
    assert parse_user_date("2026-06-20") == date(2026, 6, 20)


def test_parse_strips_whitespace():
    assert parse_user_date("  20.06.2026  ") == date(2026, 6, 20)


def test_parse_rejects_garbage():
    for raw in ["", None, "вчора", "32.13.2026", "20/06/2026", "2026.06.20"]:
        assert parse_user_date(raw) is None
