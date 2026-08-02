"""Физическое движение остатка уходит туда, где остаток живёт.

Пока чтение переехало в Postgres, а списание при отправке продолжало писать в лист,
`INVENTORY_SOURCE=pg` был не «медленнее», а **неправильным**, и ломался он молча:

1. зеркало на следующем проходе видело `лист != mirrored_quantity` и трактовало
   штатную отправку как ручную правку человека — движение `manual` и пуш владельцу
   на каждую отправленную ТТН;
2. корзина больше `STOCK_MANUAL_DELTA_LIMIT` — правка отклонялась, зеркало
   возвращало в ячейку прежнее число, и списание не применялось в PG **никогда**:
   остаток оставался завышенным, а гейт от oversell смотрит именно на него;
3. `ttn_dispatch` числится физическим типом, но `record_for_items` пишет его
   заглушками и количество не двигает — инвариант «сумма физических дельт ==
   quantity» расходился на каждой штатной отправке, то есть проверка, которая
   должна ловить баг в нашем коде, начинала кричать на исправную работу.

Поэтому тесты смотрят не на «какой метод позвали», а на наблюдаемый результат:
остаток, журнал и инвариант.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from app.config import get_settings
from app.db.models.enums import ShipmentStatus, StockMovementType, UserRole, UserStatus
from app.db.repositories import (
    SenderProfileRepository,
    ShipmentItemDraft,
    ShipmentRepository,
    StockBalanceRepository,
    StockMovementRepository,
    UserRepository,
)
from app.novaposhta.schemas import TrackingStatus
from app.services import returns
from app.services.tracking import apply_tracking_status
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import account_of


class _RecordingMutator:
    """Фейковый лист. Существует, чтобы поймать обращение к Google на PG-пути:
    его там быть не должно вовсе."""

    def __init__(self) -> None:
        self.calls: list[list[tuple[str, int]]] = []

    def apply_deltas(self, client_key: str, deltas) -> None:
        self.calls.append([(delta.sku, delta.quantity_delta) for delta in deltas])

    def read_stock(self, client_key: str):  # pragma: no cover — на PG-пути не зовётся
        return []


@pytest.fixture
def pg_inventory(monkeypatch):
    """`INVENTORY_SOURCE=pg` на время теста."""
    monkeypatch.setenv("INVENTORY_SOURCE", "pg")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _client_with_profile(session: AsyncSession, telegram_id: int):
    client = await UserRepository(session).create(
        telegram_id=telegram_id,
        full_name=f"Клієнт {telegram_id}",
        role=UserRole.client,
        status=UserStatus.active,
    )
    account = await account_of(session, client)
    await SenderProfileRepository(session).create(
        client_id=client.id,
        account_id=account.id,
        name="ФОП",
        np_api_key="np-key",
        np_sender_ref="sender",
        np_contact_ref="contact",
        sender_phone="+380501112233",
        is_default=True,
    )
    return client, account


async def _stock(session: AsyncSession, account_id, *, sku: str, quantity: int) -> None:
    """Стартовый остаток — движением `intake`, а не присваиванием.

    Присваивание в обход `apply_movement` нарушило бы инвариант журнала ещё до
    теста, и проверка инварианта в конце ничего бы не значила.
    """
    await StockBalanceRepository(session).apply_movement(
        account_id=account_id,
        sku=sku,
        delta=quantity,
        movement_type=StockMovementType.intake,
        comment="стартовий залишок",
    )
    await session.flush()


async def test_dispatch_writes_to_postgres_not_to_the_sheet(db_session: AsyncSession, pg_inventory):
    """Отправка при `pg`: остаток уменьшился в БД, в Google не ходили.

    Мутация, которую тест обязан ловить: вернуть `apply_deltas` в
    `_apply_dispatch_stock`. Тогда `mutator.calls` непуст, `quantity` остаётся 10, а
    журнал расходится с остатком.
    """
    client, account = await _client_with_profile(db_session, 4100)
    await _stock(db_session, account.id, sku="SKU-1", quantity=10)

    created = await ShipmentRepository(db_session).create(
        client_id=client.id,
        account_id=account.id,
        recipient_name="Іван",
        ttn_number="59000111",
        status=ShipmentStatus.confirmed,
        items=[ShipmentItemDraft(sku="SKU-1", name="Кава", quantity=3, unit_price=Decimal("100"))],
    )
    await db_session.flush()
    shipment = await ShipmentRepository(db_session).get_by_id(created.id)

    mutator = _RecordingMutator()
    scanned = (datetime.now(UTC) - timedelta(minutes=5)).astimezone(ZoneInfo("Europe/Kyiv"))
    changed, _ = await apply_tracking_status(
        db_session,
        shipment=shipment,
        tracking=TrackingStatus(
            number="59000111",
            status="Відправлено",
            status_code="3",
            raw={"DateScan": scanned.strftime("%d.%m.%Y %H:%M:%S")},
        ),
        mutator=mutator,
    )
    await db_session.flush()

    assert changed is True
    assert mutator.calls == [], "на PG-пути списання не сміє ходити в Google"

    balance = await StockBalanceRepository(db_session).get(account_id=account.id, sku="SKU-1")
    assert balance.quantity == 7, "остаток обязан уменьшиться в Postgres"

    movements = await StockMovementRepository(db_session).list_for_shipment(shipment.id)
    dispatches = [m for m in movements if m.movement_type is StockMovementType.ttn_dispatch]
    assert len(dispatches) == 1, "ровно одно движение отправки, а не два"
    assert (dispatches[0].quantity_before, dispatches[0].quantity_after) == (10, 7), (
        "before/after обязаны быть честными: по заглушкам 0/-3 историю не восстановить"
    )
    assert not [m for m in movements if m.movement_type is StockMovementType.manual], (
        "штатная отправка не должна выглядеть как ручная правка человека"
    )

    drift = await StockBalanceRepository(db_session).ledger_matches_balance(account.id)
    assert drift == [], f"инвариант журнала разошёлся: {drift}"


async def test_dispatch_still_writes_to_the_sheet_on_the_sheets_path(db_session: AsyncSession):
    """Путь `sheets` не изменился — это сеть безопасности отката.

    `INVENTORY_SOURCE` по умолчанию `sheets`, поэтому фикстуру не ставим.
    """
    client, account = await _client_with_profile(db_session, 4101)
    created = await ShipmentRepository(db_session).create(
        client_id=client.id,
        account_id=account.id,
        recipient_name="Іван",
        ttn_number="59000222",
        status=ShipmentStatus.confirmed,
        items=[ShipmentItemDraft(sku="SKU-1", name="Кава", quantity=2, unit_price=Decimal("100"))],
    )
    await db_session.flush()
    shipment = await ShipmentRepository(db_session).get_by_id(created.id)

    mutator = _RecordingMutator()
    scanned = (datetime.now(UTC) - timedelta(minutes=5)).astimezone(ZoneInfo("Europe/Kyiv"))
    await apply_tracking_status(
        db_session,
        shipment=shipment,
        tracking=TrackingStatus(
            number="59000222",
            status="Відправлено",
            status_code="3",
            raw={"DateScan": scanned.strftime("%d.%m.%Y %H:%M:%S")},
        ),
        mutator=mutator,
    )

    assert mutator.calls == [[("SKU-1", -2)]]


async def test_return_restocks_postgres(db_session: AsyncSession, pg_inventory):
    """Приём возврата при `pg` возвращает единицы в `stock_balances`.

    Возврат физически приезжает на склад, и если он не доедет до остатка, товар
    станет непродаваемым — ошибка в ту же сторону, что и зависшая бронь, только
    навсегда.
    """
    client, account = await _client_with_profile(db_session, 4102)
    await _stock(db_session, account.id, sku="SKU-1", quantity=4)

    created = await ShipmentRepository(db_session).create(
        client_id=client.id,
        account_id=account.id,
        recipient_name="Іван",
        ttn_number="59000333",
        status=ShipmentStatus.returning,
        items=[ShipmentItemDraft(sku="SKU-1", name="Кава", quantity=3, unit_price=Decimal("100"))],
    )
    await db_session.flush()

    mutator = _RecordingMutator()
    await returns.receive_returned_shipment(
        db_session,
        shipment_id=created.id,
        actor_user_id=client.id,
        decisions=[returns.ReturnDecision(sku="SKU-1", accepted_quantity=2, rejected_quantity=1)],
        mutator=mutator,
    )
    await db_session.flush()

    assert mutator.calls == [], "на PG-пути возврат не сміє ходити в Google"
    balance = await StockBalanceRepository(db_session).get(account_id=account.id, sku="SKU-1")
    assert balance.quantity == 6, "принято 2 из 3 — остаток 4 + 2"

    drift = await StockBalanceRepository(db_session).ledger_matches_balance(account.id)
    assert drift == [], f"инвариант журнала разошёлся: {drift}"
