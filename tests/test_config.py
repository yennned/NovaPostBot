"""Юнит-тесты конфигурации (чистая логика, без живых сервисов)."""

from __future__ import annotations

from app.config import Settings, parse_ids


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
    settings = Settings(_env_file=None)
    assert settings.timezone == "Europe/Kyiv"
    assert settings.owner_telegram_ids == []
    assert settings.redis_url.startswith("redis://")
