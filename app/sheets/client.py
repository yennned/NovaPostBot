"""Скелет read-only клиента Google Sheets (только учёт склада).

Назначение в Фазе 1 — зафиксировать границу абстракции `app/sheets/`. Реальная
реализация (gspread + service-account, лист на клиента, кэш) появится в Фазе 3
([docs/04-warehouse-sheets.md](../../docs/04-warehouse-sheets.md)).

Источник правды по остаткам — книга «Склад» (Sheets), резерв — в Postgres
(`stock_movements`), `available = quantity(Sheets) − reserved(Postgres)`.
"""

from __future__ import annotations

from typing import Any

from app.config import Settings, get_settings


class SheetsClient:
    """Ленивая авторизация service-account и чтение листов «Склад».

    Пока каркас: методы объявлены, но не реализованы (Фаза 3).
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._gc: Any | None = None  # gspread.Client, создаётся лениво

    def _authorize(self) -> Any:
        """Авторизовать service-account (gspread) и закэшировать клиент."""
        raise NotImplementedError("Sheets-авторизация будет реализована в Фазе 3")

    def get_stock_worksheet(self, client_key: str) -> Any:
        """Вернуть лист остатков клиента из книги «Склад» (read-only)."""
        raise NotImplementedError("Чтение листа «Склад» будет реализовано в Фазе 3")

    def read_rows(self, client_key: str) -> list[dict[str, Any]]:
        """Прочитать строки остатков клиента (артикул/назва/кількість/ціна)."""
        raise NotImplementedError("Чтение остатков будет реализовано в Фазе 3")
