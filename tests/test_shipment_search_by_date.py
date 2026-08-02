"""Поиск отправлений по дате — по киевскому дню и по индексу.

Было `cast(created_at, Date) == :d`. Две беды сразу, и обе тихие: выражение по
колонке не sargable (индекс неприменим, читается вся история аккаунта), а `cast`
приводит timestamptz к дате в часовом поясе СЕССИИ — то есть в UTC. ТТН, созданная
1 августа в 00:30 по Киеву, лежит в UTC как 31 июля 21:30 и по запросу «01.08» не
находилась вовсе.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.db.models.enums import ShipmentStatus, UserRole, UserStatus
from app.db.repositories import ShipmentItemDraft, ShipmentRepository, UserRepository
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import account_of

_KYIV = ZoneInfo("Europe/Kyiv")


async def _client(session: AsyncSession, telegram_id: int):
    client = await UserRepository(session).create(
        telegram_id=telegram_id,
        full_name="Клієнт",
        role=UserRole.client,
        status=UserStatus.active,
    )
    return client, await account_of(session, client)


async def _shipment(session: AsyncSession, client, account, *, number: str, created_at: datetime):
    shipment = await ShipmentRepository(session).create(
        client_id=client.id,
        account_id=account.id,
        recipient_name="Іван",
        ttn_number=number,
        status=ShipmentStatus.created,
        created_at=created_at,
        items=[ShipmentItemDraft(sku="A", name="Кава", quantity=1)],
    )
    await session.flush()
    return shipment


async def test_search_by_date_uses_the_kyiv_day(db_session: AsyncSession):
    """Ночная ТТН ищется по своей киевской дате, а не по UTC-дате.

    00:30 первого числа по Киеву — это 21:30 предыдущего дня по UTC. Пока сравнение
    шло `cast`-ом, такая накладная по своему дню не находилась, а находилась по
    вчерашнему — то есть менеджер искал её и не видел.
    """
    client, account = await _client(db_session, 2000)
    await _shipment(
        db_session,
        client,
        account,
        number="59000001",
        created_at=datetime(2026, 8, 1, 0, 30, tzinfo=_KYIV),
    )
    await _shipment(
        db_session,
        client,
        account,
        number="59000002",
        created_at=datetime(2026, 7, 31, 23, 30, tzinfo=_KYIV),
    )

    found, _ = await ShipmentRepository(db_session).get_by_account_and_status(
        account.id, query="01.08.2026", limit=50
    )
    assert [s.ttn_number for s in found] == ["59000001"]

    previous, _ = await ShipmentRepository(db_session).get_by_account_and_status(
        account.id, query="31.07.2026", limit=50
    )
    assert [s.ttn_number for s in previous] == ["59000002"]


async def test_search_by_date_is_a_range_not_a_cast(db_session: AsyncSession):
    """Запрос обязан сравнивать саму колонку, а не выражение по ней.

    `cast(created_at, Date) = :d` не sargable: индекс `(account_id, created_at)`
    к нему неприменим, и поиск по дате читает всю историю аккаунта целиком. На
    15 000 ТТН/мес это уже секунды на каждый запрос.
    """
    from app.db.repositories.shipment import _shipment_search_filters

    compiled = " ".join(str(f.compile()) for f in _shipment_search_filters("01.08.2026"))
    assert "CAST" not in compiled.upper(), "выражение по колонке убивает индекс"
    assert "created_at >=" in compiled and "created_at <" in compiled
