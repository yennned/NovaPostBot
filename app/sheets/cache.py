"""Кэш листа «Склад» между апдейтами — с коротким TTL и явной инвалидацией.

Зачем. `PerUpdateStockSource` мемоизирует чтение внутри ОДНОГО апдейта, а
сценарий створення ТТН — это восемь и больше апдейтов подряд по одному и тому же
листу: пикер, пагинация, категории, карточка товару, степпер, кошик, сабмит.
E2E-прогон на боевом стенде замерил **8 чтений на одну ТТН** по ~0.74 с. Квота
Google — 60 чтений в минуту на service-account, то есть на весь бот; получается
потолок ≈7 ТТН/мин **на всех клиентов сразу**, а дальше HTTP 429 и
«⚠️ Склад тимчасово недоступний» вместо созданной накладной. Упирается в него
даже один человек, создающий накладные подряд.

Почему это безопасно. `available = кількість(Sheets) − reserved(Postgres)`.
Устаревать может только первое слагаемое; резервы всегда читаются из БД свежими.
А главное — **гейт от oversell кэш не использует**: `shipment._resolve_items`
перед сверкой корзины принудительно инвалидирует ключ (`create_shipment`), то
есть решение «хватает ли остатка» принимается всегда по свежему листу. TTL
влияет только на то, что нарисовано на экране.

Инвалидация: по TTL, при записи дельт (`apply_deltas`) и явным вызовом
`invalidate()`. Кэш процессный — бот и воркер видят каждый свой; расхождение
живёт не дольше TTL и гейт от него не зависит.
"""

from __future__ import annotations

import threading
import time

from app.sheets.source import StockDelta, StockRow, StockSource


class TtlStockSource:
    """Обёртка над `StockSource` с общим на процесс кэшем чтений."""

    def __init__(self, source: StockSource, *, ttl_seconds: float) -> None:
        self._source = source
        self._ttl = ttl_seconds
        # Читает `read_stock` в потоке sheets-executor'а, а инвалидирует — луп;
        # блокировка нужна, хотя executor и однопоточный.
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, list[StockRow]]] = {}
        self.reads = 0  # фактические обращения к Google — для логов и тестов
        self.hits = 0

    def read_stock(self, client_key: str) -> list[StockRow]:
        if self._ttl > 0:
            with self._lock:
                entry = self._entries.get(client_key)
                if entry is not None and (time.monotonic() - entry[0]) < self._ttl:
                    self.hits += 1
                    return entry[1]

        self.reads += 1
        rows = self._source.read_stock(client_key)

        if self._ttl > 0:
            with self._lock:
                self._entries[client_key] = (time.monotonic(), rows)
        return rows

    def apply_deltas(self, client_key: str, deltas: list[StockDelta]) -> None:
        """Записать дельты, сбросив кэш ДО записи.

        Именно до: упади запись на полпути, часть строк всё равно могла
        измениться, и отдавать прежний снапшот было бы хуже, чем перечитать.
        """
        self.invalidate(client_key)
        self._source.apply_deltas(client_key, deltas)

    def invalidate(self, client_key: str | None = None) -> None:
        """Забыть кэш по ключу (или весь), чтобы следующее чтение пошло в Google."""
        with self._lock:
            if client_key is None:
                self._entries.clear()
            else:
                self._entries.pop(client_key, None)
