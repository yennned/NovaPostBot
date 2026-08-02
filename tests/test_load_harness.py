"""Тесты нагрузочного харнесса (`scripts/load/`).

Харнесс — инструмент, которым меряют систему. Сгниёт он — прогон станет зелёным
просто потому, что перестал что-либо замечать, и это худший исход: он выглядит как
доказательство готовности. Дисциплина та же, что у `tests/bot/test_e2e_lib.py`.

Проверяются ровно те свойства, потеря которых делает прогон ложно-зелёным, и
каждое из них уже было сломано в первой версии фейков:

1. 429 обязан проходить по тому же пути, что настоящий, — иначе в прогоне нет
   усиления ретраями, и насыщение из положительной обратной связи превращается в
   отрицательную;
2. ведро квоты общее на процессы — иначе N процессов дают потолок в N раз выше;
3. считаются обращения к книге, а не доменные операции — иначе расчётный потолок
   ошибается на порядок;
4. фейк НП отвечает по профилю — иначе тест голодания зелёный при любой сортировке.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest
from app.sheets.runtime import _retryable
from app.sheets.source import StockSourceUnavailable
from scripts.load.fakes import (
    GoogleMeter,
    HarnessError,
    NovaPoshtaFake,
    QuotaExceeded,
    QuotaSheetsSource,
    SharedTokenBucket,
    stock_rows,
)


def test_quota_error_travels_the_same_path_as_a_real_429():
    """429 фейка обязан быть `StockSourceUnavailable` со `status=429`.

    Иначе `_retryable` его не пропустит, тенасити не отретраит, и прогон измерит
    систему, у которой под перегрузкой **отрицательная** обратная связь: отказ
    мгновенный, квота не тратится, очередь разгружается. В проде наоборот — один
    read при 429 становится тремя HTTP, и насыщение разгоняет само себя.

    Мутация: `class QuotaExceeded(RuntimeError)` — тест краснеет на обоих assert'ах.
    """
    exc = QuotaExceeded("Магазин 01")
    assert isinstance(exc, StockSourceUnavailable)
    assert exc.status == 429
    assert _retryable(exc) is True, "иначе ретраи не отработают и усиления не будет"


def test_harness_error_is_not_mistaken_for_a_quota_hit():
    """«Сломался стенд» и «упёрлись в квоту» — разные типы.

    Смешать их значит получить прогон, который что-то показал, но неизвестно что.
    """
    assert not issubclass(HarnessError, StockSourceUnavailable)


def test_bucket_rejects_past_capacity(tmp_path: Path):
    bucket = SharedTokenBucket(tmp_path / "b.json", capacity=3)
    for _ in range(3):
        bucket.take()
    with pytest.raises(QuotaExceeded):
        bucket.take()
    assert bucket.snapshot() == {"in_window": 3, "rejected": 1}


def _take_in_child(path_and_n: tuple[str, int]) -> int:
    """Занять N слотов в отдельном ПРОЦЕССЕ. Возвращает число успешных."""
    path, n = path_and_n
    bucket = SharedTokenBucket(Path(path), capacity=10)
    taken = 0
    for _ in range(n):
        try:
            bucket.take()
        except QuotaExceeded:
            pass
        else:
            taken += 1
    return taken


def test_bucket_is_shared_across_processes(tmp_path: Path):
    """Квота Google считается на service-account — то есть на все процессы разом.

    Ведро в памяти процесса при четырёх процессах дало бы 40 слотов вместо 10, и
    критерий «ноль 429» выполнялся бы арифметически на любом профиле нагрузки,
    пока прод ложится.

    Мутация: заменить файл с `flock` на `threading.Lock` в памяти — сумма станет
    40, тест краснеет.
    """
    path = str(tmp_path / "shared.json")
    SharedTokenBucket(Path(path), capacity=10)  # создать файл до форка
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_take_in_child, [(path, 10)] * 4))

    assert sum(results) == 10, f"ведро не общее: суммарно занято {sum(results)} из 10"


def test_meter_counts_book_calls_not_domain_operations(tmp_path: Path):
    """`apply_deltas` — это три чтения и ОДНА запись, независимо от корзины.

    Первая версия считала запись на каждую `StockDelta`. Настоящий адаптер делает
    один `batch_update` на всю пачку, зато три чтения на подготовку. То есть в
    квоту упирается **чтение**, и расход **не зависит от размера корзины** —
    ошибка в противоположную, опасную сторону: харнесс с корзинами по одной
    позиции показал бы потолок 60 ТТН/мин там, где прод даёт единицы.
    """
    meter = GoogleMeter(state_dir=tmp_path, read_seconds=0.0, write_seconds=0.0)
    source = QuotaSheetsSource({"Магазин 01": stock_rows(3)}, meter=meter)

    source.apply_deltas("Магазин 01", [object()] * 8)
    meter.close()

    assert meter.reads.snapshot()["in_window"] == 3
    assert meter.writes.snapshot()["in_window"] == 1, "одна запись на пачку, а не на позицию"


def test_meter_refuses_unknown_book_method(tmp_path: Path):
    """Незнакомый метод — падение, а не молчаливый ноль расхода.

    Не посчитать обращение хуже, чем упасть: прогон отрапортует запас по квоте,
    которого нет.
    """
    meter = GoogleMeter(state_dir=tmp_path, read_seconds=0.0, write_seconds=0.0)
    with pytest.raises(HarnessError):
        meter.charge("some_new_gspread_call")
    meter.close()


def test_meter_writes_event_log_with_timestamps(tmp_path: Path):
    """Без событийного журнала «60 в минуту» не проверить в принципе.

    По суммарным счётчикам за весь прогон пик в минутном окне невыводим.
    """
    meter = GoogleMeter(state_dir=tmp_path, read_seconds=0.0, write_seconds=0.0)
    QuotaSheetsSource({}, meter=meter).read_stock("Магазин 01")
    meter.close()

    events = [
        json.loads(line)
        for path in tmp_path.glob("google_events.*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert len(events) == 3
    assert {e["direction"] for e in events} == {"read"}
    assert all(isinstance(e["ts"], float) for e in events)


def _tracking_reply(fake: NovaPoshtaFake, numbers: list[str]) -> list[dict]:
    body = {
        "modelName": "TrackingDocument",
        "calledMethod": "getStatusDocuments",
        "methodProperties": {"Documents": [{"DocumentNumber": n} for n in numbers]},
    }
    return fake._data(body)


def test_np_fake_keeps_abandoned_in_the_tracking_set():
    """Брошенные обязаны отвечать НЕИЗМЕННЫМ статусом.

    Если фейк на любой запрос отвечает `dispatched`, каждая опрошенная ТТН
    немедленно покидает `TRACKABLE_STATUSES`, и «все опрошены за N проходов»
    получается при ЛЮБОЙ сортировке — то есть проверяется не тот дефект, ради
    которого написан тест голодания. Проверено на стенде: с этим профилем мутация
    «вернуть `ORDER BY status_changed_at`» оставляет 638 из 1200 ТТН неопрошенными
    навсегда, без него — все опрошены за 3 прохода при любой сортировке.
    """
    fake = NovaPoshtaFake(unchanged_prefixes=("59",))
    rows = _tracking_reply(fake, ["59000000000001", "58000000000001"])

    by_number = {row["Number"]: row for row in rows}
    assert by_number["59000000000001"]["StatusCode"] == "1", "брошенная не сміє їхати"
    assert by_number["58000000000001"]["StatusCode"] == "3"


def test_np_fake_can_stay_silent_about_a_document():
    """Ветка «НП спросили, а строку не вернула» обязана воспроизводиться.

    Именно на ней документ навсегда застревал в голове очереди, пока
    `tracking_updated_at` не начали ставить и без ответа.
    """
    fake = NovaPoshtaFake(silent_prefixes=("57",))
    rows = _tracking_reply(fake, ["57000000000001", "58000000000001"])

    assert [row["Number"] for row in rows] == ["58000000000001"]


def test_np_fake_uses_the_real_datescan_format():
    """Формат боевой НП: время ВПЕРЕДИ даты и без секунд.

    Придуманный `01.01.2027 10:00:00` не воспроизводил тот разбор, из-за которого
    вердикт SLA чуть не выродился в «не знаем» на всех накладных.
    """
    from app.novaposhta.schemas import TrackingStatus
    from app.novaposhta.tracking import dispatch_scan_time

    fake = NovaPoshtaFake()
    row = _tracking_reply(fake, ["58000000000001"])[0]
    scanned = dispatch_scan_time(
        TrackingStatus(number=row["Number"], status=row["Status"], status_code="3", raw=row)
    )

    assert scanned is not None, f"формат {row['DateScan']!r} не разбирается боевым парсером"


def test_np_fake_latency_matches_measured_profile():
    """Задержки — из боевых замеров, а не выдуманные: на них держится колено."""

    async def _run() -> float:
        import time

        import httpx

        fake = NovaPoshtaFake(document_seconds=0.05)
        transport = fake.transport()
        request = httpx.Request(
            "POST",
            "https://example.test",
            content=json.dumps({"modelName": "InternetDocument", "calledMethod": "save"}),
        )
        started = time.perf_counter()
        await transport.handler(request)
        return time.perf_counter() - started

    assert asyncio.run(_run()) >= 0.05


def test_np_fake_answers_the_city_method_the_app_actually_calls():
    """Город ищется через `Address.getCities`, а не `searchSettlements`.

    Первая версия фейка отвечала на метод, которого в `app/novaposhta/` нет
    вовсе, — и город не находился никогда. Прогон при этом не падал: он доходил
    до шага «місто» и вставал, а выглядело это как отказ бизнес-логики.
    """
    from app.novaposhta.schemas import City

    fake = NovaPoshtaFake()
    rows = fake._data({"modelName": "Address", "calledMethod": "getCities"})

    # Ровно тот разбор, что в `methods.get_cities`.
    cities = [
        City(ref=row["Ref"], name=row.get("Description", ""), area=row.get("AreaDescription"))
        for row in rows
        if row.get("Ref")
    ]
    assert cities and cities[0].name == "Київ"


def test_operator_never_sleeps_past_the_deadline():
    """Прогон обязан закончиться вовремя даже при низкой интенсивности.

    При 1 ТТН/мин на 15 операторов интервал между сабмитами — 15 минут. Без
    обрезки по дедлайну оператор уходил спать на весь интервал, и замер «на 90
    секунд» висел четверть часа. Дефект тихий: скрипт не падает, он просто не
    заканчивается, и это легко списать на «нагрузка большая».
    """
    from scripts.load.submit import _operator

    class _Stub:
        async def send(self, *_a, **_kw):
            return {}

        async def tap_data(self, *_a, **_kw):
            return {}

        @property
        def screen(self):
            raise AssertionError("до экрана дойти не должны: сценарий обрывается на /start")

    async def _run() -> float:
        import time

        results: list = []
        started = time.perf_counter()
        # Интервал 15 минут против дедлайна в 1 секунду.
        await asyncio.wait_for(
            _operator(_Stub(), rate_per_minute=1 / 15, seconds=1.0, results=results),
            timeout=10,
        )
        return time.perf_counter() - started

    assert asyncio.run(_run()) < 5, "оператор проспал дедлайн прогона"


def test_unreproduced_profile_is_reported_not_hidden(capsys):
    """Заявленный профиль и фактический — разные вещи, расхождение обязано быть видно.

    Интервал между сабмитами одного оператора — `operators*60/rate`. Если окно
    прогона короче него, оператор успевает ровно один сабмит, и профиль
    вырождается в «столько, сколько влезло». Первый sweep дал ровно это: цели 1,
    3, 6 и 10 ТТН/мин показали **одинаковые** 10 ТТН/мин фактических — то есть
    колена не искали вовсе, четыре раза измерили одну точку, а отчёт выглядел как
    полноценная развёртка.
    """
    from scripts.load.submit import warn_if_profile_not_reproduced

    warn_if_profile_not_reproduced(rate=1, operators=15, elapsed=90.0, achieved=10.0)
    assert "не відтворено" in capsys.readouterr().out

    warn_if_profile_not_reproduced(rate=10, operators=10, elapsed=180.0, achieved=9.8)
    assert capsys.readouterr().out == ""
