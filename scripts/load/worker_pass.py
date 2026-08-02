"""Проход трекинга на объёме: успевает ли воркер опросить всех живых.

Вопрос один: **за сколько проходов каждая живая ТТН будет опрошена хотя бы раз**.
До правки шага 1 ответ был «никогда»: выборка шла `ORDER BY status_changed_at
LIMIT 200`, а `status_changed_at` двигается только при СМЕНЕ статуса — документ с
неменяющимся статусом навсегда занимал слот лимита. Тест на 200 строках этого не
покажет: все 200 влезают в один проход.

**Почему брошенные обязаны отвечать неизменным статусом.** Первая версия фейка НП
отдавала `dispatched` на любой запрос, поэтому каждая опрошенная ТТН немедленно
покидала `TRACKABLE_STATUSES`, и «все опрошены за N проходов» получалось **при
любой сортировке** — мутация «вернуть `ORDER BY status_changed_at`» тест не
заваливала. Теперь половина документов отвечает `StatusCode="1"` (статус не
меняется) и из выборки не уходит: это и есть брошенные, ровно тот профиль, который
выбирает лимит. В проде 8 из 17 отправлений сидят в `confirmed` — 47%.

Скрипт НЕ ходит в НП: транспорт подменён (`scripts/load/fakes.py`). Измеряется
наша выборка, а не чужой сервис.

Два режима задержек:

- `--fast` — нули. Отвечает только на вопрос «голодает ли выборка».
- по умолчанию — боевые задержки. Списание при отправке идёт через тот же
  процессный single-worker executor, и сотня новых `dispatched` — это минуты
  сериализованного I/O внутри прохода при периоде 180 с. С нулями этот эффект,
  ради которого проход и деградирует, не воспроизводится вовсе.

Гонять только по базе, чьё имя оканчивается на `_load` (см. `guards.py`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.load.fakes import (
    NP_COUNTERPARTY_SECONDS,
    NP_DOCUMENT_SECONDS,
    NP_REFERENCE_SECONDS,
    SHEETS_READ_SECONDS,
    SHEETS_WRITE_SECONDS,
    GoogleMeter,
    NovaPoshtaFake,
    QuotaSheetsSource,
)
from scripts.load.guards import (
    report_effective_settings,
    require_load_database,
    require_offline_stand,
)


def _write_report(artifacts: Path, payload: str) -> None:
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "worker_pass.json").write_text(payload)


LIVE_DEFAULT = 5000
#: Доля брошенных среди живых. В проде сейчас 47% (8 из 17).
ABANDONED_SHARE = 0.5
#: Префикс номера, по которому фейк НП узнаёт брошенную ТТН и отвечает по ней
#: неизменным статусом. Связь «кого сид пометил брошенным» и «кто не уходит из
#: выборки» обязана быть явной, иначе воспроизводится не тот профиль.
ABANDONED_PREFIX = "59"
FRESH_PREFIX = "58"
#: Единственный SKU стенда трекинга: измеряется выборка, а не разнообразие корзин.
STOCK_SKU = "SKU-0000"


async def _seed_live(session, *, live: int, stale_days: int) -> tuple[int, int]:
    """Завести живые ТТН: свежие и брошенные. Возвращает (свежих, брошенных).

    Заводит СВОЙ минимальный набор (один клиент, один ФОП), а не зовёт
    `scripts.load.seed`: тому нужны 20 аккаунтов и 10k позиций, а здесь измеряется
    выборка трекинга, которой всё это безразлично. Дублирования нет — общее
    (гарды, фейки) вынесено в модули.
    """
    from app.db.models.enums import ShipmentStatus, UserRole, UserStatus
    from app.db.repositories import (
        ClientAccountRepository,
        SenderProfileRepository,
        ShipmentItemDraft,
        ShipmentRepository,
        UserRepository,
    )

    users = UserRepository(session)
    client = await users.create(
        telegram_id=990_000_001,
        phone="+380999000001",
        full_name="Навантаження",
        role=UserRole.client,
        status=UserStatus.active,
    )
    membership = await ClientAccountRepository(session).get_membership(user_id=client.id)
    account = membership.account
    profile = await SenderProfileRepository(session).create(
        client_id=client.id,
        account_id=account.id,
        name="ФОП",
        np_api_key="np-key",
        np_sender_ref="sender-cp",
        np_contact_ref="sender-ct",
        sender_phone="+380501112233",
        is_default=True,
    )

    # Остаток под списание. Без него первый же `dispatched` упирается в
    # `CHECK (quantity >= 0)`: с переездом записи в Postgres (PR #152) отправка
    # реально двигает остаток, а не пишет в фейковый лист. Берём с запасом на всех.
    from app.db.models.enums import StockMovementType
    from app.db.repositories import StockBalanceRepository

    balances = StockBalanceRepository(session)
    await balances.upsert_meta(account_id=account.id, sku=STOCK_SKU, name="Товар")
    await balances.apply_movement(
        account_id=account.id,
        sku=STOCK_SKU,
        delta=live + 1,
        movement_type=StockMovementType.intake,
        comment="стартовий залишок стенда",
    )
    await session.flush()

    repo = ShipmentRepository(session)
    now = datetime.now(UTC)
    abandoned = int(live * ABANDONED_SHARE)
    for i in range(live):
        is_abandoned = i < abandoned
        # Брошенные: заведены давно и статус с тех пор не менялся — ровно те, что
        # раньше намертво занимали голову очереди. Номер кодирует судьбу документа:
        # фейк НП отвечает по нему детерминированно (см. `NovaPoshtaFake._bucket`),
        # поэтому брошенные из выборки не уходят никогда.
        age = timedelta(days=stale_days - 1) if is_abandoned else timedelta(minutes=i % 600)
        prefix = ABANDONED_PREFIX if is_abandoned else FRESH_PREFIX
        number = f"{prefix}{i:012d}"
        shipment = await repo.create(
            client_id=client.id,
            account_id=account.id,
            sender_profile_id=profile.id,
            recipient_name=f"Отримувач {i}",
            ttn_number=number,
            status=ShipmentStatus.confirmed,
            created_at=now - age,
            status_changed_at=now - age,
            items=[ShipmentItemDraft(sku=STOCK_SKU, name="Товар", quantity=1)],
        )
        # `tracking_updated_at` пуст: ни одну ещё не опрашивали.
        shipment.tracking_updated_at = None
        if i % 500 == 0:
            await session.flush()
    await session.flush()
    return live - abandoned, abandoned


async def _run(*, live: int, passes: int, fast: bool, artifacts: Path) -> None:
    import app.db.models  # noqa: F401
    from app.config import get_settings
    from app.db import base as db_base
    from app.db.base import Base, make_engine
    from app.jobs import poll_tracking_job
    from app.novaposhta.client import NovaPoshtaClient
    from sqlalchemy.ext.asyncio import async_sessionmaker

    settings = get_settings()
    require_load_database(settings.database_url)
    require_offline_stand()

    print("Ефективна конфігурація стенда:")
    for key, value in report_effective_settings(settings).items():
        print(f"  {key} = {value}")
    print()

    engine = make_engine()
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    # `poll_tracking_job` берёт фабрику из модуля — подменяем её, чтобы джоба
    # работала с нашим движком, а не с процесс-глобальным.
    db_base._sessionmaker = factory

    async with factory() as session:
        fresh, abandoned = await _seed_live(
            session, live=live, stale_days=settings.tracking_stale_days
        )
        await session.commit()

    print(f"Живих ТТН: {live} (свіжих {fresh}, покинутих {abandoned})\n")

    # Половина документов отвечает неизменным статусом — это брошенные, и из
    # выборки они не уходят. Без этого тест голодания зелёный по построению.
    np_fake = NovaPoshtaFake(
        # Префикс «59» сид даёт брошенным — они отвечают неизменным статусом и из
        # выборки не уходят. Свежие идут под «58» и уезжают.
        unchanged_prefixes=(ABANDONED_PREFIX,),
        document_seconds=0.0 if fast else NP_DOCUMENT_SECONDS,
        counterparty_seconds=0.0 if fast else NP_COUNTERPARTY_SECONDS,
        reference_seconds=0.0 if fast else NP_REFERENCE_SECONDS,
    )
    np_client = NovaPoshtaClient(settings=settings, transport=np_fake.transport())

    # Списание при отправке — это ЗАПИСЬ в Google на пути `sheets`. Проход,
    # нашедший сотню новых `dispatched`, выдаёт сотни обращений подряд на общий с
    # ботом лимит 60/мин, и они сериализованы single-worker executor'ом.
    meter = GoogleMeter(
        state_dir=artifacts,
        read_seconds=0.0 if fast else SHEETS_READ_SECONDS,
        write_seconds=0.0 if fast else SHEETS_WRITE_SECONDS,
    )
    mutator = QuotaSheetsSource({}, meter=meter)

    report: list[dict] = []
    polled_ever = 0
    for attempt in range(1, passes + 1):
        started = time.perf_counter()
        result = await poll_tracking_job(np_client=np_client, mutator=mutator, settings=settings)
        elapsed = time.perf_counter() - started

        async with factory() as session:
            from app.db.repositories import ShipmentRepository

            stale_before = datetime.now(UTC) - timedelta(days=settings.tracking_stale_days)
            backlog, never, oldest = await ShipmentRepository(session).tracking_backlog(
                stale_before=stale_before
            )
        polled_ever = live - never
        row = {
            "pass": attempt,
            "checked": result.checked,
            "updated": result.updated,
            "backlog": backlog,
            "never_polled": never,
            "polled_ever": polled_ever,
            "oldest_polled_at": oldest.isoformat() if oldest else None,
            "seconds": round(elapsed, 2),
        }
        report.append(row)
        print(
            f"прохід {attempt:>2}: перевірено {result.checked:>4}, "
            f"черга {backlog:>5}, жодного разу не опитано {never:>5}, "
            f"{elapsed:.1f} с"
        )
        if never == 0:
            break

    payload = json.dumps(
        {
            "live": live,
            "fresh": fresh,
            "abandoned": abandoned,
            "fast": fast,
            "tracking_batch_limit": settings.tracking_batch_limit,
            "passes": report,
            "np": np_fake.snapshot(),
            "google": meter.snapshot(),
        },
        ensure_ascii=False,
        indent=1,
    )
    # Файл пишем в потоке: `pathlib` в async-функции ловит ASYNC240, и правило по
    # делу — блокирующий вызов в лупе. Объём мал, но правило нарушать незачем.
    await asyncio.to_thread(_write_report, artifacts, payload)
    meter.close()
    # Вердикт выносит `validate.py`, а не этот скрипт: правило 3 харнесса
    # (`scripts/e2e/README.md`) — персона пишет сырой лог, «зелено/красно» решает
    # одно место, иначе каждый скрипт мерит успех по-своему.
    print(f"\nСирий звіт: {artifacts / 'worker_pass.json'}")
    await np_client.aclose()
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", type=int, default=LIVE_DEFAULT)
    parser.add_argument("--passes", type=int, default=30)
    parser.add_argument(
        "--fast",
        action="store_true",
        help="нулевые задержки: отвечает только на вопрос «голодает ли выборка»",
    )
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=Path(__file__).resolve().parent / "artifacts",
    )
    args = parser.parse_args()
    asyncio.run(_run(live=args.live, passes=args.passes, fast=args.fast, artifacts=args.artifacts))


if __name__ == "__main__":
    main()
