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

    Дефолт — **все семь дней 08:00–20:00**: склад и Нова Пошта работают без
    выходных. Раньше здесь стоял `range(0, 5)` (Пн–Пт), и поскольку `WORK_SCHEDULE`
    в прод-`.env` не задан, прод жил по нему — с двумя последствиями, которые
    ничем не проявлялись наружу. Первое: `_should_run_daytime` гасил трекинг и
    low-stock два дня из семи. Второе: `add_working_minutes` перепрыгивал субботу и
    воскресенье, поэтому ТТН, созданная в субботу, получала дедлайн SLA в
    понедельник — то есть на ~2/7 объёма SLA не значил ничего, а комиссия бралась
    полная.

    Безопасная деградация в `_should_run_daytime` («пустое расписание = поллим
    всегда») от этого не спасала: расписание было непустым, просто неверным.
    """
    if not value:
        return dict.fromkeys(range(0, 7), ("08:00", "20:00"))

    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("WORK_SCHEDULE must be a JSON object")

    schedule: dict[int, tuple[str, str]] = {}
    for raw_day, raw_window in payload.items():
        day = int(raw_day)
        # Ключ вне 0..6 не соответствует ни одному `weekday()`, поэтому раньше он
        # молча оседал в словаре и не влиял ни на что: опечатка `"7"` вместо `"0"`
        # читалась как «понедельник выходной» без единого сообщения.
        if not 0 <= day <= 6:
            raise ValueError(f"WORK_SCHEDULE has weekday {day}, expected 0..6 (0=Mon, 6=Sun)")
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

    # Среда исполнения — чтобы в логах/`/version` было видно, куда подключён процесс.
    # На поведение кода не влияет (разделение идёт через .env: токен/URL), только на
    # трассировку. `staging` убран вместе со staging-стендом (2026-07-31): у проекта
    # один бот, а незанятое значение в Literal читается как «стенд есть» и приглашает
    # поднять второй поллер на боевом токене.
    environment: Literal["local", "production"] = Field(
        default="local",
        alias="ENVIRONMENT",
    )

    # Telegram. Один бот на проект: токен живёт только в .env на боевом сервере.
    # Локально пусто — `app/main.py` тогда не поднимает поллинг, и машина
    # разработчика физически не может перехватить трафик реальных клиентов.
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
    # Ретраи ЧТЕНИЯ Sheets на временную недоступность (квота 429, 5xx). Квота
    # Google — 60 read/min на service-account, то есть на весь бот, поэтому упереться
    # в неё реально. Записи (`apply_deltas`) не ретраим: они могли примениться
    # частично, и повтор удвоил бы дельту остатка.
    sheets_retry_attempts: int = Field(default=3, alias="SHEETS_RETRY_ATTEMPTS")
    sheets_retry_backoff: float = Field(default=0.5, alias="SHEETS_RETRY_BACKOFF")

    # Откуда берётся КОЛИЧЕСТВО на складе. `sheets` — книга «Склад» (как было);
    # `pg` — таблица `stock_balances`. Переключатель существует, чтобы откат был
    # сменой переменной окружения и рестартом, без пересборки и без миграции вниз.
    # `crm` — контракт-заглушка Фазы 7, честно падает «ще не реалізовано».
    inventory_source: Literal["sheets", "crm", "pg"] = Field(
        default="sheets",
        alias="INVENTORY_SOURCE",
    )

    # Сколько секунд переиспользовать прочитанный лист «Склад» между апдейтами.
    # Квота Google — 60 чтений/мин на service-account, то есть на ВЕСЬ бот, а один
    # сценарий створення ТТН делал ~8 чтений: потолок ≈7 ТТН/мин на всех клиентов,
    # дальше 429 и «Склад тимчасово недоступний» (замерено E2E-прогоном).
    # Гейт от oversell (`shipment._resolve_items`) кэш не читает — он всегда
    # перечитывает лист, поэтому TTL влияет только на отрисовку экранов.
    # 0 — выключить кэш (поведение до правки).
    stock_cache_ttl_seconds: int = Field(default=45, alias="STOCK_CACHE_TTL_SECONDS")

    # Нова Пошта (ключ — per-ФОП, шифруется в БД; здесь только транспорт).
    # Тарифы/мин-стоимость не храним — НП валидирует онлайн.
    np_api_url: str = Field(default="https://api.novaposhta.ua/v2.0/json/", alias="NP_API_URL")
    np_timeout_seconds: float = Field(default=15.0, alias="NP_TIMEOUT_SECONDS")
    np_max_retries: int = Field(default=3, alias="NP_MAX_RETRIES")
    # Интерактивный поиск справочников (город/відділення при создании ТТН): жёстче
    # таймаут и меньше ретраев, чем у фоновых вызовов — иначе флаки-НП вешает
    # пользователя до ~45с (15с × 3). Здесь важнее быстрый отклик: не нашли —
    # пользователь просто повторит ввод.
    np_lookup_timeout_seconds: float = Field(default=6.0, alias="NP_LOOKUP_TIMEOUT_SECONDS")
    np_lookup_max_retries: int = Field(default=2, alias="NP_LOOKUP_MAX_RETRIES")
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
    # Сколько ТТН забирает один проход трекинга. Раньше здесь стояла зашитая
    # константа 200, и её не хватало: выборка сортировалась по `status_changed_at`,
    # который двигается только при СМЕНЕ статуса, поэтому документы с неменяющимся
    # статусом занимали слоты навсегда. Сортировка теперь по `tracking_updated_at`,
    # но лимит всё равно должен быть настраиваемым — на случай всплеска.
    tracking_batch_limit: int = Field(default=500, alias="TRACKING_BATCH_LIMIT")
    # ТТН, не уехавшая за столько дней, выводится из трекинга: клиент завёл накладную
    # и передумал. Без отсечки такие копятся в `confirmed` монотонно и снова выбирают
    # лимит — просто медленнее, чем раньше.
    tracking_stale_days: int = Field(default=14, alias="TRACKING_STALE_DAYS")

    # Поздний опрос возвратов: после `dispatched` посылка уходит из горячего трекинга,
    # но НП может развернуть её назад (неполучение — обычно 7 дней хранения, возврат
    # приезжает к нам к 10–12-му дню). Возврат физически приходит на наш склад и
    # должен вернуться в остаток, поэтому редкий опрос дешевле ручной дисциплины.
    returns_poll_seconds: int = Field(default=21_600, alias="RETURNS_POLL_SECONDS")
    returns_watch_min_days: int = Field(default=3, alias="RETURNS_WATCH_MIN_DAYS")
    returns_watch_max_days: int = Field(default=21, alias="RETURNS_WATCH_MAX_DAYS")
    returns_recheck_hours: int = Field(default=24, alias="RETURNS_RECHECK_HOURS")
    # Ингест приёмки: лист «Історія» книги «Склад» → `stock_balances`.
    # Выключен по умолчанию и НЕ привязан к `INVENTORY_SOURCE` намеренно: порядок
    # выкатки — сначала backfill балансов, потом ингест на наблюдении несколько
    # дней, и только потом переключение чтения на PG. Включённый ингест до
    # backfill'а построил бы баланс из одних приходов, без стартового остатка.
    stock_ingest_enabled: bool = Field(default=False, alias="STOCK_INGEST_ENABLED")
    stock_ingest_seconds: int = Field(default=60, alias="STOCK_INGEST_SECONDS")
    # Сколько строк журнала забирает один проход. Читаются одним запросом окном
    # `A{водораздел}:F{водораздел+N}`, поэтому цена прохода — ровно одно обращение
    # к Google независимо от лимита.
    stock_ingest_batch_limit: int = Field(default=500, alias="STOCK_INGEST_BATCH_LIMIT")

    # Зеркало Postgres → лист «Склад»: пишет только «Кількість» и «Резерв», забирает
    # описательные поля обратно и принимает ручные правки количества.
    # 300 секунд, а не 60: проход стоит чтение + запись на аккаунт, и при 20
    # аккаунтах минутный интервал съел бы две трети квоты Google в обе стороны.
    stock_mirror_enabled: bool = Field(default=False, alias="STOCK_MIRROR_ENABLED")
    stock_mirror_seconds: int = Field(default=300, alias="STOCK_MIRROR_SECONDS")
    # Предохранитель на ручную правку ячейки «Кількість». Опечатка в одну цифру
    # иначе становится реальным изменением остатка, а гейт от oversell смотрит
    # именно на него. 0 — снять ограничение (не рекомендуется).
    stock_manual_delta_limit: int = Field(default=100, alias="STOCK_MANUAL_DELTA_LIMIT")

    # Бронь остатка на время похода в НП. TTL короткий: он существует ровно на
    # длину внешнего вызова (p50 2,5 с, до 45 с при флаки-НП) плюс запас. Длиннее —
    # значит дольше держать заниженный `available` после падения процесса.
    stock_hold_ttl_seconds: int = Field(default=300, alias="STOCK_HOLD_TTL_SECONDS")
    stock_hold_sweep_seconds: int = Field(default=120, alias="STOCK_HOLD_SWEEP_SECONDS")

    # Сверка остатка. Отдельно от зеркала и от ингеста: по порядку выкатки она
    # работает в режиме наблюдения несколько дней ПЕРЕД тем, как включить зеркало
    # и переключить чтение на PG. Час, а не минуты: сверка стоит чтение на аккаунт,
    # а расхождение эскалируется только пережив два цикла подряд.
    stock_reconcile_enabled: bool = Field(default=False, alias="STOCK_RECONCILE_ENABLED")
    stock_reconcile_seconds: int = Field(default=3600, alias="STOCK_RECONCILE_SECONDS")

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
