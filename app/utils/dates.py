"""Парсинг дат из пользовательского ввода (uk-форматы)."""

from __future__ import annotations

from datetime import date

# Поддерживаемые форматы ручного ввода даты — для подсказок пользователю.
USER_DATE_HINT = "ДД.ММ.РРРР або РРРР-ММ-ДД"


def parse_user_date(raw: str | None) -> date | None:
    """Разобрать дату из строки (`ДД.ММ.РРРР` или `РРРР-ММ-ДД`), иначе `None`."""
    value = (raw or "").strip()
    if not value:
        return None
    try:
        if "-" in value:
            return date.fromisoformat(value)
        dd, mm, yyyy = value.split(".")
        return date.fromisoformat(f"{yyyy}-{mm}-{dd}")
    except ValueError:
        return None
