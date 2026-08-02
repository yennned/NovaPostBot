"""Проверки стенда, без которых нагрузочный прогон измеряет не то или ломает прод.

Все проверки **fail-closed и до создания движка**: молчаливое расхождение
конфигурации — тот же класс дефекта, который уже стоил проекту двух выходных дней
трекинга (`WORK_SCHEDULE` не был задан, и прод жил по дефолту Пн–Пт).

Две опасности, разные по цене:

1. **Стереть боевую БД.** Сид делает `drop_all`. Отсюда требование к имени базы.
2. **Уйти в живой Google.** Гораздо тише и потому хуже. Фейк подменяет
   `StockSource`, но `_sync_client_sheets_sync` (`app/services/client_sheet_sync.py`)
   берёт **настоящий** `shared_sheets_client()`, а `best_effort_sync` проглатывает
   все ошибки. То есть прогон на 5000 ТТН при боевом `.env` устроил бы боевым
   таблицам десятки тысяч обращений, и в выводе это выглядело бы как успех.
"""

from __future__ import annotations

import os

#: Переменные, при непустом значении которых прогон может уйти в живой внешний
#: сервис. `GOOGLE_SA_JSON` — ключ доступа к книгам, `SHEETS_*_BOOK_ID` — сами
#: книги, `BOT_TOKEN` — боевой бот (харнесс в stub-режиме токен не использует).
_LIVE_ENV_KEYS = (
    "GOOGLE_SA_JSON",
    "SHEETS_STOCK_BOOK_ID",
    "SHEETS_INTAKE_BOOK_ID",
    "BOT_TOKEN",
)


def require_load_database(url: str) -> None:
    """База прогона обязана оканчиваться на `_load`: сид её стирает."""
    name = url.rsplit("/", 1)[-1].split("?")[0]
    if not name.endswith("_load"):
        raise SystemExit(
            f"отказ: база «{name}» не оканчивается на _load.\n"
            "Сид стирает данные — гонять его можно только по выделенной базе.\n"
            "Пример: DATABASE_URL=postgresql+asyncpg://…/novapostbot_load"
        )


def require_offline_stand(*, allow_live_google: bool = False) -> None:
    """Отказать, если окружение может увести прогон в живой Google или Telegram."""
    if allow_live_google:
        return
    live = [key for key in _LIVE_ENV_KEYS if os.environ.get(key, "").strip()]
    if live:
        raise SystemExit(
            "отказ: заданы переменные живых сервисов: " + ", ".join(live) + ".\n"
            "Фейк подменяет только StockSource; синк книги-вьюшки берёт настоящий\n"
            "SheetsClient и проглатывает ошибки — прогон молча ушёл бы в боевые\n"
            "таблицы. Очистите их или передайте --allow-live-google осознанно."
        )


def report_effective_settings(settings) -> dict[str, object]:
    """Что фактически увидит система под нагрузкой. Печатать обязательно.

    Сид кладёт остаток в `stock_balances`, а дефолт `INVENTORY_SOURCE` —
    `sheets`. При расхождении прогон разваливается **до** нагрузки: пустой склад →
    `InsufficientStock` на каждом сабмите, и это выглядит как отказ бизнес-логики,
    а не как мисконфиг стенда. Плюс на `sheets` гейт от oversell не применяется
    вовсе (`_hold_stock` возвращает `None`) — то есть проверялось бы не то.
    """
    return {
        "INVENTORY_SOURCE": settings.inventory_source,
        "DB_POOL": f"{settings.db_pool_size}+{settings.db_max_overflow}",
        "DB_POOL_TIMEOUT": settings.db_pool_timeout,
        "TRACKING_BATCH_LIMIT": settings.tracking_batch_limit,
        "TRACKING_STALE_DAYS": settings.tracking_stale_days,
        "STOCK_HOLD_TTL_SECONDS": settings.stock_hold_ttl_seconds,
        "STOCK_CACHE_TTL_SECONDS": settings.stock_cache_ttl_seconds,
        "SHEETS_RETRY_ATTEMPTS": settings.sheets_retry_attempts,
        "STOCK_INGEST_ENABLED": settings.stock_ingest_enabled,
        "STOCK_MIRROR_ENABLED": settings.stock_mirror_enabled,
        "STOCK_RECONCILE_ENABLED": settings.stock_reconcile_enabled,
    }


def require_pg_inventory(settings) -> None:
    """Гейт от oversell и PG-путь записи проверяются только при `INVENTORY_SOURCE=pg`."""
    if settings.inventory_source != "pg":
        raise SystemExit(
            f"отказ: INVENTORY_SOURCE={settings.inventory_source}, а сид кладёт остаток в\n"
            "stock_balances. На пути sheets прогон увидит пустой склад и упадёт в\n"
            "InsufficientStock до всякой нагрузки, а гейт от oversell не включится\n"
            "вовсе. Выставьте INVENTORY_SOURCE=pg или гоняйте с --backend sheets,\n"
            "предварительно засеяв лист."
        )
