"""Профили сабмита ТТН: где колено.

Гоняет N «операторов» через **настоящий** `build_dispatcher` живыми
aiogram-`Update` с заданной интенсивностью и пишет сырой покадровый JSONL.
Вердикт выносит `report.py`, а не этот скрипт: правило харнесса — «персона пишет
лог, зелено/красно решает одно место».

**Топология: один процесс, N персон.** Ограничение `scripts/e2e/README.md` («один
процесс — одна персона») — на второй `Dispatcher`, а не на вторую персону:
роутеры в `app/bot/handlers` модульные синглтоны, но `dp.feed_update(bot, update)`
принимает бота аргументом, а ключ FSM строится из `(bot_id, chat_id, user_id)`.
Ровно так работает прод.

Разносить персон по процессам **нельзя**, и это не про удобство: каждый процесс
поднял бы свой пул (20+30 — при 20 процессах до 1000 коннектов, локальный
Postgres откажет), а главное — одна персона на процесс означает один апдейт в
момент времени, то есть **ожидание коннекта в пуле и глубина очереди
Sheets-executor'а тождественно нулевые**. Это две из метрик, ради которых прогон
и делается.

Что меряется помимо латентности шагов:

- **глубина и время ожидания в очереди Sheets-executor'а** — подменой
  `app.sheets.runtime._sheets_executor`. Модуль читает этот глобал **в момент
  вызова**, поэтому правка `app/` не нужна;
- **ожидание коннекта в пуле** — событиями пула SQLAlchemy;
- **расход Google по минутам** — событийным журналом `GoogleMeter`.

Гонять только по базе на `_load` при `INVENTORY_SOURCE=pg` (см. `guards.py`).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.load.fakes import (
    NP_COUNTERPARTY_SECONDS,
    NP_DOCUMENT_SECONDS,
    NP_REFERENCE_SECONDS,
    GoogleMeter,
    NovaPoshtaFake,
)
from scripts.load.guards import (
    report_effective_settings,
    require_load_database,
    require_offline_stand,
    require_pg_inventory,
    require_seeded_operators,
)

#: Профили из модели нагрузки: пик 2,5 ТТН/мин, всплески 5–8. Берём с запасом.
PROFILES = (1, 3, 6, 10)
#: Одновременных операторов в форме — из модели нагрузки.
OPERATORS = 15
#: SKU, который берут все операторы. С ценой (первые пять позиций сида без неё
#: намеренно), и один на всех — чтобы гейт от oversell работал под нагрузкой
#: на общей строке остатка, а не расходился по разным.
_PICK_SKU = "SKU-0100"


@dataclass
class ExecutorProbe:
    """Глубина очереди single-worker executor'а и время ожидания в ней.

    Снимается подменой глобала, а не правкой `app/`: `run_on_sheets_executor`
    читает `_sheets_executor` в момент вызова.
    """

    depth: int = 0
    max_depth: int = 0
    waits: list[float] = field(default_factory=list)

    def wrap(self, executor: ThreadPoolExecutor) -> ThreadPoolExecutor:
        probe = self
        original = executor.submit

        def submit(fn, /, *args, **kwargs):
            probe.depth += 1
            probe.max_depth = max(probe.max_depth, probe.depth)
            queued = time.perf_counter()

            def wrapped(*a, **kw):
                probe.waits.append(time.perf_counter() - queued)
                probe.depth -= 1
                return fn(*a, **kw)

            return original(wrapped, *args, **kwargs)

        executor.submit = submit  # type: ignore[method-assign]
        return executor

    def snapshot(self) -> dict[str, float]:
        return {
            "max_depth": self.max_depth,
            "wait_p95_ms": _percentile(self.waits, 0.95) * 1000,
            "wait_max_ms": (max(self.waits) if self.waits else 0.0) * 1000,
        }


@dataclass
class PoolProbe:
    """Насыщение пула коннектов: сколько занято одновременно и были ли отказы.

    Меряем **занятость**, а не «время ожидания». SQLAlchemy не эмитит события
    «начал ждать коннект» — есть только `checkout`, то есть момент, когда коннект
    уже выдан. Первая версия вешала два слушателя на одно и то же событие и
    выдавала разницу между ними за ожидание: получались правдоподобные 3,6 с,
    которые не значили ничего. Правдоподобное неверное число хуже отсутствующего —
    по нему принимают решения.

    Занятость против `pool_size + max_overflow` отвечает на тот вопрос, ради
    которого метрика и заводилась: подошли ли мы к исчерпанию пула.
    """

    limit: int = 0
    max_checked_out: int = 0
    timeouts: int = 0
    _engine: Any = None
    _task: Any = None

    def install(self, engine, *, settings) -> None:
        self._engine = engine
        self.limit = settings.db_pool_size + settings.db_max_overflow

    async def sample_until(self, stop: asyncio.Event, *, period: float = 0.05) -> None:
        """Опрашивать пул, пока идёт прогон. Пик — то, что нас интересует."""
        pool = self._engine.sync_engine.pool
        while not stop.is_set():
            self.max_checked_out = max(self.max_checked_out, pool.checkedout())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=period)

    def snapshot(self) -> dict[str, float]:
        return {
            "max_checked_out": self.max_checked_out,
            "limit": self.limit,
            "utilisation": round(self.max_checked_out / self.limit, 3) if self.limit else 0.0,
            "timeouts": self.timeouts,
        }


def warn_if_profile_not_reproduced(
    *, rate: float, operators: int, elapsed: float, achieved: float
) -> None:
    """Сказать вслух, если заявленный профиль не был воспроизведён.

    Интервал между сабмитами одного оператора — `operators*60/rate`. Окно короче
    него означает, что каждый успел ровно один сабмит, и профиль выродился в
    «столько, сколько влезло». Молчать здесь нельзя: отчёт выглядит как
    полноценная развёртка, а на деле это одна точка, измеренная N раз.
    """
    interval = operators * 60.0 / rate if rate else 0.0
    if interval > elapsed:
        print(
            f"⚠️  вікно {elapsed:.0f} с коротше інтервалу оператора {interval:.0f} с: "
            f"профіль {rate} ТТН/хв не відтворено, фактично {achieved:.1f} ТТН/хв. "
            f"Потрібно --seconds >= {interval * 3:.0f} або менше операторів."
        )


def _percentile(values: list[float], q: float) -> float:
    """Nearest-rank, как в `scripts/e2e/validate.py` — чтобы отчёты сравнивались."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


async def _one_submit(persona, *, index: int) -> dict:
    """Один сабмит целиком через живые апдейты. Возвращает исход шага.

    Успех определяется **по ответу бота**, а не по факту тапа: тап проходит и
    тогда, когда экран отдал «⚠️ Склад тимчасово недоступний», — а такой прогон
    ещё и быстрее успешного, то есть по латентности выглядел бы лучше.

    Последовательность — как у `scripts/e2e/cascade._one_ttn`, но без «человеческих»
    отвлечений (листание, опечатки): здесь меряется пропускная способность, и
    разброс шагов только зашумил бы перцентили. `/start` обязателен: без него у
    персоны нет экрана вовсе, и все тапы уходят в пустоту (`missing: true`) —
    первый прогон этого драйвера дал ровно это.
    """
    started = time.perf_counter()
    outcome: dict = {"index": index, "submitted": False, "failed_at": None}
    try:
        await persona.send("/start")
        button = persona.screen.find_reply("Створити ТТН")
        if button is None:
            outcome["failed_at"] = "start"
            outcome["screen"] = persona.screen.text[:200]
            return outcome
        await persona.send(button.text)

        # Несколько ФОП — появляется экран выбора отправителя (так у части аккаунтов).
        for prefix in ("ttn:sender:", "cab:ttn:sender:"):
            if persona.screen.find_data(prefix):
                await persona.tap_data(prefix)
                break

        # Товар выбираем ПОИСКОМ по конкретному SKU, а не «первый в списке».
        # Первые пять позиций сида намеренно без цены (как в проде: 1631 из 1636),
        # а без цены бот блокирует отправку — «Вкажіть оголошену вартість». Первый
        # прогон упёрся ровно в это: доходил до карточки и получал отказ.
        await persona.tap_data("cab:ttn:search")
        await persona.send(_PICK_SKU)
        for attempt in range(6):
            if not await persona.tap_data("cab:ttn:pick:", nth=attempt):
                break
            if persona.screen.find_data("cab:ttn:qok"):
                break
        if not persona.screen.find_data("cab:ttn:qok"):
            outcome["failed_at"] = "picker"
            outcome["screen"] = persona.screen.text[:200]
            return outcome

        await persona.tap_data("cab:ttn:qd:1")
        if not await persona.tap_data("cab:ttn:qok"):
            outcome["failed_at"] = "stepper"
            outcome["screen"] = persona.screen.text[:200]
            return outcome
        # После добавления бот возвращает на пикер, и «Далі» там нет — сперва в
        # корзину. Тот же порядок, что в `cascade._one_ttn`.
        if not await persona.tap_data("cab:ttn:next"):
            await persona.tap_data("cab:ttn:cart")
            if not await persona.tap_data("cab:ttn:next"):
                outcome["failed_at"] = "cart"
                outcome["screen"] = persona.screen.text[:200]
                return outcome

        # Шаг 2 — параметры посылки, шаг 3 — получатель.
        await persona.tap_data("cab:ttn:sz:m")
        if not await persona.tap_data("cab:ttn:torcpt"):
            outcome["failed_at"] = "parcel"
            outcome["screen"] = persona.screen.text[:200]
            return outcome

        # Шаг 3 — отримувач. Данные фиксированные: меряется пропускная
        # способность, а не разбор пользовательского ввода.
        await persona.tap_data("cab:ttn:rk:p")
        await persona.send("Іван Петренко")
        await persona.send("380671234567")
        await persona.send("Київ")
        # При единственном совпадении бот сам уходит на выбор відділення —
        # тапать город тогда не по чему, и жёсткая проверка ложно валила шаг.
        if persona.screen.find_data("cab:ttn:city:"):
            await persona.tap_data("cab:ttn:city:")
        if not await persona.tap_data("cab:ttn:wh:"):
            outcome["failed_at"] = "warehouse"
            outcome["screen"] = persona.screen.text[:200]
            return outcome
        if not persona.screen.find_data("cab:ttn:send"):
            outcome["failed_at"] = "card"
            outcome["screen"] = persona.screen.text[:200]
            return outcome

        step = await persona.tap_data("cab:ttn:send")
        screen = str((step or {}).get("screen_text", "")) or persona.screen.text
        outcome["submitted"] = "ТТН створено" in screen
        outcome["error_screen"] = (step or {}).get("error_screen")
        outcome["screen"] = screen[:200]
        if not outcome["submitted"]:
            outcome["failed_at"] = "submit"
    except Exception as exc:
        outcome["exception"] = f"{type(exc).__name__}: {exc}"
        outcome["failed_at"] = "exception"
    outcome["ms"] = round((time.perf_counter() - started) * 1000, 1)
    return outcome


async def _operator(persona, *, rate_per_minute: float, seconds: float, results: list) -> None:
    """Один оператор: сабмиты с заданной интенсивностью, пока не выйдет время."""
    interval = 60.0 / rate_per_minute if rate_per_minute else 0.0
    deadline = time.perf_counter() + seconds
    index = 0
    while time.perf_counter() < deadline:
        tick = time.perf_counter()
        results.append(await _one_submit(persona, index=index))
        index += 1
        # Ритм держим от НАЧАЛА шага: иначе интенсивность падает вместе с
        # латентностью, и профиль «10 ТТН/мин» превращается в «сколько выйдет».
        #
        # Сон обрезаем по дедлайну прогона. Без этого на малой интенсивности
        # оператор засыпает дольше, чем длится замер: при 1 ТТН/мин на 15
        # операторов интервал — 15 минут, и прогон «на 120 секунд» висел бы
        # четверть часа. Поймано прогоном, а не рассуждением.
        sleep_for = min(interval - (time.perf_counter() - tick), deadline - time.perf_counter())
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)


async def _run(*, rate: float, operators: int, seconds: float, artifacts: Path) -> None:
    import app.sheets.runtime as sheets_runtime
    from app.config import get_settings
    from app.db.base import get_engine
    from scripts.e2e.lib import attach_persona, build_persona

    settings = get_settings()
    require_load_database(settings.database_url)
    require_offline_stand()
    require_pg_inventory(settings)

    print("Ефективна конфігурація стенда:")
    for key, value in report_effective_settings(settings).items():
        print(f"  {key} = {value}")
    print(f"  профіль = {rate} ТТН/хв, операторів = {operators}, тривалість = {seconds:.0f} с\n")

    run_dir = artifacts / f"submit-{rate:g}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Очередь Sheets-executor'а: подменяем ДО первого обращения к складу.
    probe = ExecutorProbe()
    sheets_runtime._sheets_executor = probe.wrap(sheets_runtime._sheets_executor)

    meter = GoogleMeter(state_dir=run_dir)
    np_fake = NovaPoshtaFake(
        # База нумерации своя на прогон: `shipments.ttn_number` уникален, и общая
        # база означала бы IntegrityError на каждом сабмите второго прогона по той
        # же базе. Наружу это выглядит как «⚠️ Сталася помилка» и 0 % созданных —
        # то есть как отказ системы, хотя ломается стенд.
        serial_base=int(time.time()) % 1_000_000 * 100,
        document_seconds=NP_DOCUMENT_SECONDS,
        counterparty_seconds=NP_COUNTERPARTY_SECONDS,
        reference_seconds=NP_REFERENCE_SECONDS,
    )

    first, np_client, redis_client = await build_persona(
        name="op-00",
        telegram_id=900_000_000,
        np_transport=np_fake.transport(),
        log_path=run_dir / "op-00.jsonl",
        chat_id=900_000_000,
        install_sheets_meter=False,
    )
    pool = PoolProbe()
    pool.install(get_engine(), settings=settings)

    # Незасеянный оператор упирается в экран авторизации, и прогон показывает 0 %
    # созданных — что читается как «система не тянет», хотя она не участвовала.
    telegram_ids = [900_000_000 + i * 100 for i in range(operators)]
    from app.db.base import get_sessionmaker

    async with get_sessionmaker()() as session:
        await require_seeded_operators(session, telegram_ids)

    personas = [first] + [
        attach_persona(
            name=f"op-{i:02d}",
            telegram_id=900_000_000 + i * 100,
            dispatcher=first.dp,
            log_path=run_dir / f"op-{i:02d}.jsonl",
            chat_id=900_000_000 + i * 100,
        )
        for i in range(1, operators)
    ]

    results: list[dict] = []
    per_operator = rate / max(operators, 1)
    stop = asyncio.Event()
    sampler = asyncio.create_task(pool.sample_until(stop))
    started = time.perf_counter()
    await asyncio.gather(
        *(
            _operator(p, rate_per_minute=per_operator, seconds=seconds, results=results)
            for p in personas
        )
    )
    elapsed = time.perf_counter() - started
    stop.set()
    await sampler

    for persona in personas:
        persona.close()
    meter.close()
    await np_client.aclose()
    await redis_client.aclose()

    attempted = len(results)
    submitted = sum(1 for r in results if r["submitted"])
    achieved = attempted / elapsed * 60 if elapsed else 0.0

    # Заявленная интенсивность и фактическая — разные вещи, и расхождение обязано
    # быть видно. Интервал между сабмитами одного оператора — `operators*60/rate`;
    # если окно прогона короче него, оператор успевает ровно один сабмит, и профиль
    # вырождается в «столько, сколько влезло». Первый сweep дал ровно это: 1, 3, 6
    # и 10 ТТН/мин показали одинаковые 10 ТТН/мин фактических, то есть колена не
    # искали вовсе — четыре раза измерили одну точку.
    interval = operators * 60.0 / rate if rate else 0.0
    warn_if_profile_not_reproduced(
        rate=rate, operators=operators, elapsed=elapsed, achieved=achieved
    )
    payload = {
        "rate_per_minute": rate,
        "operators": operators,
        "seconds": round(elapsed, 1),
        "attempted": attempted,
        "submitted": submitted,
        "achieved_per_minute": round(achieved, 2),
        "operator_interval_seconds": round(interval, 1),
        "success_ratio": round(submitted / attempted, 4) if attempted else 0.0,
        "error_screens": sum(1 for r in results if r.get("error_screen")),
        "exceptions": sum(1 for r in results if r.get("exception")),
        "submit_ms": {
            "p50": _percentile([r["ms"] for r in results if r["submitted"]], 0.50),
            "p95": _percentile([r["ms"] for r in results if r["submitted"]], 0.95),
            "p99": _percentile([r["ms"] for r in results if r["submitted"]], 0.99),
        },
        "sheets_executor": probe.snapshot(),
        "db_pool": pool.snapshot(),
        "google": meter.snapshot(),
        "np": np_fake.snapshot(),
        "results": results,
    }
    await asyncio.to_thread(
        (run_dir / "submit.json").write_text,
        json.dumps(payload, ensure_ascii=False, indent=1),
    )

    print(
        f"фактично {achieved:.1f} ТТН/хв, "
        f"створено {submitted}/{attempted} "
        f"({payload['success_ratio']:.1%}), "
        f"p95 сабміту {payload['submit_ms']['p95']:.0f} мс, "
        f"черга Sheets max {probe.max_depth}, "
        f"429 read {meter.reads.snapshot()['rejected']}"
    )
    print(f"Сирий звіт: {run_dir / 'submit.json'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rate", type=float, default=3.0, help="ТТН/мин на весь стенд")
    parser.add_argument("--operators", type=int, default=OPERATORS)
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument(
        "--artifacts", type=Path, default=Path(__file__).resolve().parent / "artifacts"
    )
    args = parser.parse_args()
    asyncio.run(
        _run(
            rate=args.rate,
            operators=args.operators,
            seconds=args.seconds,
            artifacts=args.artifacts,
        )
    )


if __name__ == "__main__":
    main()
