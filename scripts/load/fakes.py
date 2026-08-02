"""Подделки внешнего мира для нагрузочного прогона: Google Sheets и Нова Пошта.

Смысл один: **сделать потолок воспроизводимым, а не постулированным**. План
опирается на два числа — 60 операций в минуту у Google и p50 2,5 с у
`InternetDocument.save`. Пока они живут в тексте, проверить их нечем: живой прогон
упирается в настоящую квоту один раз и не повторяется.

Задержки взяты из боевого E2E-прогона (`PROGRESS.md`, раздел за 2026-07-2x), а не
выдуманы: `InternetDocument.save` p50 2,5 с, чтение листа склада 0,74 с.

**Четыре вещи, из-за которых первая версия этих фейков дала бы ложно-зелёный
результат** — все исправлены здесь, и каждая закрыта тестом в
`tests/test_load_harness.py`:

1. **Форма 429.** Раньше бросался `RuntimeError`, а `app/sheets/runtime.py`
   ретраит только `StockSourceUnavailable`. В проде один пользовательский read при
   429 превращается в три HTTP — каждый жрёт квоту и снова встаёт в очередь
   single-worker executor'а, то есть насыщение **разгоняет само себя**. С
   `RuntimeError` отказ мгновенный и токен не тратится — обратная связь
   становится отрицательной, и харнесс измеряет систему, которая под перегрузкой
   сама себя чинит.
2. **Что считать.** Раньше — по записи на каждую `StockDelta`. Настоящий
   `apply_deltas` делает **одну** запись независимо от размера корзины, зато три
   чтения. Упирается **чтение**, и расход **не зависит от корзины**. Считаем на
   уровне обращений к книге, а не доменных операций.
3. **Общая квота.** 60/мин Google считает на service-account, то есть на все
   процессы разом. Ведро в памяти процесса при N процессах даёт потолок в N раз
   выше. Здесь — скользящее окно поверх файла под `flock`, на `time.time()`
   (`time.monotonic()` между процессами несравним).
4. **Профиль ответов НП.** Раньше на любой запрос отдавался `dispatched`, поэтому
   каждая опрошенная ТТН немедленно покидала выборку трекинга — и тест голодания
   был зелёным при любой сортировке. Теперь доля документов отвечает неизменным
   статусом (это и есть брошенные), а часть не отвечает вовсе.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from app.sheets.source import StockRow, StockSourceUnavailable

#: Замеры боевого прогона. Меняются только вместе с новыми замерами.
NP_COUNTERPARTY_SECONDS = 0.9
NP_DOCUMENT_SECONDS = 2.5
NP_REFERENCE_SECONDS = 0.4
SHEETS_READ_SECONDS = 0.74
SHEETS_WRITE_SECONDS = 0.55

#: Публичная квота Google на service-account: 60 чтений и 60 записей в минуту.
#: Само число — гипотеза (в документации мы её не нашли), и закрыть её может
#: только живой прогон. Здесь оно вынесено в константу именно поэтому.
GOOGLE_READS_PER_MINUTE = 60
GOOGLE_WRITES_PER_MINUTE = 60


class HarnessError(RuntimeError):
    """Сломался стенд, а не система под нагрузкой.

    Отдельный тип обязателен: «уперлись в квоту» — это измеряемый результат
    прогона, «фейк отдал чушь» — баг харнесса. Смешать их значит получить прогон,
    который что-то показал, но неизвестно что.
    """


class QuotaExceeded(StockSourceUnavailable):
    """То, чем Google отвечает на превышение, — HTTP 429.

    Наследуется от `StockSourceUnavailable` со `status=429` не для красоты, а
    чтобы исключение прошло **тем же путём**, что настоящее: `_retryable`
    (`app/sheets/runtime.py`) пропустит его, тенасити отретраит, каждая попытка
    ударит по ведру, а `errors`-роутер покажет «Склад тимчасово недоступний», а не
    «Сталася помилка». Без этого измеряется не наша система.
    """

    def __init__(self, client_key: str | None = None) -> None:
        super().__init__(client_key, 429)


@dataclass
class SharedTokenBucket:
    """Скользящее окно на минуту, общее для всех процессов прогона.

    Файл + `flock`, а не память процесса: квота Google считается на
    service-account, то есть на бота, воркера и все процессы харнесса разом.

    Запись под блокировкой идёт с `flush` + `fsync` **до** `LOCK_UN` — иначе
    соседний процесс читает усечённый JSON. Эти грабли в проекте уже оплачены
    (`scripts/e2e/cascade.py::TtnBudget`, регресс закрыт `tests/bot/test_e2e_lib.py`).
    """

    path: Path
    capacity: int
    window: float = 60.0

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(json.dumps({"hits": [], "rejected": 0}))

    def _rewrite(self, fh, state: dict) -> None:
        fh.seek(0)
        fh.truncate()
        json.dump(state, fh)
        fh.flush()
        os.fsync(fh.fileno())

    def take(self) -> None:
        """Занять слот. Превышение — `QuotaExceeded` (то есть 429 по форме)."""
        now = time.time()  # НЕ monotonic: между процессами он несравним
        with self.path.open("r+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                state = json.load(fh)
                hits = [t for t in state.get("hits", []) if t > now - self.window]
                if len(hits) >= self.capacity:
                    state["hits"] = hits
                    state["rejected"] = state.get("rejected", 0) + 1
                    self._rewrite(fh, state)
                    raise QuotaExceeded()
                hits.append(now)
                state["hits"] = hits
                self._rewrite(fh, state)
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def snapshot(self) -> dict[str, int]:
        with self.path.open("r") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            try:
                state = json.load(fh)
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        now = time.time()
        return {
            "in_window": sum(1 for t in state.get("hits", []) if t > now - self.window),
            "rejected": int(state.get("rejected", 0)),
        }


class GoogleMeter:
    """Счётчик обращений к книге с квотой и событийным журналом.

    Считает **обращения**, а не доменные операции: `apply_deltas` — это три чтения
    и одна запись, и разница между «одна запись на корзину» и «одна запись на
    позицию» меняет расчётный потолок на порядок.

    Журнал событий с таймстампами нужен, чтобы «60 в минуту» можно было проверить
    вообще: по суммарным счётчикам за весь прогон это невыводимо.
    """

    #: Методы gspread по направлению. Всё, чего здесь нет, — баг стенда, а не
    #: «ноль расхода»: молча не считать обращение хуже, чем упасть.
    READ_METHODS = frozenset(
        {"open_by_key", "worksheet", "get_all_records", "row_values", "col_values", "get_values"}
    )
    WRITE_METHODS = frozenset({"update", "batch_update", "batch_clear", "append_row"})

    def __init__(
        self,
        *,
        state_dir: Path,
        read_seconds: float = SHEETS_READ_SECONDS,
        write_seconds: float = SHEETS_WRITE_SECONDS,
        reads_per_minute: int = GOOGLE_READS_PER_MINUTE,
        writes_per_minute: int = GOOGLE_WRITES_PER_MINUTE,
    ) -> None:
        self.reads = SharedTokenBucket(state_dir / "google_reads.json", reads_per_minute)
        self.writes = SharedTokenBucket(state_dir / "google_writes.json", writes_per_minute)
        self._read_seconds = read_seconds
        self._write_seconds = write_seconds
        self._events = (state_dir / f"google_events.{os.getpid()}.jsonl").open("a")

    def charge(self, method: str, *, client_key: str | None = None) -> None:
        """Списать обращение. Бросает `QuotaExceeded` (429) при превышении."""
        if method in self.READ_METHODS:
            bucket, delay, direction = self.reads, self._read_seconds, "read"
        elif method in self.WRITE_METHODS:
            bucket, delay, direction = self.writes, self._write_seconds, "write"
        else:
            raise HarnessError(f"неизвестный метод книги: {method!r} — счётчик молча промахнётся")

        started = time.time()
        try:
            bucket.take()
        except QuotaExceeded:
            self._record(started, method, direction, ok=False)
            raise
        if delay:
            time.sleep(delay)
        self._record(started, method, direction, ok=True)

    def _record(self, ts: float, method: str, direction: str, *, ok: bool) -> None:
        self._events.write(
            json.dumps({"ts": ts, "method": method, "direction": direction, "ok": ok}) + "\n"
        )
        self._events.flush()

    def snapshot(self) -> dict[str, Any]:
        return {"reads": self.reads.snapshot(), "writes": self.writes.snapshot()}

    def close(self) -> None:
        self._events.close()


class QuotaSheetsSource:
    """Фейковый `StockSource` поверх `GoogleMeter`.

    Синхронный намеренно: настоящий источник гоняется через single-worker executor
    (`app/sheets/runtime.py`), и весь Sheets-I/O процесса из-за этого сериализован.
    Асинхронный фейк стёр бы ровно то свойство, из-за которого очередь и растёт.

    Число обращений повторяет настоящий адаптер (`app/sheets/inventory.py`):
    `read_stock` — `open_by_key` + `worksheet` + `get_all_records`;
    `apply_deltas` — те же три чтения и **один** `batch_update` на всю пачку.
    """

    def __init__(self, rows_by_key: dict[str, list[StockRow]], *, meter: GoogleMeter) -> None:
        self._rows = rows_by_key
        self._meter = meter

    def read_stock(self, client_key: str) -> list[StockRow]:
        for method in ("open_by_key", "worksheet", "get_all_records"):
            self._meter.charge(method, client_key=client_key)
        return list(self._rows.get(client_key, []))

    def apply_deltas(self, client_key: str, deltas) -> None:
        for method in ("open_by_key", "worksheet", "get_all_records"):
            self._meter.charge(method, client_key=client_key)
        # Одна запись на всю пачку — как настоящий `batch_update`, а не на позицию.
        self._meter.charge("batch_update", client_key=client_key)


@dataclass
class NovaPoshtaFake:
    """`httpx.MockTransport` с задержками боевых замеров и профилем ответов.

    Профиль — не украшение. Если на любой запрос отвечать `dispatched`, каждая
    опрошенная ТТН немедленно покидает `TRACKABLE_STATUSES`, и «все опрошены за N
    проходов» получается **при любой сортировке** — то есть проверяется не тот
    дефект, ради которого писался тест голодания.

    Судьба документа задаётся **префиксом номера**, а не долей:

    - `unchanged_prefixes` — отвечают прежним статусом (`StatusCode="1"`). Это и
      есть брошенные: из выборки они не уходят и честно конкурируют за лимит;
    - `silent_prefixes` — НП не возвращает строку вовсе (ветка `tracking is None`
      в `app/services/tracking.py`);
    - остальные — уезжают (`StatusCode="3"`).

    Префикс, а не доля по хэшу номера: доля должна была совпасть с тем, какие
    строки сид пометил брошенными, а совпасть она не могла — первая версия делила
    по последним цифрам, и свежие ТТН с малым суффиксом тоже отвечали «не
    изменился». Дефект нашёлся первым же прогоном: очередь не убывала. Связь
    «кого сид завёл брошенным» и «кто отвечает неизменным статусом» обязана быть
    явной, иначе воспроизводится не тот профиль.
    """

    calls: dict[str, int] = field(default_factory=dict)
    document_seconds: float = NP_DOCUMENT_SECONDS
    counterparty_seconds: float = NP_COUNTERPARTY_SECONDS
    reference_seconds: float = NP_REFERENCE_SECONDS
    unchanged_prefixes: tuple[str, ...] = ()
    silent_prefixes: tuple[str, ...] = ()
    #: С чего начинать нумерацию выданных ТТН. Обязан различаться между прогонами:
    #: `shipments.ttn_number` уникален в схеме, и фиксированная база означала бы,
    #: что второй прогон по той же базе получает `IntegrityError` на КАЖДОМ
    #: сабмите. Наружу это выглядело как «⚠️ Сталася помилка» и 0 % созданных —
    #: то есть как отказ системы под нагрузкой, хотя ломался стенд. Поймано
    #: прогоном профилей: 1 ТТН/мин дал 100 %, все последующие — 0 %.
    serial_base: int = 59_000_000
    _serial: int = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = f"{body['modelName']}.{body['calledMethod']}"
        self.calls[method] = self.calls.get(method, 0) + 1
        await asyncio.sleep(self._latency(body["modelName"]))
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": self._data(body),
                "errors": [],
                "errorCodes": [],
                "warnings": [],
                "info": [],
            },
        )

    def _latency(self, model: str) -> float:
        if model == "InternetDocument":
            return self.document_seconds
        if model in ("Counterparty", "ContactPerson"):
            return self.counterparty_seconds
        return self.reference_seconds

    def _bucket(self, number: str) -> str:
        """Куда попадает документ: молчание, неизменный статус или отправка.

        По префиксу номера — тот же номер всегда получает тот же ответ, и связь с
        тем, что завёл сид, видна глазами.
        """
        if self.silent_prefixes and number.startswith(self.silent_prefixes):
            return "silent"
        if self.unchanged_prefixes and number.startswith(self.unchanged_prefixes):
            return "unchanged"
        return "dispatched"

    def _tracking_rows(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for doc in body.get("methodProperties", {}).get("Documents", []):
            number = doc.get("DocumentNumber", "")
            bucket = self._bucket(number)
            if bucket == "silent":
                continue
            if bucket == "unchanged":
                rows.append({"Number": number, "Status": "Прийнято", "StatusCode": "1"})
                continue
            rows.append(
                {
                    "Number": number,
                    "Status": "Відправлено",
                    "StatusCode": "3",
                    # Формат боевой НП: время ВПЕРЕДИ даты и без секунд. Проверено
                    # живым пробником (`app/novaposhta/tracking.py`); придуманный
                    # `01.01.2027 10:00:00` не воспроизводил тот разбор, из-за
                    # которого вердикт SLA чуть не выродился в «не знаем».
                    "DateScan": "20:05 01.08.2026",
                }
            )
        return rows

    def _data(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        model, called = body["modelName"], body["calledMethod"]
        if model == "InternetDocument" and called == "save":
            self._serial = (self._serial or self.serial_base) + 1
            return [
                {
                    "Ref": f"doc-{self._serial}",
                    "IntDocNumber": str(self._serial),
                    "CostOnSite": 70,
                    "EstimatedDeliveryDate": "01.01.2027",
                }
            ]
        if model == "InternetDocument" and called == "getDocumentPrice":
            return [{"Cost": 70, "CostRedelivery": 20}]
        if model in ("Counterparty", "ContactPerson"):
            return [{"Ref": "cp-ref", "ContactPerson": {"data": [{"Ref": "ct-ref"}]}}]
        if model == "Address" and called == "getCities":
            # Ровно та форма, которую разбирает `methods.get_cities`: `Ref` +
            # `Description`. Первая версия отвечала на `searchSettlements` —
            # метода, которого в коде нет вовсе, — и город не находился никогда.
            return [
                {"Ref": "city-ref", "Description": "Київ", "AreaDescription": "Київська"},
            ]
        if model == "Address" and called == "getWarehouses":
            return [
                {
                    "Ref": f"wh-{i}",
                    "Description": f"Відділення №{i}",
                    "Number": str(i),
                    "CityRef": "city-ref",
                    "TypeOfWarehouse": "841339c7-591a-42e2-8233-7a0a00f0ed6f",
                }
                for i in range(1, 6)
            ]
        if model == "TrackingDocument":
            return self._tracking_rows(body)
        return [{}]

    def snapshot(self) -> dict[str, Any]:
        return {"np_calls": dict(self.calls), "np_total": sum(self.calls.values())}


def stock_rows(count: int, *, prefix: str = "SKU") -> list[StockRow]:
    """Строки остатка для фейкового листа — форма как у боевых данных."""
    return [
        StockRow(
            sku=f"{prefix}-{i:04d}",
            name=f"Товар {i:04d}",
            category=("Кава", "Чай", "Какао")[i % 3],
            quantity=500,
            price=Decimal("100.00"),
        )
        for i in range(count)
    ]
