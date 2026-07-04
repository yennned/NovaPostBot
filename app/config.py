"""Конфигурация приложения (pydantic-settings).

Все значения берутся из окружения / `.env` (см. `.env.example`). Секреты в git не
коммитим.
"""

from __future__ import annotations

import json
from datetime import time as dt_time
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_ids(value: str | None) -> list[int]:
    """Распарсить список Telegram ID из строки `111, 222; 333`."""
    if not value:
        return []
    parts = value.replace(";", ",").split(",")
    return [int(p.strip()) for p in parts if p.strip()]


def parse_work_schedule(value: str | None) -> dict[int, tuple[str, str]]:
    """Распарсить JSON-расписание вида `{"0": ["08:00", "20:00"], ...}`.

    Ключи — `weekday()` Python: 0=понедельник, 6=воскресенье.
    Значение `null`/пусто для дня означает «выходной».
    """
    if not value:
        return dict.fromkeys(range(0, 5), ("08:00", "20:00"))

    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("WORK_SCHEDULE must be a JSON object")

    schedule: dict[int, tuple[str, str]] = {}
    for raw_day, raw_window in payload.items():
        day = int(raw_day)
        if raw_window in (None, "", []):
            continue
        if (
            not isinstance(raw_window, list | tuple)
            or len(raw_window) != 2
            or not all(isinstance(part, str) for part in raw_window)
        ):
            raise ValueError(f"WORK_SCHEDULE[{raw_day}] must be ['HH:MM', 'HH:MM']")
        start, end = raw_window
        dt_time.fromisoformat(start)
        dt_time.fromisoformat(end)
        schedule[day] = (start, end)
    return schedule


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Версия сборки (git sha от CI, «dev» локально) — для логов старта и /version.
    app_version: str = Field(default="dev", alias="APP_VERSION")

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
    inventory_source: Literal["sheets", "crm"] = Field(
        default="sheets",
        alias="INVENTORY_SOURCE",
    )

    # Нова Пошта (ключ — per-ФОП, шифруется в БД; здесь только транспорт).
    # Тарифы/мин-стоимость не храним — НП валидирует онлайн.
    np_api_url: str = Field(default="https://api.novaposhta.ua/v2.0/json/", alias="NP_API_URL")
    np_timeout_seconds: float = Field(default=15.0, alias="NP_TIMEOUT_SECONDS")
    np_max_retries: int = Field(default=3, alias="NP_MAX_RETRIES")
    # Базовый множитель экспоненциального бэкоффа ретраев (сек). 0 — без пауз
    # (используется в тестах, чтобы ретраи не спали по-настоящему).
    np_retry_backoff: float = Field(default=0.5, alias="NP_RETRY_BACKOFF")
    # TTL кэша справочников НП в Redis (города меняются редко — сутки; відділення
    # чаще — 6 часов).
    np_cities_ttl_seconds: int = Field(default=86_400, alias="NP_CITIES_TTL_SECONDS")
    np_warehouses_ttl_seconds: int = Field(default=21_600, alias="NP_WAREHOUSES_TTL_SECONDS")
    # Наш склад-отправитель (физически один на фулфилмент) — Ref города и
    # відділення НП. Подставляются как отправитель при создании ТТН.
    np_sender_city_ref: str = Field(default="", alias="NP_SENDER_CITY_REF")
    np_sender_warehouse_ref: str = Field(default="", alias="NP_SENDER_WAREHOUSE_REF")

    # Воркер / SLA
    work_schedule_raw: str = Field(default="", alias="WORK_SCHEDULE")
    tracking_poll_seconds: int = Field(default=180, alias="TRACKING_POLL_SECONDS")
    low_stock_poll_seconds: int = Field(default=900, alias="LOW_STOCK_POLL_SECONDS")
    low_stock_threshold: int = Field(default=3, alias="LOW_STOCK_THRESHOLD")
    # Период проверки авто-снятия дежурства (закрытие отделения), сек.
    duty_check_seconds: int = Field(default=300, alias="DUTY_CHECK_SECONDS")

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

    @property
    def work_schedule(self) -> dict[int, tuple[str, str]]:
        return parse_work_schedule(self.work_schedule_raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кешированный синглтон настроек.

    Без кеша каждый вызов конструировал бы новый `Settings()` (чтение `.env` с
    диска + повторный парс списков ID), а вызывается это в горячем пути —
    `is_dev`/`can_manage`/`has_permission` на каждый апдейт Telegram. В тестах
    кеш сбрасывается autouse-фикстурой (`get_settings.cache_clear()`).
    """
    return Settings()
