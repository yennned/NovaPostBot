"""Область трекинга: кого опрашиваем, в каком порядке и что делаем с молчанием НП.

Ходит через настоящий `NovaPoshtaClient` на `httpx.MockTransport` — без сети, но с
настоящим разбором конверта НП, как в остальных тестах НП.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
from app.config import Settings
from app.db.models.enums import ShipmentStatus, UserRole, UserStatus
from app.db.repositories import SenderProfileRepository, ShipmentRepository, UserRepository
from app.db.repositories.shipment import ShipmentItemDraft
from app.novaposhta.client import NovaPoshtaClient
from app.services.tracking import poll_returns, poll_shipments
from sqlalchemy.ext.asyncio import AsyncSession


class _NpStub:
    """Отвечает статусом на любой запрошенный номер; помнит, о чём спрашивали."""

    def __init__(self, *, status: str = "Прийнято", status_code: str = "1") -> None:
        self.asked: list[list[str]] = []
        self.calls = 0
        self._status = status
        self._status_code = status_code
        #: номера, про которые НП сознательно промолчит (не вернёт строку)
        self.silent: set[str] = set()

    def client(self, settings: Settings) -> NovaPoshtaClient:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            numbers = [doc["DocumentNumber"] for doc in payload["methodProperties"]["Documents"]]
            self.asked.append(numbers)
            self.calls += 1
            data = [
                {"Number": number, "Status": self._status, "StatusCode": self._status_code}
                for number in numbers
                if number not in self.silent
            ]
            return httpx.Response(
                200, json={"success": True, "data": data, "errors": [], "errorCodes": []}
            )

        return NovaPoshtaClient(settings=settings, transport=httpx.MockTransport(handler))

    @property
    def asked_numbers(self) -> set[str]:
        return {number for chunk in self.asked for number in chunk}


def _settings(**overrides) -> Settings:
    base = Settings(_env_file=None)
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


async def _client_with_profile(session: AsyncSession, telegram_id: int):
    """Клиент с ФОП. ФОП обязателен: обе выборки трекинга требуют `sender_profile_id`
    — без ключа НП спрашивать не у кого."""
    user = await UserRepository(session).create(
        telegram_id=telegram_id,
        full_name=f"Клієнт {telegram_id}",
        role=UserRole.client,
        status=UserStatus.active,
    )
    profile = await SenderProfileRepository(session).create(
        client_id=user.id,
        name="ФОП",
        np_api_key="np-key",
        np_sender_ref="sender",
        np_contact_ref="contact",
        sender_phone="+380501112233",
        is_default=True,
    )
    return user, profile


async def _shipment(
    session: AsyncSession,
    client,
    profile,
    *,
    number: str,
    status: ShipmentStatus,
    created_at: datetime | None = None,
    status_changed_at: datetime | None = None,
    tracking_updated_at: datetime | None = None,
    dispatched_at: datetime | None = None,
):
    shipment = await ShipmentRepository(session).create(
        client_id=client.id,
        sender_profile_id=profile.id,
        recipient_name="Іван",
        ttn_number=number,
        status=status,
        created_at=created_at,
        status_changed_at=status_changed_at,
        items=[ShipmentItemDraft(sku="SKU-1", name="Кава", quantity=1)],
    )
    shipment.tracking_updated_at = tracking_updated_at
    if dispatched_at is not None:
        shipment.dispatched_at = dispatched_at
    await session.flush()
    return shipment


async def test_abandoned_shipments_do_not_starve_the_queue(db_session: AsyncSession):
    """Брошенные ТТН не должны вытеснять живые из лимита выборки.

    Ровно этот дефект и жил в проде: выборка сортировалась по `status_changed_at`,
    который двигается только при СМЕНЕ статуса, поэтому документы с неменяющимся
    статусом занимали слоты навсегда, и новые ТТН не опрашивались вовсе.
    """
    now = datetime.now(UTC)
    client, profile = await _client_with_profile(db_session, 950)
    long_ago = now - timedelta(days=5)
    # Пять «старожилов»: статус не менялся давно, но их уже опрашивали.
    for index in range(5):
        await _shipment(
            db_session,
            client,
            profile,
            number=f"old-{index}",
            status=ShipmentStatus.confirmed,
            status_changed_at=long_ago,
            tracking_updated_at=now - timedelta(minutes=1),
        )
    # Две свежие: статус поменялся только что, но их не спрашивали ни разу.
    for index in range(2):
        await _shipment(
            db_session,
            client,
            profile,
            number=f"new-{index}",
            status=ShipmentStatus.created,
            status_changed_at=now,
            tracking_updated_at=None,
        )

    stub = _NpStub()
    np_client = stub.client(_settings())
    try:
        await poll_shipments(
            db_session, np_client=np_client, settings=_settings(tracking_batch_limit=2)
        )
    finally:
        await np_client.aclose()

    # При сортировке по status_changed_at в лимит попали бы пятеро старых.
    assert stub.asked_numbers == {"new-0", "new-1"}


async def test_unanswered_shipment_is_stamped_and_yields_its_slot(db_session: AsyncSession):
    """НП промолчала о документе — отметку времени всё равно ставим.

    Иначе документ навсегда остаётся «самым давно не опрошенным», вечно занимает
    начало очереди и вытесняет остальных — то же голодание, только с другого конца.
    """
    client, profile = await _client_with_profile(db_session, 951)
    silent = await _shipment(
        db_session, client, profile, number="ghost-1", status=ShipmentStatus.confirmed
    )
    other = await _shipment(
        db_session, client, profile, number="live-1", status=ShipmentStatus.confirmed
    )

    stub = _NpStub()
    stub.silent = {"ghost-1"}
    np_client = stub.client(_settings())
    try:
        await poll_shipments(db_session, np_client=np_client, settings=_settings())
    finally:
        await np_client.aclose()

    assert silent.tracking_updated_at is not None, "молчание НП обязано двигать отметку"
    assert other.tracking_updated_at is not None

    # Следующий проход с лимитом 1 должен взять кого-то одного, а не зациклиться
    # на «призраке»: обе отметки уже проставлены, значит очередь движется.
    stub2 = _NpStub()
    stub2.silent = {"ghost-1"}
    np_client = stub2.client(_settings())
    try:
        await poll_shipments(
            db_session, np_client=np_client, settings=_settings(tracking_batch_limit=1)
        )
    finally:
        await np_client.aclose()
    assert len(stub2.asked_numbers) == 1


async def test_stale_shipments_leave_the_tracking_set(db_session: AsyncSession):
    client, profile = await _client_with_profile(db_session, 952)
    now = datetime.now(UTC)
    await _shipment(
        db_session,
        client,
        profile,
        number="forgotten",
        status=ShipmentStatus.confirmed,
        created_at=now - timedelta(days=30),
    )
    await _shipment(db_session, client, profile, number="fresh", status=ShipmentStatus.confirmed)

    stub = _NpStub()
    np_client = stub.client(_settings())
    try:
        await poll_shipments(
            db_session, np_client=np_client, settings=_settings(tracking_stale_days=14)
        )
    finally:
        await np_client.aclose()

    assert stub.asked_numbers == {"fresh"}


async def test_dispatched_left_out_of_hot_tracking(db_session: AsyncSession):
    """После `dispatched` горячий трекинг документ больше не трогает."""
    client, profile = await _client_with_profile(db_session, 953)
    await _shipment(
        db_session,
        client,
        profile,
        number="gone",
        status=ShipmentStatus.dispatched,
        dispatched_at=datetime.now(UTC),
    )

    stub = _NpStub()
    np_client = stub.client(_settings())
    try:
        result = await poll_shipments(db_session, np_client=np_client, settings=_settings())
    finally:
        await np_client.aclose()

    assert stub.calls == 0
    assert result.checked == 0


async def test_return_watch_picks_only_the_window(db_session: AsyncSession):
    """Поздний проход берёт только тех, кого НП ещё может развернуть обратно."""
    client, profile = await _client_with_profile(db_session, 954)
    now = datetime.now(UTC)
    await _shipment(
        db_session,
        client,
        profile,
        number="too-fresh",
        status=ShipmentStatus.dispatched,
        dispatched_at=now - timedelta(days=1),
    )
    await _shipment(
        db_session,
        client,
        profile,
        number="in-window",
        status=ShipmentStatus.dispatched,
        dispatched_at=now - timedelta(days=7),
    )
    await _shipment(
        db_session,
        client,
        profile,
        number="too-old",
        status=ShipmentStatus.dispatched,
        dispatched_at=now - timedelta(days=40),
    )
    await _shipment(
        db_session,
        client,
        profile,
        number="checked-today",
        status=ShipmentStatus.dispatched,
        dispatched_at=now - timedelta(days=7),
        tracking_updated_at=now - timedelta(hours=1),
    )

    stub = _NpStub()
    np_client = stub.client(_settings())
    try:
        await poll_returns(db_session, np_client=np_client, settings=_settings())
    finally:
        await np_client.aclose()

    assert stub.asked_numbers == {"in-window"}


async def test_return_watch_detects_return(db_session: AsyncSession):
    client, profile = await _client_with_profile(db_session, 955)
    shipment = await _shipment(
        db_session,
        client,
        profile,
        number="coming-back",
        status=ShipmentStatus.dispatched,
        dispatched_at=datetime.now(UTC) - timedelta(days=9),
    )

    # StatusCode 9 у НП — «Відмова від отримання», наш маппинг ведёт его в returning.
    stub = _NpStub(status="Відмова від отримання", status_code="9")
    np_client = stub.client(_settings())
    try:
        result = await poll_returns(db_session, np_client=np_client, settings=_settings())
    finally:
        await np_client.aclose()

    assert result.updated == 1
    assert shipment.status is ShipmentStatus.returning
