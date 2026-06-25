"""Клиент Google Sheets для чтения и корректировок складских листов.

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
from app.sheets.source import StockSheetNotFound

# Канонические колонки листа «Склад» (см. scripts/provision_sheets.STOCK_HEADERS[:5]).
# Передаём как expected_headers в get_all_records: иначе панель-итог справа и любой
# контент за таблицей добавляют пустые ячейки в строку-шапку, и gspread падает на
# дублирующихся пустых заголовках.
_STOCK_EXPECTED_HEADERS = ["Артикул", "Назва", "Категорія", "Кількість", "Ціна"]


class SheetsClient:
    """Ленивая авторизация service-account и чтение листов «Склад».

    Авторизация ленивая; по умолчанию используем права read/write, потому что
    Phase 5 начинает списывать и возвращать остатки ботом.
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
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        if raw.startswith("{"):
            creds = Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
        else:
            creds = Credentials.from_service_account_file(Path(raw), scopes=scopes)
        self._gc = gspread.authorize(creds)
        return self._gc

    def get_stock_worksheet(self, client_key: str) -> Any:
        """Вернуть лист остатков клиента из книги «Склад» (read-only).

        Нет листа с таким именем → доменный `StockSheetNotFound` (а не сырой
        `gspread.WorksheetNotFound`), чтобы сервис-слой не зависел от gspread.
        """
        if not self._settings.sheets_stock_book_id:
            raise RuntimeError("SHEETS_STOCK_BOOK_ID не настроен")
        from gspread.exceptions import WorksheetNotFound

        book = self._authorize().open_by_key(self._settings.sheets_stock_book_id)
        try:
            return book.worksheet(client_key)
        except WorksheetNotFound as exc:
            raise StockSheetNotFound(client_key) from exc

    def read_rows(self, client_key: str) -> list[dict[str, Any]]:
        """Прочитать строки остатков клиента (артикул/назва/кількість/ціна)."""
        worksheet = self.get_stock_worksheet(client_key)
        return list(
            worksheet.get_all_records(default_blank="", expected_headers=_STOCK_EXPECTED_HEADERS)
        )
