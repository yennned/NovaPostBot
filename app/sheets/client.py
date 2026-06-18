"""Скелет read-only клиента Google Sheets (только учёт склада).

Назначение в Фазе 1 — зафиксировать границу абстракции `app/sheets/`. Реальная
реализация (gspread + service-account, лист на клиента, кэш) появится в Фазе 3
([docs/04-warehouse-sheets.md](../../docs/04-warehouse-sheets.md)).

Источник правды по остаткам — книга «Склад» (Sheets), резерв — в Postgres
(`stock_movements`), `available = quantity(Sheets) − reserved(Postgres)`.
"""

from __future__ import annotations

import json
from pathlib import Path
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
        if self._gc is not None:
            return self._gc

        import gspread
        from google.oauth2.service_account import Credentials

        raw = self._settings.google_sa_json.strip()
        if not raw:
            raise RuntimeError("GOOGLE_SA_JSON не настроен")

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        if raw.startswith("{"):
            creds = Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
        else:
            creds = Credentials.from_service_account_file(Path(raw), scopes=scopes)
        self._gc = gspread.authorize(creds)
        return self._gc

    def get_stock_worksheet(self, client_key: str) -> Any:
        """Вернуть лист остатков клиента из книги «Склад» (read-only)."""
        if not self._settings.sheets_stock_book_id:
            raise RuntimeError("SHEETS_STOCK_BOOK_ID не настроен")
        book = self._authorize().open_by_key(self._settings.sheets_stock_book_id)
        return book.worksheet(client_key)

    def read_rows(self, client_key: str) -> list[dict[str, Any]]:
        """Прочитать строки остатков клиента (артикул/назва/кількість/ціна)."""
        worksheet = self.get_stock_worksheet(client_key)
        return list(worksheet.get_all_records(default_blank=""))
