"""Юнит-тесты конфигурации (чистая логика, без живых сервисов)."""

from __future__ import annotations

import pytest
from app.config import Settings, get_settings, parse_ids, parse_work_schedule
from pydantic import ValidationError


def test_parse_ids_variants():
    assert parse_ids("111, 222; 333") == [111, 222, 333]
    assert parse_ids("") == []
    assert parse_ids(None) == []
    assert parse_ids("42") == [42]


def test_settings_ids_from_env(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "111, 222")
    monkeypatch.setenv("DEV_TELEGRAM_IDS", "333")
    settings = Settings(_env_file=None)
    assert settings.owner_telegram_ids == [111, 222]
    assert settings.dev_telegram_ids == [333]


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("OWNER_TELEGRAM_IDS", raising=False)
    monkeypatch.delenv("DEV_TELEGRAM_IDS", raising=False)
    monkeypatch.delenv("TIMEZONE", raising=False)
    monkeypatch.delenv("INVENTORY_SOURCE", raising=False)
    settings = Settings(_env_file=None)
    assert settings.timezone == "Europe/Kyiv"
    assert settings.owner_telegram_ids == []
    assert settings.redis_url.startswith("redis://")
    assert settings.work_schedule[0] == ("08:00", "20:00")
    assert settings.inventory_source == "sheets"


def test_default_work_schedule_covers_all_seven_days():
    """Склад и НП работают без выходных; дефолт обязан это отражать.

    Раньше дефолтом был `range(0, 5)`, а `WORK_SCHEDULE` в прод-`.env` не задан —
    то есть прод два дня из семи не трекал ТТН и выдавал субботним отправлениям
    дедлайн SLA в понедельник.
    """
    schedule = parse_work_schedule(None)
    assert sorted(schedule) == [0, 1, 2, 3, 4, 5, 6]
    assert set(schedule.values()) == {("08:00", "20:00")}


def test_parse_work_schedule_from_json():
    schedule = parse_work_schedule('{"0": ["09:00", "18:00"], "5": null}')
    assert schedule[0] == ("09:00", "18:00")
    assert 5 not in schedule


def test_parse_work_schedule_rejects_weekday_out_of_range():
    # Опечатка «7» вместо «0» раньше молча оседала в словаре и читалась как
    # «понедельник выходной»: ни ошибки, ни расписания на понедельник.
    with pytest.raises(ValueError, match=r"expected 0\.\.6"):
        parse_work_schedule('{"7": ["08:00", "20:00"]}')


def test_get_settings_is_cached():
    # Горячий путь (permissions/middleware) не должен пересоздавать Settings.
    assert get_settings() is get_settings()


def test_inventory_source_can_switch_to_crm(monkeypatch):
    monkeypatch.setenv("INVENTORY_SOURCE", "crm")
    settings = Settings(_env_file=None)
    assert settings.inventory_source == "crm"


def test_inventory_source_accepts_pg(monkeypatch):
    """`pg` — путь отката с Postgres обратно на лист одной переменной окружения.

    Если бы `Literal` его не принимал, переключение потребовало бы релиза, а откат
    под инцидентом — второго релиза.
    """
    monkeypatch.setenv("INVENTORY_SOURCE", "pg")
    assert Settings(_env_file=None).inventory_source == "pg"


def test_inventory_source_rejects_typos(monkeypatch):
    """Опечатка обязана падать на старте, а не тихо оставлять прежний источник."""
    monkeypatch.setenv("INVENTORY_SOURCE", "postgres")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_db_pool_is_configured_not_defaulted(monkeypatch):
    """Пул задан явно — дефолты SQLAlchemy нам не подходят.

    Коннект удерживается через внешнее I/O (НП, Sheets внутри апдейта), поэтому
    5+10 кончаются на десятке одновременных отправок, а `pool_timeout=30` означает
    полминуты тишины перед той же ошибкой.
    """
    for key in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_TIMEOUT", "DB_POOL_RECYCLE"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None)
    assert (settings.db_pool_size, settings.db_max_overflow) == (20, 30)
    assert settings.db_pool_timeout == 10, "долгое ожидание в пуле выглядит как зависший бот"
    # Коннект, закрытый пулером Neon со своей стороны, дешевле ронять по возрасту.
    assert settings.db_pool_recycle == 300
