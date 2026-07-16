"""Физическое удаление клиента (этап 5) — Postgres + фейковый НП.

Проверяет: гейт по активным ТТН (ноль изменений), классификацию `returned`
по движению `ttn_return`, отмену невідправлених NP-first, снос владельца+команды
с сохранением истории и уничтожением ключей НП, идемпотентность, owner/dev-гейт,
а также закрытую дыру — отказ create/cancel при не-active аккаунте.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from app.config import Settings
from app.db.models.client_account import ClientAccount
from app.db.models.enums import (
    ClientAccountStatus,
    ShipmentStatus,
    StockMovementType,
    SupportThreadStatus,
    UserRole,
    UserStatus,
)
from app.db.models.notification_setting import NotificationSetting
from app.db.models.sender_profile import SenderProfile
from app.db.models.shipment import Shipment
from app.db.models.support import SupportThread
from app.db.models.user import User
from app.db.repositories import (
    ClientAccountRepository,
    NotificationSettingRepository,
    SenderProfileRepository,
    ShipmentRepository,
    StockMovementRepository,
    SupportRepository,
    UserRepository,
)
from app.db.repositories.shipment import ShipmentItemDraft
from app.novaposhta.client import NovaPoshtaClient
from app.services import clients
from app.services import shipment as shipment_service
from app.services.exceptions import (
    ClientDeletionBlocked,
    ClientDeletionRetryable,
    PermissionDenied,
)
from app.sheets.inventory import StockRow
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import account_of, employee_of


def _np(routes: dict[tuple[str, str], object]) -> NovaPoshtaClient:
    settings = Settings(_env_file=None)
    settings.np_retry_backoff = 0.0

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        result = routes[(body["modelName"], body["calledMethod"])]
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(
            200, json={"success": True, "data": result, "errors": [], "errorCodes": []}
        )

    return NovaPoshtaClient(settings=settings, transport=httpx.MockTransport(handler))


def _np_ok() -> NovaPoshtaClient:
    return _np({("InternetDocument", "delete"): [{"Ref": "doc-ref"}]})


def _np_fail() -> NovaPoshtaClient:
    fail = httpx.Response(
        200, json={"success": False, "data": [], "errors": ["НП недоступна"], "errorCodes": []}
    )
    return _np({("InternetDocument", "delete"): fail})


def _exploding_np() -> NovaPoshtaClient:
    settings = Settings(_env_file=None)
    settings.np_retry_backoff = 0.0

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("НП не повинна викликатися — гейт має спрацювати раніше")

    return NovaPoshtaClient(settings=settings, transport=httpx.MockTransport(handler))


class _FakeReader:
    def read_stock(self, client_key: str):
        return [
            StockRow(sku="COF-1", name="Кава", category="Кава", quantity=10, price=Decimal("100"))
        ]


async def _platform_owner(session: AsyncSession, tg: int = 90100) -> User:
    return await UserRepository(session).create(
        telegram_id=tg,
        phone=f"+38050000{tg}",
        full_name="Власник платформи",
        role=UserRole.owner,
        status=UserStatus.active,
    )


async def _client_owner(
    session: AsyncSession, tg: int = 90200, phone: str = "380991110000"
) -> tuple[User, ClientAccount, SenderProfile]:
    owner = await UserRepository(session).create(
        telegram_id=tg,
        phone=phone,
        full_name="Клієнт",
        role=UserRole.client,
        status=UserStatus.active,
    )
    account = await account_of(session, owner)
    profile = await SenderProfileRepository(session).create(
        client_id=owner.id, name="ФОП", np_api_key="np-key", is_default=True
    )
    return owner, account, profile


async def _shipment(
    session: AsyncSession,
    *,
    account: ClientAccount,
    profile: SenderProfile,
    status: ShipmentStatus,
    ttn: str,
    client_id,
    np_ref: str | None = "doc-ref",
) -> Shipment:
    drafts = [ShipmentItemDraft(sku="COF-1", name="Кава", quantity=2)]
    return await ShipmentRepository(session).create(
        client_id=client_id,
        account_id=account.id,
        created_by_user_id=client_id,
        sender_profile_id=profile.id,
        recipient_name="Отримувач",
        items=drafts,
        status=status,
        ttn_number=ttn,
        np_ref=np_ref,
    )


# --- Гейт по ТТН ----------------------------------------------------------


async def test_delete_blocked_by_active_ttn_changes_nothing(db_session: AsyncSession):
    actor = await _platform_owner(db_session)
    owner, account, profile = await _client_owner(db_session)
    ship = await _shipment(
        db_session,
        account=account,
        profile=profile,
        status=ShipmentStatus.dispatched,
        ttn="59000101",
        client_id=owner.id,
    )
    await db_session.flush()
    ship_id, account_id, owner_id = ship.id, account.id, owner.id

    with pytest.raises(ClientDeletionBlocked) as excinfo:
        await clients.delete_client(db_session, actor=actor, client_id=owner_id, np_client=_np_ok())
    assert any(b.id == ship_id for b in excinfo.value.blocking)

    db_session.expire_all()
    assert (await db_session.get(ClientAccount, account_id)).status is ClientAccountStatus.active
    assert await db_session.get(User, owner_id) is not None
    assert (await db_session.get(Shipment, ship_id)).status is ShipmentStatus.dispatched


async def test_returned_without_ttn_return_movement_blocks(db_session: AsyncSession):
    actor = await _platform_owner(db_session)
    owner, account, profile = await _client_owner(db_session)
    await _shipment(
        db_session,
        account=account,
        profile=profile,
        status=ShipmentStatus.returned,
        ttn="59000102",
        client_id=owner.id,
    )
    await db_session.flush()

    with pytest.raises(ClientDeletionBlocked):
        await clients.delete_client(db_session, actor=actor, client_id=owner.id, np_client=_np_ok())


async def test_returned_with_ttn_return_movement_is_finished(db_session: AsyncSession):
    actor = await _platform_owner(db_session)
    owner, account, profile = await _client_owner(db_session)
    ship = await _shipment(
        db_session,
        account=account,
        profile=profile,
        status=ShipmentStatus.returned,
        ttn="59000103",
        client_id=owner.id,
    )
    # Оформленный возврат на склад — это и есть движение `ttn_return`.
    await StockMovementRepository(db_session).record_for_items(
        client_id=owner.id,
        account_id=account.id,
        shipment_id=ship.id,
        items=[ShipmentItemDraft(sku="COF-1", name="Кава", quantity=2)],
        movement_type=StockMovementType.ttn_return,
        sign=1,
        comment="повернення",
    )
    await db_session.flush()
    account_id = account.id

    result = await clients.delete_client(
        db_session, actor=actor, client_id=owner.id, np_client=_np_ok()
    )
    assert result.already_done is False
    db_session.expire_all()
    assert (await db_session.get(ClientAccount, account_id)).status is ClientAccountStatus.archived


# --- Снос --------------------------------------------------------------------


async def test_delete_cancels_unsent_tears_down_and_keeps_history(db_session: AsyncSession):
    actor = await _platform_owner(db_session)
    owner, account, profile = await _client_owner(db_session)
    employee = await employee_of(db_session, owner, phone="380991110001", telegram_id=90201)
    ship = await _shipment(
        db_session,
        account=account,
        profile=profile,
        status=ShipmentStatus.created,
        ttn="59000104",
        client_id=owner.id,
    )
    await NotificationSettingRepository(db_session).set_enabled(
        user_id=owner.id, key="low_stock", enabled=False
    )
    thread = await SupportRepository(db_session).create_thread(
        client_id=owner.id, account_id=account.id
    )
    await db_session.flush()
    owner_id, employee_id = owner.id, employee.id
    account_id, ship_id, profile_id, thread_id = account.id, ship.id, profile.id, thread.id

    # Резерв под `created` активен до отмены.
    assert await ShipmentRepository(db_session).reserved_by_account(account_id) == {"COF-1": 2}

    result = await clients.delete_client(
        db_session, actor=actor, client_id=owner_id, np_client=_np_ok()
    )
    assert result.already_done is False
    assert result.cancelled == 1
    assert result.team_removed == 2

    db_session.expire_all()
    # Владелец и работник физически удалены — номера/Telegram свободны.
    assert await db_session.get(User, owner_id) is None
    assert await db_session.get(User, employee_id) is None
    # Аккаунт — анонимная надгробная плита.
    account = await db_session.get(ClientAccount, account_id)
    assert account.status is ClientAccountStatus.archived
    assert account.name == clients.DELETED_CLIENT_NAME
    assert account.stock_sheet_key is None and account.stock_view_book_id is None
    # ФОП с зашифрованным ключом НП уничтожен.
    assert await db_session.get(SenderProfile, profile_id) is None
    # Членства нет.
    assert await ClientAccountRepository(db_session).get_membership(user_id=owner_id) is None
    # Настройки уведомлений ушли каскадом.
    settings_left = await db_session.scalar(
        select(func.count())
        .select_from(NotificationSetting)
        .where(NotificationSetting.user_id == owner_id)
    )
    assert settings_left == 0
    # История цела: ТТН на месте, автор обнулён, компания сохранена, статус cancelled.
    ship = await db_session.get(Shipment, ship_id)
    assert ship is not None
    assert ship.status is ShipmentStatus.cancelled
    assert ship.client_id is None and ship.account_id == account_id
    # Резерв освобождён отменой.
    assert await ShipmentRepository(db_session).reserved_by_account(account_id) == {}
    # Обращение закрыто, а не «висит» без клиента.
    assert (await db_session.get(SupportThread, thread_id)).status is SupportThreadStatus.closed


async def test_delete_np_error_is_retryable_and_keeps_team(db_session: AsyncSession):
    actor = await _platform_owner(db_session)
    owner, account, profile = await _client_owner(db_session)
    ship = await _shipment(
        db_session,
        account=account,
        profile=profile,
        status=ShipmentStatus.created,
        ttn="59000105",
        client_id=owner.id,
    )
    await db_session.flush()
    owner_id, account_id, ship_id = owner.id, account.id, ship.id

    with pytest.raises(ClientDeletionRetryable):
        await clients.delete_client(
            db_session, actor=actor, client_id=owner_id, np_client=_np_fail()
        )

    db_session.expire_all()
    # Снос не начинался: команда цела, ТТН не отменена — но заморозка уцелела.
    assert await db_session.get(User, owner_id) is not None
    assert (await db_session.get(ClientAccount, account_id)).status is ClientAccountStatus.blocked
    assert (await db_session.get(Shipment, ship_id)).status is ShipmentStatus.created


async def test_delete_is_idempotent_on_repeat(db_session: AsyncSession):
    actor = await _platform_owner(db_session)
    owner, _account, _profile = await _client_owner(db_session)
    await db_session.flush()
    owner_id = owner.id

    first = await clients.delete_client(
        db_session, actor=actor, client_id=owner_id, np_client=_np_ok()
    )
    assert first.already_done is False
    second = await clients.delete_client(
        db_session, actor=actor, client_id=owner_id, np_client=_np_ok()
    )
    assert second.already_done is True


# --- Права -------------------------------------------------------------------


async def test_delete_requires_owner(db_session: AsyncSession):
    manager = await UserRepository(db_session).create(
        telegram_id=90300,
        phone="+380509990300",
        full_name="Менеджер",
        role=UserRole.manager,
        status=UserStatus.active,
    )
    owner, _account, _profile = await _client_owner(db_session)
    await db_session.flush()
    owner_id = owner.id

    with pytest.raises(PermissionDenied):
        await clients.delete_client(
            db_session, actor=manager, client_id=owner_id, np_client=_np_ok()
        )
    with pytest.raises(PermissionDenied):
        await clients.preview_client_deletion(db_session, actor=manager, client_id=owner_id)
    assert await db_session.get(User, owner_id) is not None


async def test_delete_refuses_employee_target(db_session: AsyncSession):
    actor = await _platform_owner(db_session)
    owner, _account, _profile = await _client_owner(db_session)
    employee = await employee_of(db_session, owner, phone="380991110002", telegram_id=90202)
    await db_session.flush()
    employee_id = employee.id

    with pytest.raises(PermissionDenied):
        await clients.delete_client(
            db_session, actor=actor, client_id=employee_id, np_client=_np_ok()
        )
    assert await db_session.get(User, employee_id) is not None


async def test_preview_counts_team_and_shipments(db_session: AsyncSession):
    actor = await _platform_owner(db_session)
    owner, account, profile = await _client_owner(db_session)
    await employee_of(db_session, owner, phone="380991110003", telegram_id=90203)
    await _shipment(
        db_session,
        account=account,
        profile=profile,
        status=ShipmentStatus.delivered,
        ttn="59000106",
        client_id=owner.id,
    )
    await _shipment(
        db_session,
        account=account,
        profile=profile,
        status=ShipmentStatus.created,
        ttn="59000107",
        client_id=owner.id,
    )
    await db_session.flush()

    preview = await clients.preview_client_deletion(db_session, actor=actor, client_id=owner.id)
    assert preview.team_size == 2  # владелец + работник
    assert preview.shipments_total == 2
    assert preview.full_name == "Клієнт"


# --- Закрытая дыра: заморозка реальна ---------------------------------------


async def test_cancel_refused_for_blocked_account_owner(db_session: AsyncSession):
    owner, account, profile = await _client_owner(db_session)
    ship = await _shipment(
        db_session,
        account=account,
        profile=profile,
        status=ShipmentStatus.created,
        ttn="59000108",
        client_id=owner.id,
    )
    # Аккаунт заморожен, но статус ВЛАДЕЛЬЦА остаётся active — ровно состояние
    # `delete_client` во время сноса (и обычной блокировки для работников).
    account.status = ClientAccountStatus.blocked
    await db_session.flush()
    ship_id = ship.id

    with pytest.raises(PermissionDenied):
        await shipment_service.cancel_shipment(
            db_session, client=owner, shipment_id=ship_id, np_client=_np_ok()
        )
    db_session.expire_all()
    assert (await db_session.get(Shipment, ship_id)).status is ShipmentStatus.created


async def test_cancel_refused_for_blocked_account_employee(db_session: AsyncSession):
    owner, account, profile = await _client_owner(db_session)
    employee = await employee_of(db_session, owner, phone="380991110004", telegram_id=90204)
    ship = await _shipment(
        db_session,
        account=account,
        profile=profile,
        status=ShipmentStatus.created,
        ttn="59000109",
        client_id=employee.id,  # ТТН создана работником
    )
    account.status = ClientAccountStatus.blocked  # работник остаётся active
    await db_session.flush()
    ship_id = ship.id

    with pytest.raises(PermissionDenied):
        await shipment_service.cancel_shipment(
            db_session, client=employee, shipment_id=ship_id, np_client=_np_ok()
        )
    db_session.expire_all()
    assert (await db_session.get(Shipment, ship_id)).status is ShipmentStatus.created


async def test_create_refused_for_blocked_account(db_session: AsyncSession):
    owner, account, _profile = await _client_owner(db_session)
    account.status = ClientAccountStatus.blocked
    await db_session.flush()

    with pytest.raises(PermissionDenied):
        await shipment_service.create_shipment(
            db_session,
            client=owner,
            account=account,
            items=[("COF-1", 1)],
            recipient_kind="person",
            recipient_name="Іван",
            recipient_phone="380671234567",
            recipient_city_ref="c",
            recipient_city_name="Київ",
            recipient_warehouse_ref="w",
            recipient_warehouse_name="№1",
            weight=Decimal("1"),
            size_preset="mala",
            description="Кава",
            insured_amount=Decimal("100"),
            np_client=_exploding_np(),  # гейт срабатывает ДО обращения к НП
            reader=_FakeReader(),
        )
