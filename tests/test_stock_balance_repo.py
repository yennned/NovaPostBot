"""Остаток в Postgres: честные before/after, лок и запрет ухода в минус."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from app.db.models.enums import ShipmentStatus, StockMovementType, UserRole, UserStatus
from app.db.repositories import (
    ShipmentItemDraft,
    ShipmentRepository,
    StockBalanceRepository,
    StockMovementRepository,
    UserRepository,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import account_of


async def _account(session: AsyncSession, telegram_id: int) -> uuid.UUID:
    """Аккаунт заводится сам при создании клиента (`UserRepository.create`)."""
    user = await UserRepository(session).create(
        telegram_id=telegram_id,
        full_name=f"Клієнт {telegram_id}",
        role=UserRole.client,
        status=UserStatus.active,
    )
    account = await account_of(session, user)
    return account.id


async def test_apply_movement_records_real_before_and_after(db_session: AsyncSession):
    """Журнал движений должен быть восстановимой историей, а не заглушками.

    Раньше `record_for_items` писал `quantity_before=0, quantity_after=delta` для
    всех типов сразу: по такому журналу нельзя ни восстановить остаток, ни
    проверить его — то есть аудита фактически не было.
    """
    account_id = await _account(db_session, 1100)
    repo = StockBalanceRepository(db_session)

    first = await repo.apply_movement(
        account_id=account_id, sku="SKU-1", delta=10, movement_type=StockMovementType.intake
    )
    second = await repo.apply_movement(
        account_id=account_id, sku="SKU-1", delta=-4, movement_type=StockMovementType.ttn_dispatch
    )
    third = await repo.apply_movement(
        account_id=account_id, sku="SKU-1", delta=2, movement_type=StockMovementType.ttn_return
    )

    assert (first.quantity_before, first.quantity_after) == (0, 10)
    assert (second.quantity_before, second.quantity_after) == (10, 6)
    assert (third.quantity_before, third.quantity_after) == (6, 8)
    # Цепочка связна: конец каждого движения — начало следующего.
    assert first.quantity_after == second.quantity_before
    assert second.quantity_after == third.quantity_before

    balance = await repo.get(account_id=account_id, sku="SKU-1")
    assert balance is not None and balance.quantity == 8


async def test_reserve_movement_does_not_move_quantity(db_session: AsyncSession):
    """Бронь держит статус ТТН, а не остаток — количество она двигать не должна."""
    account_id = await _account(db_session, 1101)
    repo = StockBalanceRepository(db_session)
    await repo.apply_movement(
        account_id=account_id, sku="SKU-1", delta=5, movement_type=StockMovementType.intake
    )

    reserve = await repo.apply_movement(
        account_id=account_id, sku="SKU-1", delta=-3, movement_type=StockMovementType.ttn_reserve
    )

    assert reserve.quantity_before == reserve.quantity_after == 5
    balance = await repo.get(account_id=account_id, sku="SKU-1")
    assert balance is not None and balance.quantity == 5


async def test_quantity_cannot_go_negative(db_session: AsyncSession):
    """Последний рубеж под гейтом от oversell: БД откажет продать то, чего нет."""
    account_id = await _account(db_session, 1102)
    repo = StockBalanceRepository(db_session)
    await repo.apply_movement(
        account_id=account_id, sku="SKU-1", delta=2, movement_type=StockMovementType.intake
    )

    with pytest.raises(IntegrityError):
        await repo.apply_movement(
            account_id=account_id,
            sku="SKU-1",
            delta=-5,
            movement_type=StockMovementType.ttn_dispatch,
        )
        await db_session.flush()


async def test_lock_for_update_orders_by_sku(db_session: AsyncSession):
    """`ORDER BY sku` при `FOR UPDATE` — защита от дедлока на пересекающихся корзинах.

    Два сабмита с корзинами `[A, B]` и `[B, A]` без сортировки берут строки в
    разном порядке и получают `DeadlockDetected`. Пиним намерение в SQL, чтобы
    защита не держалась на одном лишь тесте гонки.
    """
    repo = StockBalanceRepository(db_session)

    # Компилируем ТОТ ЖЕ запрос, что строит репозиторий. Первая версия теста
    # собирала копию запроса руками и потому не проверяла ничего: мутация
    # «убрать ORDER BY» её не валила, хотя защита была снята.
    compiled = str(StockBalanceRepository.lock_stmt(uuid.uuid4(), ["B", "A"]).compile())
    assert "FOR UPDATE" in compiled
    assert "ORDER BY stock_balances.sku" in compiled

    account_id = await _account(db_session, 1103)
    await repo.apply_movement(
        account_id=account_id, sku="B", delta=1, movement_type=StockMovementType.intake
    )
    await repo.apply_movement(
        account_id=account_id, sku="A", delta=1, movement_type=StockMovementType.intake
    )
    locked = await repo.lock_for_update(account_id=account_id, skus=["B", "A"])
    assert list(locked) == ["A", "B"], "строки обязаны отдаваться в порядке SKU"


async def test_upsert_meta_does_not_touch_quantity(db_session: AsyncSession):
    """Описательными полями владеет лист — но количество из него не приходит."""
    account_id = await _account(db_session, 1104)
    repo = StockBalanceRepository(db_session)
    await repo.apply_movement(
        account_id=account_id, sku="SKU-1", delta=7, movement_type=StockMovementType.intake
    )

    await repo.upsert_meta(
        account_id=account_id,
        sku="SKU-1",
        name="Кава мелена",
        category="Напої",
        price=Decimal("120.50"),
    )

    balance = await repo.get(account_id=account_id, sku="SKU-1")
    assert balance is not None
    assert (balance.name, balance.category, balance.price) == (
        "Кава мелена",
        "Напої",
        Decimal("120.50"),
    )
    assert balance.quantity == 7


async def test_ledger_invariant_catches_drift(db_session: AsyncSession):
    """Сумма физических дельт обязана сходиться с остатком."""
    account_id = await _account(db_session, 1105)
    repo = StockBalanceRepository(db_session)
    await repo.apply_movement(
        account_id=account_id, sku="SKU-1", delta=10, movement_type=StockMovementType.intake
    )
    assert await repo.ledger_matches_balance(account_id) == []

    # Правим остаток мимо `apply_movement` — ровно то, что джоба сверки должна ловить.
    balance = await repo.get(account_id=account_id, sku="SKU-1")
    assert balance is not None
    balance.quantity = 42
    await db_session.flush()

    drift = await repo.ledger_matches_balance(account_id)
    assert drift == [("SKU-1", 10, 42)]


async def _shipment_with_reserve(
    session: AsyncSession,
    *,
    telegram_id: int,
    ttn: str,
    status: ShipmentStatus,
    extra: StockMovementType | None = None,
):
    """ТТН с бронью в журнале; `extra` — движение, снимающее её."""
    user = await UserRepository(session).create(
        telegram_id=telegram_id,
        full_name=f"Клієнт {telegram_id}",
        role=UserRole.client,
        status=UserStatus.active,
    )
    account = await account_of(session, user)
    shipments = ShipmentRepository(session)
    created = await shipments.create(
        client_id=user.id,
        recipient_name="Іван",
        ttn_number=ttn,
        status=status,
        items=[ShipmentItemDraft(sku="SKU-1", name="Кава", quantity=3, unit_price=Decimal("100"))],
    )
    shipment = await shipments.get_by_id(created.id)
    movements = StockMovementRepository(session)
    await movements.record_for_items(
        client_id=user.id,
        account_id=account.id,
        shipment_id=shipment.id,
        actor_user_id=user.id,
        items=shipment.items,
        movement_type=StockMovementType.ttn_reserve,
        sign=-1,
        comment="Резерв",
    )
    if extra is not None:
        await movements.record_for_items(
            client_id=user.id,
            account_id=account.id,
            shipment_id=shipment.id,
            actor_user_id=user.id,
            items=shipment.items,
            movement_type=extra,
            sign=1 if extra is StockMovementType.ttn_cancel else -1,
            comment="Знято",
        )
    await session.flush()
    return account.id, created.id


async def test_unreleased_reserve_found_only_when_ttn_is_closed(db_session: AsyncSession):
    """Бронь под закрытой ТТН — дефект; под открытой — норма.

    `ledger_matches_balance` этого не видит в принципе: `ttn_reserve` не входит в
    физические типы, поэтому «сумма дельт == остаток» держится и при висячей брони.
    Отсутствие такой проверки и дало дефекту «ТТН удалили в кабинете НП» прожить
    полтора года — нашёлся он живым прогоном, а не сверкой.

    Мутация: убрать условие `Shipment.status.not_in(RESERVING_STATUSES)` — открытая
    ТТН попадёт в выборку, и второй assert покраснеет.
    """
    repo = StockBalanceRepository(db_session)

    open_account, _ = await _shipment_with_reserve(
        db_session, telegram_id=1110, ttn="59001110", status=ShipmentStatus.confirmed
    )
    assert await repo.unreleased_reserves(open_account) == []

    broken_account, broken_id = await _shipment_with_reserve(
        db_session, telegram_id=1111, ttn="59001111", status=ShipmentStatus.cancelled
    )
    assert await repo.unreleased_reserves(broken_account) == [(broken_id, "59001111", -3)]


@pytest.mark.parametrize(
    "released_by", [StockMovementType.ttn_cancel, StockMovementType.ttn_dispatch]
)
async def test_unreleased_reserve_silent_when_reserve_was_released(
    db_session: AsyncSession, released_by: StockMovementType
):
    """Бронь снята отменой или отправкой — жаловаться не на что.

    `ttn_dispatch` считается снятием намеренно: отправка выводит ТТН из
    `RESERVING_STATUSES`, бронь исчезает вместе со статусом, и парного `ttn_cancel`
    в штатном пути нет. Без этой ветки сверка кричала бы на каждой отправленной ТТН.

    Мутация: убрать `ttn_dispatch` из `released_types` — параметризованный случай
    покраснеет.
    """
    telegram_id = 1120 + list(StockMovementType).index(released_by)
    account_id, _ = await _shipment_with_reserve(
        db_session,
        telegram_id=telegram_id,
        ttn=f"5900{telegram_id}",
        status=ShipmentStatus.dispatched,
        extra=released_by,
    )
    assert await StockBalanceRepository(db_session).unreleased_reserves(account_id) == []
