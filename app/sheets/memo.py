"""Источник остатков с мемоизацией чтения в пределах ОДНОГО апдейта.

Открытие экрана «📦 Товари» читало лист «Склад» дважды: сначала `list_inventory`
для рендера, следом `best_effort_sync` — тот же лист ещё раз. Так же в
`create_shipment` (`_ensure_stock` + синк). Каждое чтение — три запроса к Google
API, а квота Sheets (60 read/min) считается на service-account, то есть на весь бот.

Это НЕ кэш и TTL здесь быть не может. Экземпляр создаётся мидлварью на каждый
апдейт и живёт ровно столько же, сколько `db_session`, поэтому снапшот физически не
может пережить хендлер, который его отрисовал. Семантически это то же самое, что
прочитать лист один раз и передать список по стеку, — инвариант «остаток всегда
свежий» не нарушен.
"""

from __future__ import annotations

from app.sheets.source import StockDelta, StockRow, StockSource


class PerUpdateStockSource:
    """Обёртка над `StockSource`, кэширующая `read_stock` на время одного апдейта."""

    def __init__(self, source: StockSource) -> None:
        self._source = source
        self._rows: dict[str, list[StockRow]] = {}
        self.reads = 0  # фактические обращения к источнику — для логов и тестов

    def read_stock(self, client_key: str) -> list[StockRow]:
        cached = self._rows.get(client_key)
        if cached is not None:
            return cached
        self.reads += 1
        rows = self._source.read_stock(client_key)
        self._rows[client_key] = rows
        return rows

    def apply_deltas(self, client_key: str, deltas: list[StockDelta]) -> None:
        """Записать дельты и сбросить мемо: после записи прежний снапшот неверен.

        Инвалидируем ДО записи: упади она на полпути, часть строк всё равно могла
        измениться, и отдавать старый снапшот было бы хуже, чем перечитать.
        """
        self._rows.pop(client_key, None)
        self._source.apply_deltas(client_key, deltas)
