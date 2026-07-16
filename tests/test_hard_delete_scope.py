"""Инвариант скоупа: история переживает физическое удаление человека.

Предусловие удаления менеджеров/клиентов/работников. Схема здесь строится из
`Base.metadata`, поэтому тест стережёт именно модели: вернувшийся `CASCADE` на
`client_id` уронит эти проверки, а не прод.
"""

from __future__ import annotations

from app.db.models.enums import ShipmentStatus, StockMovementType, UserRole, UserStatus
from app.db.models.sender_profile import SenderProfile
from app.db.models.shipment import Shipment, ShipmentItem
from app.db.models.stock_movement import StockMovement
from app.db.models.support import SupportThread
from app.db.repositories import (
    SenderProfileRepository,
    ShipmentRepository,
    StockMovementRepository,
    SupportRepository,
    UserRepository,
)
from app.db.repositories.shipment import ShipmentItemDraft
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import account_of


async def _seed(session: AsyncSession, telegram_id: int, phone: str):
    owner = await UserRepository(session).create(
        telegram_id=telegram_id,
        phone=phone,
        full_name="Власник",
        role=UserRole.client,
        status=UserStatus.active,
    )
    account = await account_of(session, owner)
    profile = await SenderProfileRepository(session).create(
        client_id=owner.id, name="ФОП", np_api_key="secret-np-key", is_default=True
    )
    drafts = [ShipmentItemDraft(sku="A1", name="Товар", quantity=2)]
    shipment = await ShipmentRepository(session).create(
        client_id=owner.id,
        account_id=account.id,
        created_by_user_id=owner.id,
        sender_profile_id=profile.id,
        recipient_name="Отримувач",
        items=drafts,
        status=ShipmentStatus.delivered,
        ttn_number=f"204500000{telegram_id}",
    )
    await StockMovementRepository(session).record_for_items(
        client_id=owner.id,
        account_id=account.id,
        shipment_id=shipment.id,
        items=drafts,
        movement_type=StockMovementType.ttn_dispatch,
        sign=-1,
        comment="тест",
    )
    thread = await SupportRepository(session).create_thread(
        client_id=owner.id, account_id=account.id
    )
    await session.flush()
    return owner, account, profile, shipment, thread


async def test_deleting_user_keeps_history_and_nulls_the_author(db_session: AsyncSession):
    owner, account, _profile, shipment, thread = await _seed(db_session, 96001, "380936660011")
    # Идентификаторы забираем ДО удаления: после него ORM-объекты истекают, и
    # обращение к атрибуту ушло бы в ленивую загрузку из sync-контекста.
    ship_id, thread_id, account_id = shipment.id, thread.id, account.id

    await db_session.delete(owner)
    await db_session.flush()
    db_session.expire_all()

    ship = await db_session.get(Shipment, ship_id)
    assert ship is not None, "ТТН снесло каскадом вместе с человеком"
    assert ship.client_id is None and ship.created_by_user_id is None
    assert ship.account_id == account_id, "компания у ТТН обязана остаться"

    items = await db_session.scalar(
        select(func.count()).select_from(ShipmentItem).where(ShipmentItem.shipment_id == ship_id)
    )
    assert items == 1, "позиции ТТН потеряны"

    movements = list(
        await db_session.scalars(select(StockMovement).where(StockMovement.shipment_id == ship_id))
    )
    assert movements, "движения склада снесло каскадом"
    assert all(m.client_id is None and m.account_id == account_id for m in movements)

    alive_thread = await db_session.get(SupportThread, thread_id)
    assert alive_thread is not None, "тред поддержки снесло каскадом"
    assert alive_thread.client_id is None


async def test_deleting_user_destroys_their_np_key(db_session: AsyncSession):
    """Обратная сторона: ключ НП обязан умирать вместе с владельцем.

    Именно поэтому `sender_profiles.client_id` остался `CASCADE`, а не поехал в
    `SET NULL` вместе с историей: `account_id` — NOT NULL и аккаунт при удалении
    клиента сохраняется анонимизированным, так что SET NULL оставил бы живой
    расшифровываемый секрет сиротой.
    """
    owner, _account, profile, _shipment, _thread = await _seed(db_session, 96002, "380936660022")
    profile_id = profile.id

    await db_session.delete(owner)
    await db_session.flush()
    db_session.expire_all()

    assert await db_session.get(SenderProfile, profile_id) is None, (
        "ФОП с ключом НП пережил владельца"
    )
