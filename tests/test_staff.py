"""Тесты сервиса управления персоналом (`app/services/staff.py`) — на Postgres."""

from __future__ import annotations

import pytest
from app.bot import permissions as perm
from app.bot.types import ClientAccountContext
from app.db.models.audit import AuditLog
from app.db.models.enums import (
    ClientAccountStatus,
    MembershipRole,
    SupportThreadStatus,
    UserRole,
    UserStatus,
)
from app.db.repositories import (
    ClientAccountRepository,
    SenderProfileRepository,
    SupportRepository,
    UserRepository,
)
from app.services import account_team, staff
from app.services.exceptions import (
    InvalidPermissionFlag,
    PermissionDenied,
    StaffAlreadyManager,
    StaffPromotionForbidden,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def _owner(session: AsyncSession, telegram_id: int = 1):
    return await UserRepository(session).create(
        telegram_id=telegram_id, role=UserRole.owner, status=UserStatus.active
    )


async def _manager(session: AsyncSession, telegram_id: int = 10):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=f"+38067{telegram_id}",
        full_name=f"Менеджер {telegram_id}",
        role=UserRole.manager,
        status=UserStatus.active,
    )


async def _client(session: AsyncSession, telegram_id: int = 100, status=UserStatus.active):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=f"+38050{telegram_id}",
        full_name=f"Клієнт {telegram_id}",
        role=UserRole.client,
        status=status,
    )


async def _audit_actions(session: AsyncSession) -> list[str]:
    rows = await session.scalars(select(AuditLog.action).order_by(AuditLog.created_at))
    return list(rows)


async def test_list_staff_owner_only(db_session: AsyncSession):
    owner = await _owner(db_session)
    await _manager(db_session)
    client = await _client(db_session)

    page = await staff.list_staff(db_session, actor=owner)
    assert page.total == 1

    with pytest.raises(PermissionDenied):
        await staff.list_staff(db_session, actor=client)


async def test_add_manager_by_telegram_creates(db_session: AsyncSession):
    owner = await _owner(db_session)

    result = await staff.add_manager(db_session, actor=owner, telegram_id=555)

    created = await UserRepository(db_session).get_by_telegram_id(555)
    assert created.role is UserRole.manager
    assert created.status is UserStatus.active
    assert result.telegram_id == 555
    assert perm.has_permission(created, perm.CAN_HANDLE_SUPPORT)  # флаги on по умолчанию
    assert "manager_added" in await _audit_actions(db_session)


async def test_add_manager_rejects_active_client(db_session: AsyncSession):
    owner = await _owner(db_session)
    client = await _client(db_session, telegram_id=200)

    with pytest.raises(StaffPromotionForbidden):
        await staff.add_manager(db_session, actor=owner, telegram_id=client.telegram_id)


async def test_add_manager_already_manager(db_session: AsyncSession):
    owner = await _owner(db_session)
    manager = await _manager(db_session, telegram_id=11)

    with pytest.raises(StaffAlreadyManager):
        await staff.add_manager(db_session, actor=owner, telegram_id=manager.telegram_id)


async def test_add_manager_by_normalized_phone_promotes(db_session: AsyncSession):
    owner = await _owner(db_session)
    users = UserRepository(db_session)
    # Хранится в формате НП (как теперь пишет register_contact).
    await users.create(
        telegram_id=888,
        phone="380671234567",
        role=UserRole.client,
        status=UserStatus.pending,
    )

    # Найм по тому же номеру в НП-формате (handler нормализует 0.../+380... к нему).
    result = await staff.add_manager(db_session, actor=owner, phone="380671234567")

    assert result.telegram_id == 888
    assert (await users.get_by_telegram_id(888)).role is UserRole.manager


async def test_add_manager_by_phone_precreates_unknown(db_session: AsyncSession):
    """Незнакомый номер → предзаготовка менеджера без telegram_id (подхват при входе)."""
    owner = await _owner(db_session)
    users = UserRepository(db_session)

    result = await staff.add_manager(db_session, actor=owner, phone="380509998877")

    assert result.telegram_id is None  # ещё не входил в бота
    precreated = await users.get_by_phone("380509998877")
    assert precreated is not None
    assert precreated.telegram_id is None
    assert precreated.role is UserRole.manager
    assert precreated.status is UserStatus.active
    assert perm.has_permission(precreated, perm.CAN_HANDLE_SUPPORT)
    assert "manager_added" in await _audit_actions(db_session)


async def test_add_manager_requires_exactly_one_identifier(db_session: AsyncSession):
    owner = await _owner(db_session)

    with pytest.raises(StaffPromotionForbidden):
        await staff.add_manager(db_session, actor=owner)
    with pytest.raises(StaffPromotionForbidden):
        await staff.add_manager(db_session, actor=owner, telegram_id=5, phone="380671112233")


async def test_set_permission_toggles_and_audits(db_session: AsyncSession):
    owner = await _owner(db_session)
    manager = await _manager(db_session)

    card = await staff.set_permission(
        db_session,
        actor=owner,
        manager_id=manager.id,
        flag=perm.CAN_HANDLE_SUPPORT,
        enabled=False,
    )

    state = {p.key: p.enabled for p in card.permissions}
    assert state[perm.CAN_HANDLE_SUPPORT] is False
    assert not perm.has_permission(manager, perm.CAN_HANDLE_SUPPORT)
    assert "permission_changed" in await _audit_actions(db_session)


async def test_set_permission_invalid_flag(db_session: AsyncSession):
    owner = await _owner(db_session)
    manager = await _manager(db_session)
    with pytest.raises(InvalidPermissionFlag):
        await staff.set_permission(
            db_session, actor=owner, manager_id=manager.id, flag="can_fly", enabled=True
        )


async def test_block_clears_duty_and_threads_then_unblock(db_session: AsyncSession):
    owner = await _owner(db_session)
    manager = await _manager(db_session)
    client = await _client(db_session)
    await UserRepository(db_session).set_duty(manager, on_duty=True, duty_since=None)
    thread = await SupportRepository(db_session).create_thread(
        client_id=client.id, assigned_manager_id=manager.id, status=SupportThreadStatus.open
    )

    card = await staff.block_manager(db_session, actor=owner, manager_id=manager.id)
    assert card.status is UserStatus.blocked
    assert manager.on_duty is False
    refreshed = await SupportRepository(db_session).get_with_messages(thread.id)
    assert refreshed.status is SupportThreadStatus.waiting  # тред вернулся в очередь
    assert refreshed.assigned_manager_id is None

    back = await staff.unblock_manager(db_session, actor=owner, manager_id=manager.id)
    assert back.status is UserStatus.active


async def test_delete_manager_blocks_demotes_and_clears_threads(db_session: AsyncSession):
    owner = await _owner(db_session)
    manager = await _manager(db_session)
    client = await _client(db_session)
    await UserRepository(db_session).set_duty(manager, on_duty=True, duty_since=None)
    thread = await SupportRepository(db_session).create_thread(
        client_id=client.id, assigned_manager_id=manager.id, status=SupportThreadStatus.open
    )

    await staff.delete_manager(db_session, actor=owner, manager_id=manager.id)

    assert manager.role is UserRole.client
    assert manager.status is UserStatus.blocked
    assert manager.on_duty is False
    assert manager.permissions == {}
    refreshed = await SupportRepository(db_session).get_with_messages(thread.id)
    assert refreshed.status is SupportThreadStatus.waiting
    assert refreshed.assigned_manager_id is None
    assert "manager_deleted" in await _audit_actions(db_session)


async def test_add_manager_rejects_client_employee(db_session: AsyncSession):
    # Инвариант владельца: клиент/его работники и менеджер платформы —
    # непересекающиеся множества. Обратное направление уже закрыто в
    # `account_team.invite_employee`, а найм работника проходил: приглашённый
    # заведён как `role=client, status=pending`, то есть не «активный клиент».
    actor = await _owner(db_session)
    shop = await UserRepository(db_session).create(
        telegram_id=700,
        phone="380507000700",
        full_name="Магазин",
        role=UserRole.client,
        status=UserStatus.active,
    )
    accounts = ClientAccountRepository(db_session)
    membership = await accounts.get_membership(user_id=shop.id)
    assert membership is not None
    invited = await account_team.invite_employee(
        db_session,
        context=ClientAccountContext(user=shop, account=membership.account, membership=membership),
        phone="0507000701",
    )

    with pytest.raises(StaffPromotionForbidden):
        await staff.add_manager(db_session, actor=actor, phone="380507000701")

    # Работник остался работником, акаунт работодателя не тронут.
    employee = await UserRepository(db_session).get_by_id(invited.user_id)
    assert employee.role is UserRole.client
    assert membership.account.status is ClientAccountStatus.active


async def test_delete_manager_gives_demoted_client_an_account(db_session: AsyncSession):
    # Регрессия: `users.create` заводит акаунт только роли `client`, поэтому у
    # менеджера его нет. Снятие роли делало его клиентом БЕЗ акаунта, а `account_id`
    # во всех клиентских таблицах NOT NULL → первая же запись (ФОП/ТТН/склад) падала
    # NotNullViolation. Путь достижим: найм по Telegram-ID → зняття ролі → розблокування.
    owner = await _owner(db_session)
    manager = await _manager(db_session, telegram_id=777)
    accounts = ClientAccountRepository(db_session)
    assert await accounts.get_membership(user_id=manager.id) is None

    await staff.delete_manager(db_session, actor=owner, manager_id=manager.id)

    assert manager.role is UserRole.client
    membership = await accounts.get_membership(user_id=manager.id)
    assert membership is not None, "клиент без акаунта — сломанное состояние"
    assert membership.role is MembershipRole.account_owner

    # И запись клиентских данных теперь проходит, а не падает NotNullViolation.
    await SenderProfileRepository(db_session).create(
        client_id=manager.id, name="ФОП", np_api_key="k", is_default=True
    )
    await db_session.flush()
