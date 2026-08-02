"""Гейт от oversell: брони остатка под локом.

Здесь есть один тест, ради которого написана вся эта таблица, — гонка двух
одновременных сабмитов одного аккаунта. Он идёт **по-настоящему**: две отдельные
сессии на двух коннектах, обе коммитят. Через общий `db_session` его поставить
нельзя — там одна транзакция, а вся защита именно в том, что второй коннект
видит бронь первого.

Побочный эффект: этот тест пишет в базу вне пер-тестовой транзакции, поэтому
чистит за собой сам (CASCADE от аккаунта).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.models.client_account import ClientAccount
from app.db.models.enums import ShipmentStatus, StockMovementType, UserRole, UserStatus
from app.db.models.user import User
from app.db.repositories import (
    InsufficientAvailable,
    ShipmentItemDraft,
    ShipmentRepository,
    StockBalanceRepository,
    StockHoldRepository,
    UserRepository,
    available_from,
)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tests.conftest import account_of


async def _account(session: AsyncSession, telegram_id: int):
    user = await UserRepository(session).create(
        telegram_id=telegram_id,
        full_name=f"Клієнт {telegram_id}",
        role=UserRole.client,
        status=UserStatus.active,
    )
    return user, await account_of(session, user)


async def _stock(session: AsyncSession, account_id: uuid.UUID, sku: str, quantity: int) -> None:
    await StockBalanceRepository(session).apply_movement(
        account_id=account_id, sku=sku, delta=quantity, movement_type=StockMovementType.intake
    )


def test_available_formula_is_one_place():
    """Экран, гейт и сверка обязаны считать доступное одинаково."""
    assert available_from(quantity=10, reserved=3, held=2) == 5
    assert available_from(quantity=10, reserved=8, held=5) == 0, "ниже нуля не опускаемся"


async def test_hold_reserves_and_lowers_available(db_session: AsyncSession):
    _, account = await _account(db_session, 1600)
    await _stock(db_session, account.id, "A", 10)
    holds = StockHoldRepository(db_session)

    await holds.hold(
        account_id=account.id,
        client_id=None,
        submit_key="s1",
        wanted={"A": 4},
        reserved={},
        ttl_seconds=300,
    )

    assert await holds.active_by_sku(account.id) == {"A": 4}


async def test_hold_refuses_more_than_available(db_session: AsyncSession):
    """Отказ считает и бронь, и резерв под живыми ТТН — иначе оба слоя не сложатся."""
    _, account = await _account(db_session, 1601)
    await _stock(db_session, account.id, "A", 10)
    holds = StockHoldRepository(db_session)
    await holds.hold(
        account_id=account.id,
        client_id=None,
        submit_key="s1",
        wanted={"A": 4},
        reserved={},
        ttl_seconds=300,
    )

    with pytest.raises(InsufficientAvailable) as exc:
        await holds.hold(
            account_id=account.id,
            client_id=None,
            submit_key="s2",
            wanted={"A": 5},
            reserved={"A": 3},  # резерв под уже созданными ТТН
            ttl_seconds=300,
        )
    assert (exc.value.sku, exc.value.requested, exc.value.available) == ("A", 5, 3)


async def test_repeat_submit_key_does_not_hold_twice(db_session: AsyncSession):
    """Двойной тап не должен резервировать корзину дважды.

    Апдейты обрабатываются параллельными задачами и без `events_isolation`, так
    что два нажатия «Відправити» — это норма, а не экзотика.
    """
    _, account = await _account(db_session, 1602)
    await _stock(db_session, account.id, "A", 10)
    holds = StockHoldRepository(db_session)

    first = await holds.hold(
        account_id=account.id,
        client_id=None,
        submit_key="same",
        wanted={"A": 6},
        reserved={},
        ttl_seconds=300,
    )
    second = await holds.hold(
        account_id=account.id,
        client_id=None,
        submit_key="same",
        wanted={"A": 6},
        reserved={},
        ttl_seconds=300,
    )

    assert {h.id for h in first} == {h.id for h in second}
    assert await holds.active_by_sku(account.id) == {"A": 6}


async def test_attach_releases_hold_so_reservation_is_not_counted_twice(
    db_session: AsyncSession,
):
    """После создания ТТН остаток держит её статус — бронь обязана уйти.

    Оставь её активной, и то же количество вычлось бы дважды: один раз бронью,
    второй — резервом под ТТН. Клиент увидел бы вдвое меньший доступный остаток.
    """
    client, account = await _account(db_session, 1603)
    await _stock(db_session, account.id, "A", 10)
    holds = StockHoldRepository(db_session)
    await holds.hold(
        account_id=account.id,
        client_id=None,
        submit_key="s1",
        wanted={"A": 4},
        reserved={},
        ttl_seconds=300,
    )

    shipment = await ShipmentRepository(db_session).create(
        client_id=client.id,
        account_id=account.id,
        recipient_name="Іван",
        status=ShipmentStatus.created,
        items=[ShipmentItemDraft(sku="A", name="Кава", quantity=4)],
    )
    shipment_id = shipment.id
    released = await holds.attach("s1", shipment_id=shipment_id)

    assert released == 1
    assert await holds.active_by_sku(account.id) == {}
    rows = await holds.by_submit_key("s1")
    # Связь с ТТН остаётся: по ней разбирают, откуда взялась бронь.
    assert [r.shipment_id for r in rows] == [shipment_id]


async def test_release_frees_stock_after_np_failure(db_session: AsyncSession):
    """НП отказала — бронь снимается, товар снова продаётся."""
    _, account = await _account(db_session, 1604)
    await _stock(db_session, account.id, "A", 10)
    holds = StockHoldRepository(db_session)
    await holds.hold(
        account_id=account.id,
        client_id=None,
        submit_key="s1",
        wanted={"A": 10},
        reserved={},
        ttl_seconds=300,
    )

    assert await holds.release("s1") == 1
    await holds.hold(
        account_id=account.id,
        client_id=None,
        submit_key="s2",
        wanted={"A": 10},
        reserved={},
        ttl_seconds=300,
    )
    assert await holds.active_by_sku(account.id) == {"A": 10}


async def test_sweeper_frees_holds_left_by_a_crashed_process(db_session: AsyncSession):
    """Процесс упал между фазами — бронь не должна висеть вечно.

    Заниженный `available` означает, что клиент не может продать собственный
    товар. Это та сторона ошибки, которую можно вычищать фоном; oversell так
    вычистить нельзя, поэтому TTL короткий и дворник обязателен.
    """
    _, account = await _account(db_session, 1605)
    await _stock(db_session, account.id, "A", 10)
    holds = StockHoldRepository(db_session)
    created = await holds.hold(
        account_id=account.id,
        client_id=None,
        submit_key="s1",
        wanted={"A": 10},
        reserved={},
        ttl_seconds=300,
    )
    created[0].expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()

    assert await holds.sweep_expired() == 1
    assert await holds.active_by_sku(account.id) == {}


async def test_sweeper_does_not_touch_live_holds(db_session: AsyncSession):
    """Иначе дворник сам открыл бы окно oversell на каждом проходе."""
    _, account = await _account(db_session, 1606)
    await _stock(db_session, account.id, "A", 10)
    holds = StockHoldRepository(db_session)
    await holds.hold(
        account_id=account.id,
        client_id=None,
        submit_key="s1",
        wanted={"A": 10},
        reserved={},
        ttl_seconds=300,
    )

    assert await holds.sweep_expired() == 0
    assert await holds.active_by_sku(account.id) == {"A": 10}


async def test_two_concurrent_submits_only_one_passes(engine: AsyncEngine):
    """Гонка, ради которой всё и делалось: остаток 10, две корзины по 8.

    Сегодня (лист как источник правды) проходят **обе**: `_resolve_items` читает
    снимок, потом уходит в НП на 2,5 секунды, и только затем пишет резерв — оба
    сабмита видят один и тот же остаток.

    Здесь проверка и захват брони — одна операция под `FOR UPDATE` по строкам
    остатка, поэтому второй коннект ждёт первого и видит его бронь.

    Тест ходит двумя настоящими коннектами и коммитит: через общий `db_session`
    он был бы бессмысленным (одна транзакция сама с собой не конкурирует).
    Поэтому за собой он убирает вручную.

    **Почему окно задаётся явно.** Первая версия просто пускала два `hold()` через
    `asyncio.gather` — и зеленела со снятым `FOR UPDATE`: корутины успевали
    отработать почти последовательно, второй коннект видел уже закоммиченную бронь
    первого, и лок был ни при чём. Здесь первый сабмит берёт лок и держит его
    заведомо дольше, чем второму нужно на запрос, поэтому без лока второй читает
    остаток 10 и тоже проходит — мутация «убрать `FOR UPDATE`» тест валит.
    """
    account_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    try:
        async with AsyncSession(engine, expire_on_commit=False) as setup:
            user, account = await _account(setup, 1699)
            await _stock(setup, account.id, "A", 10)
            await setup.commit()
            account_id, user_id = account.id, user.id

        # Обе транзакции обязаны начаться до того, как хоть одна возьмёт строку.
        started = asyncio.Barrier(2)
        first_locked = asyncio.Event()

        async def submit(key: str, *, lock_window: float, wait_for_lock: bool) -> str:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                await session.execute(select(1))
                await started.wait()
                if wait_for_lock:
                    await first_locked.wait()
                try:
                    await StockHoldRepository(session).hold(
                        account_id=account_id,
                        client_id=None,
                        submit_key=key,
                        wanted={"A": 8},
                        reserved={},
                        ttl_seconds=300,
                    )
                except InsufficientAvailable:
                    await session.rollback()
                    return "refused"
                first_locked.set()
                # Держим лок дольше, чем второму нужно на свой запрос: без этого
                # окна тест зеленел бы и на снятом `FOR UPDATE`.
                await asyncio.sleep(lock_window)
                await session.commit()
                return "held"

        results = await asyncio.wait_for(
            asyncio.gather(
                submit("a", lock_window=0.5, wait_for_lock=False),
                submit("b", lock_window=0.0, wait_for_lock=True),
            ),
            timeout=30,
        )

        assert results == ["held", "refused"], (
            f"первый сабмит держит лок — второй обязан получить отказ, получено {results}"
        )
        async with AsyncSession(engine, expire_on_commit=False) as check:
            assert await StockHoldRepository(check).active_by_sku(account_id) == {"A": 8}
    finally:
        async with AsyncSession(engine, expire_on_commit=False) as cleanup:
            if account_id is not None:
                await cleanup.execute(delete(ClientAccount).where(ClientAccount.id == account_id))
            if user_id is not None:
                await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.commit()
