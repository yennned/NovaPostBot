"""Единый перевод хранимого времени (tz-aware UTC) в зону отображения.

Все DateTime-колонки объявлены `timezone=True` (Postgres хранит как UTC). Для
пользователя показываем в `settings.timezone` (Europe/Kyiv). Голый `strftime`
без конверсии печатает UTC-стенки часов — отсюда баг «дедлайн 05:30 замість
08:30». Этот модуль — единая точка форматирования, чтобы форматтеры по разным
text-модулям снова не разъезжались (UTC где-то, Kyiv где-то).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.config import get_settings


def to_local(value: datetime) -> datetime:
    """tz-aware UTC (или naive, трактуем как UTC) → зона отображения (Europe/Kyiv)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo(get_settings().timezone))


def fmt_dt(value: datetime, fmt: str = "%d.%m %H:%M") -> str:
    """Время → строка в зоне отображения. Дефолт — «дд.мм гг:хх»."""
    return f"{to_local(value):{fmt}}"
