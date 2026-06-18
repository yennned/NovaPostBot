"""Конфигурация приложения (pydantic-settings).

Все значения берутся из окружения / `.env` (см. `.env.example`). Секреты в git не
коммитим.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_ids(value: str | None) -> list[int]:
    """Распарсить список Telegram ID из строки `111, 222; 333`."""
    if not value:
        return []
    parts = value.replace(";", ",").split(",")
    return [int(p.strip()) for p in parts if p.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Telegram
    bot_token: str = Field(default="", alias="BOT_TOKEN")

    # PostgreSQL (Neon): pooled — для приложения, direct — для Alembic
    database_url: str = Field(default="", alias="DATABASE_URL")
    database_url_direct: str = Field(default="", alias="DATABASE_URL_DIRECT")

    # Redis
    redis_url: str = Field(default="redis://redis:6379/0", alias="REDIS_URL")

    # Шифрование ключей НП
    fernet_key: str = Field(default="", alias="FERNET_KEY")

    # Google Sheets (только склад)
    google_sa_json: str = Field(default="", alias="GOOGLE_SA_JSON")
    sheets_stock_book_id: str = Field(default="", alias="SHEETS_STOCK_BOOK_ID")
    sheets_intake_book_id: str = Field(default="", alias="SHEETS_INTAKE_BOOK_ID")

    # Нова Пошта (ключ — per-ФОП, шифруется в БД; здесь только транспорт).
    # Тарифы/мин-стоимость не храним — НП валидирует онлайн.
    np_api_url: str = Field(default="https://api.novaposhta.ua/v2.0/json/", alias="NP_API_URL")
    np_timeout_seconds: float = Field(default=15.0, alias="NP_TIMEOUT_SECONDS")
    np_max_retries: int = Field(default=3, alias="NP_MAX_RETRIES")
    # Базовый множитель экспоненциального бэкоффа ретраев (сек). 0 — без пауз
    # (используется в тестах, чтобы ретраи не спали по-настоящему).
    np_retry_backoff: float = Field(default=0.5, alias="NP_RETRY_BACKOFF")

    # Роли (сырые строки из env; распарсенные — в свойствах ниже)
    owner_telegram_ids_raw: str = Field(default="", alias="OWNER_TELEGRAM_IDS")
    dev_telegram_ids_raw: str = Field(default="", alias="DEV_TELEGRAM_IDS")

    # Прочее
    timezone: str = Field(default="Europe/Kyiv", alias="TIMEZONE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def owner_telegram_ids(self) -> list[int]:
        return parse_ids(self.owner_telegram_ids_raw)

    @property
    def dev_telegram_ids(self) -> list[int]:
        return parse_ids(self.dev_telegram_ids_raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кешированный синглтон настроек.

    Без кеша каждый вызов конструировал бы новый `Settings()` (чтение `.env` с
    диска + повторный парс списков ID), а вызывается это в горячем пути —
    `is_dev`/`can_manage`/`has_permission` на каждый апдейт Telegram. В тестах
    кеш сбрасывается autouse-фикстурой (`get_settings.cache_clear()`).
    """
    return Settings()
