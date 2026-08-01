"""Сервис управления клиентами (Фаза 2) — доменная логика без aiogram.

Паттерн как в `app/services/bootstrap.py`: функции принимают `AsyncSession`,
внутри строят репозитории, пишут аудит. Транзакцией управляет вызывающий
(middleware бота / тест). Бот-слой зовёт эти функции и рендерит результат;
ошибки — подтипы `ClientServiceError` (см. `app/services/exceptions.py`).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import permissions
from app.config import Settings
from app.db.models.enums import (
    ClientAccountStatus,
    MembershipRole,
    MembershipStatus,
    ShipmentStatus,
    StockMovementType,
    UserRole,
    UserStatus,
)
from app.db.models.user import User
from app.db.repositories import (
    AuditRepository,
    ClientAccountRepository,
    SenderProfileRepository,
    ShipmentRepository,
    SupportRepository,
    UserRepository,
)
from app.services import shipment as shipment_service
from app.services import shipments
from app.services.client_sheet_sync import best_effort_sync
from app.services.exceptions import (
    AlreadyInStatus,
    ClientDeletionBlocked,
    ClientDeletionRetryable,
    ClientNotFound,
    PermissionDenied,
    PhoneAlreadyTaken,
    TransitionForbidden,
    TtnCancelFailed,
)

if TYPE_CHECKING:
    from app.db.models.shipment import Shipment
    from app.novaposhta.client import NovaPoshtaClient

# Имя-надгробие удалённого клиента. Аккаунт физически не удаляется (иначе
# осиротеет `account_id` в истории ТТН/склада/поддержки), а анонимизируется:
# `name = DELETED_CLIENT_NAME`, `status = archived`, ссылки на листы очищены.
DELETED_CLIENT_NAME = "Видалений клієнт"

# Активные ТТН блокируют удаление: их сначала доводит до конца менеджер. `returned`
# сюда не входит статически — он блокирует только без движения `ttn_return`
# (возврат ещё не оформлен на склад), проверяется отдельно.
_ACTIVE_SHIPMENT_STATUSES = {
    ShipmentStatus.dispatched,
    ShipmentStatus.in_transit,
    ShipmentStatus.arrived,
    ShipmentStatus.returning,
}

# Per-flag права (ключи в `users.permissions`). Канонический источник —
# `app/bot/permissions.py`; здесь — алиас для обратной совместимости вызовов
# `clients.CAN_MANAGE_CLIENTS`. Правка профиля клиента per-flag'ом больше не
# гейтится — это действие только владельца (`permissions.require_owner`).
CAN_MANAGE_CLIENTS = permissions.CAN_MANAGE_CLIENTS  # подтверждение/блокировка


@dataclass(frozen=True, slots=True)
class ClientListItem:
    id: uuid.UUID
    telegram_id: int
    full_name: str | None
    phone: str | None
    status: UserStatus
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ClientPage:
    items: list[ClientListItem]
    total: int
    status_counts: dict[UserStatus, int]
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class ClientCard:
    id: uuid.UUID
    telegram_id: int
    full_name: str | None
    phone: str | None
    role: UserRole
    status: UserStatus
    created_at: datetime
    sender_profiles_count: int
    default_sender_name: str | None


@dataclass(frozen=True, slots=True)
class BlockingShipment:
    """ТТН, из-за которой удаление клиента отклонено (для показа владельцу)."""

    id: uuid.UUID
    ttn_number: str | None
    status: ShipmentStatus


@dataclass(frozen=True, slots=True)
class ClientDeletionPreview:
    """Сводка для двойного подтверждения: сколько людей и ТТН заденет удаление."""

    client_id: uuid.UUID
    account_id: uuid.UUID
    full_name: str | None
    phone: str | None
    team_size: int  # включая владельца
    shipments_total: int


@dataclass(frozen=True, slots=True)
class ClientDeletionResult:
    account_id: uuid.UUID
    cancelled: int
    team_removed: int
    already_done: bool = False


async def _get_client(users: UserRepository, client_id: uuid.UUID) -> User:
    user = await users.get_by_id(client_id)
    if user is None or user.role is not UserRole.client:
        raise ClientNotFound(str(client_id))
    return user


async def _card(session: AsyncSession, user: User) -> ClientCard:
    profiles = SenderProfileRepository(session)
    items = await profiles.list_for_client(user.id)
    # Дефолт уже среди items — отдельный запрос не нужен.
    default = next((p for p in items if p.is_default), None)
    return ClientCard(
        id=user.id,
        telegram_id=user.telegram_id,
        full_name=user.full_name,
        phone=user.phone,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        sender_profiles_count=len(items),
        default_sender_name=default.name if default else None,
    )


def _check_transition(user: User, to: UserStatus, allowed_from: set[UserStatus]) -> None:
    if user.status is to:
        raise AlreadyInStatus(to)
    if user.status not in allowed_from:
        raise TransitionForbidden(user.status, to)


async def _transition(
    session: AsyncSession,
    *,
    actor: User,
    client_id: uuid.UUID,
    to: UserStatus,
    allowed_from: set[UserStatus],
    action: str,
    settings: Settings | None,
    notes: str | None = None,
) -> ClientCard:
    users = UserRepository(session)
    user = await _get_client(users, client_id)
    permissions.require_can_manage(actor, user, CAN_MANAGE_CLIENTS, settings)
    _check_transition(user, to, allowed_from)

    before = {"status": user.status}
    await users.update_status(user, to)
    membership = await ClientAccountRepository(session).get_membership(user_id=user.id)
    if membership is not None:
        # Доступ работников режет именно `account.status`: их членства здесь не
        # трогаются (`get_membership` возвращает членство ВЛАДЕЛЬЦА), а
        # `get_context_for_user` смотрит на статус акаунта.
        account_status = {
            UserStatus.blocked: ClientAccountStatus.blocked,
            UserStatus.archived: ClientAccountStatus.archived,
            # `pending` — тоже НЕ active: `restore_client` возвращает archived→pending
            # именно чтобы не снять блок молча, а активный акаунт вернул бы доступ
            # всей команде раньше, чем менеджер повторно подтвердит владельца.
            # Обратно в active акаунт вернёт `approve_client`.
            UserStatus.pending: ClientAccountStatus.blocked,
        }.get(to, ClientAccountStatus.active)
        membership.account.status = account_status
        membership_status = (
            MembershipStatus.blocked
            if to in {UserStatus.blocked, UserStatus.archived}
            else MembershipStatus.active
        )
        await ClientAccountRepository(session).set_membership_status(membership, membership_status)
    await AuditRepository(session).log(
        action,
        user_id=actor.id,
        # Субъект — аккаунт клиента, которого менеджер трогает, а не аккаунт актора.
        account_id=membership.account_id if membership is not None else None,
        affected_entity=f"user:{user.id}",
        before=before,
        after={"status": to},
        notes=notes,
    )
    return await _card(session, user)


# --- Чтение ---------------------------------------------------------------


async def list_clients(
    session: AsyncSession,
    *,
    actor: User,
    status: UserStatus | None = None,
    query: str | None = None,
    limit: int = 20,
    offset: int = 0,
    settings: Settings | None = None,
) -> ClientPage:
    permissions.require_staff(actor, settings)
    users = UserRepository(session)
    rows, total = await users.list_by_status(status=status, query=query, limit=limit, offset=offset)
    counts = await users.count_by_status()
    items = [
        ClientListItem(
            id=u.id,
            telegram_id=u.telegram_id,
            full_name=u.full_name,
            phone=u.phone,
            status=u.status,
            created_at=u.created_at,
        )
        for u in rows
    ]
    return ClientPage(items=items, total=total, status_counts=counts, limit=limit, offset=offset)


async def get_client_card(
    session: AsyncSession,
    *,
    actor: User,
    client_id: uuid.UUID,
    settings: Settings | None = None,
) -> ClientCard:
    permissions.require_staff(actor, settings)
    user = await _get_client(UserRepository(session), client_id)
    return await _card(session, user)


# --- Мутации --------------------------------------------------------------


async def approve_client(
    session: AsyncSession, *, actor: User, client_id: uuid.UUID, settings: Settings | None = None
) -> ClientCard:
    return await _transition(
        session,
        actor=actor,
        client_id=client_id,
        to=UserStatus.active,
        allowed_from={UserStatus.pending},
        action="client_approved",
        settings=settings,
    )


async def block_client(
    session: AsyncSession,
    *,
    actor: User,
    client_id: uuid.UUID,
    reason: str | None = None,
    settings: Settings | None = None,
) -> ClientCard:
    return await _transition(
        session,
        actor=actor,
        client_id=client_id,
        to=UserStatus.blocked,
        allowed_from={UserStatus.pending, UserStatus.active},
        action="client_blocked",
        settings=settings,
        notes=reason,
    )


async def unblock_client(
    session: AsyncSession, *, actor: User, client_id: uuid.UUID, settings: Settings | None = None
) -> ClientCard:
    return await _transition(
        session,
        actor=actor,
        client_id=client_id,
        to=UserStatus.active,
        allowed_from={UserStatus.blocked},
        action="client_unblocked",
        settings=settings,
    )


async def archive_client(
    session: AsyncSession, *, actor: User, client_id: uuid.UUID, settings: Settings | None = None
) -> ClientCard:
    return await _transition(
        session,
        actor=actor,
        client_id=client_id,
        to=UserStatus.archived,
        allowed_from={UserStatus.pending, UserStatus.active, UserStatus.blocked},
        action="client_archived",
        settings=settings,
    )


async def restore_client(
    session: AsyncSession, *, actor: User, client_id: uuid.UUID, settings: Settings | None = None
) -> ClientCard:
    # Архивный клиент мог быть заархивирован из blocked — возвращаем в pending
    # (повторное подтверждение), а не сразу в active, чтобы не «снять» блок молча.
    return await _transition(
        session,
        actor=actor,
        client_id=client_id,
        to=UserStatus.pending,
        allowed_from={UserStatus.archived},
        action="client_restored",
        settings=settings,
    )


async def update_client_profile(
    session: AsyncSession,
    *,
    actor: User,
    client_id: uuid.UUID,
    full_name: str | None = None,
    phone: str | None = None,
    settings: Settings | None = None,
) -> ClientCard:
    users = UserRepository(session)
    user = await _get_client(users, client_id)
    permissions.require_owner(actor, settings)

    before = {"full_name": user.full_name, "phone": user.phone}
    changed = False
    if full_name is not None and full_name != user.full_name:
        user.full_name = full_name
        changed = True
    if phone is not None and phone != user.phone:
        # Телефон — UNIQUE: проверяем коллизию заранее, чтобы вернуть доменную
        # ошибку, а не «сырой» IntegrityError на flush (бот ловит ClientServiceError).
        clash = await users.get_by_phone(phone)
        if clash is not None and clash.id != user.id:
            raise PhoneAlreadyTaken(phone)
        user.phone = phone
        changed = True
    if changed:
        await session.flush()
        # Субъект — аккаунт клиента, чей профиль правят. Актор (владелец) здесь ни
        # при чём, поэтому членство тянем по `user`, а не по `actor`.
        membership = await ClientAccountRepository(session).get_membership(user_id=user.id)
        await AuditRepository(session).log(
            "client_profile_updated",
            user_id=actor.id,
            account_id=membership.account_id if membership is not None else None,
            affected_entity=f"user:{user.id}",
            before=before,
            after={"full_name": user.full_name, "phone": user.phone},
        )
        if full_name is not None and membership is not None:
            await best_effort_sync(
                session,
                client=user,
                account=membership.account,
                log_key="client_profile_sheet_sync_failed",
                user_id=str(user.id),
            )
    return await _card(session, user)


# --- Физическое удаление клиента ------------------------------------------


async def _lock_account(session: AsyncSession, account_id: uuid.UUID) -> None:
    """Сериализовать удаление аккаунта advisory-локом (снимается с транзакцией).

    Двойной клик по «🗑 Видалити клієнта» шлёт два колбэка параллельными тасками
    (у каждой своя сессия). Без сериализации обе прошли бы классификацию и обе
    начали бы снос. Образец — `support._lock_thread_scope`.
    """
    key = int.from_bytes(
        hashlib.blake2b(account_id.bytes, digest_size=8).digest(), "big", signed=True
    )
    await session.execute(select(func.pg_advisory_xact_lock(key)))


async def _all_shipments(
    repo: ShipmentRepository, account_id: uuid.UUID, statuses: set[ShipmentStatus]
) -> list[Shipment]:
    """Все ТТН аккаунта в заданных статусах (пагинация, без «тихого» усечения)."""
    out: list[Shipment] = []
    offset = 0
    while True:
        rows, total = await repo.get_by_account_and_status(
            account_id, statuses=statuses, limit=100, offset=offset
        )
        out.extend(rows)
        offset += len(rows)
        if not rows or offset >= total:
            return out


async def preview_client_deletion(
    session: AsyncSession, *, actor: User, client_id: uuid.UUID, settings: Settings | None = None
) -> ClientDeletionPreview:
    """Сводка для двойного подтверждения удаления клиента (owner/dev, read-only)."""
    permissions.require_owner(actor, settings)
    user = await UserRepository(session).get_by_id(client_id)
    if user is None or user.role is not UserRole.client:
        raise ClientNotFound(str(client_id))
    accounts = ClientAccountRepository(session)
    membership = await accounts.get_membership(user_id=client_id)
    if membership is None or membership.role is not MembershipRole.account_owner:
        # Работника аккаунта удаляет владелец команды через «👥 Команда», а не
        # менеджер платформы через карточку клиента.
        raise PermissionDenied("це працівник акаунта, а не головний клієнт")
    account = membership.account
    _, team_size = await accounts.list_members(account.id, limit=1)
    _, shipments_total = await ShipmentRepository(session).get_by_account_and_status(
        account.id, limit=1
    )
    return ClientDeletionPreview(
        client_id=user.id,
        account_id=account.id,
        full_name=user.full_name,
        phone=user.phone,
        team_size=team_size,
        shipments_total=shipments_total,
    )


async def delete_client(
    session: AsyncSession,
    *,
    actor: User,
    client_id: uuid.UUID,
    np_client: NovaPoshtaClient,
    settings: Settings | None = None,
) -> ClientDeletionResult:
    """Физически удалить клиента-владельца вместе со всей его командой. Owner/dev-only.

    Операция многотранзакционная — редкое исключение из правила «транзакцией
    управляет вызывающий» (см. докстринг модуля). Причина: заморозку нужно сделать
    **durable до** звонков в НП (иначе работник в параллельной сессии не увидит
    `blocked` и создаст ТТН во время удаления), а сам снос — атомарным и только
    после успешной отмены всех ТТН. Поэтому функция коммитит сама.

    Фаза A (txn1): заблокировать строку аккаунта, перечитать ТТН. Активные
    (`dispatched`/`in_transit`/`arrived`/`returning`) и `returned` без оформленного
    возврата на склад → `ClientDeletionBlocked` без единого изменения. Иначе аккаунт
    → `blocked` и **commit** (команда заперта `_refuse_if_account_frozen`).

    Фаза B (txn2): отменить `created`/`confirmed` NP-first (идемпотентно,
    `NovaPoshtaNotFound` = успех). Ошибка НП → `ClientDeletionRetryable`, откат
    частичных флипов; заморозка из txn1 уцелела, повтор безопасен.

    Фаза C (та же txn2): закрыть обращения; удалить владельца и всех работников
    (членства, ФОП-профили с зашифрованными ключами НП, настройки уведомлений —
    каскадом); аккаунт → имя «Видалений клієнт», `archived`, ссылки на листы
    очищены. История (ТТН, склад, поддержка) остаётся: `client_id` → NULL (этап 2),
    `account_id` держит анонимную привязку, автор в UI — «Видалений користувач».

    Идемпотентно: повторный клик после успеха видит удалённого владельца или
    аккаунт-надгробие и возвращает `already_done` без изменений.
    """
    permissions.require_owner(actor, settings)
    users = UserRepository(session)
    accounts = ClientAccountRepository(session)

    user = await users.get_by_id(client_id)
    if user is None:
        # Уже удалён (напр. повторный клик после успеха) — идемпотентный no-op.
        return ClientDeletionResult(
            account_id=client_id, cancelled=0, team_removed=0, already_done=True
        )
    if user.role is not UserRole.client:
        raise ClientNotFound(str(client_id))
    membership = await accounts.get_membership(user_id=client_id)
    if membership is None or membership.role is not MembershipRole.account_owner:
        raise PermissionDenied("це працівник акаунта, а не головний клієнт")
    account = membership.account
    if account.status is ClientAccountStatus.archived and account.name == DELETED_CLIENT_NAME:
        return ClientDeletionResult(
            account_id=account.id, cancelled=0, team_removed=0, already_done=True
        )

    # --- Фаза A: гейт по ТТН + заморозка (txn1) ---
    await _lock_account(session, account.id)
    repo = ShipmentRepository(session)
    blocking = list(await _all_shipments(repo, account.id, _ACTIVE_SHIPMENT_STATUSES))
    for shipment in await _all_shipments(repo, account.id, {ShipmentStatus.returned}):
        # `returned` без движения `ttn_return` — возврат ещё не оформлен на склад
        # (менеджер должен его принять). С движением — завершён, не блокирует.
        if not await repo.movement_exists(shipment.id, StockMovementType.ttn_return):
            blocking.append(shipment)
    if blocking:
        raise ClientDeletionBlocked(
            [BlockingShipment(id=s.id, ttn_number=s.ttn_number, status=s.status) for s in blocking]
        )

    account.status = ClientAccountStatus.blocked
    await AuditRepository(session).log(
        "client_delete_started",
        user_id=actor.id,
        account_id=account.id,
        affected_entity=f"account:{account.id}",
        after={"status": ClientAccountStatus.blocked.value},
    )
    await session.flush()
    await session.commit()  # заморозка durable ДО звонков в НП — команда заперта

    # --- Фаза B: отмена невідправлених ТТН NP-first (txn2) ---
    await _lock_account(session, account.id)
    await session.refresh(account)
    if account.status is ClientAccountStatus.archived and account.name == DELETED_CLIENT_NAME:
        # Параллельный второй клик уже завершил снос — идемпотентно выходим.
        return ClientDeletionResult(
            account_id=account.id, cancelled=0, team_removed=0, already_done=True
        )
    unsent = await _all_shipments(repo, account.id, shipments.CANCELABLE_STATUSES)
    try:
        for shipment in unsent:
            await shipment_service.cancel_shipment_np_first(
                session,
                shipment=shipment,
                client=user,
                np_client=np_client,
                account_id=account.id,
                actor_user_id=user.id,
                sync=False,
            )
    except TtnCancelFailed as exc:
        await session.rollback()  # откат частичных флипов; заморозка (txn1) уцелела
        raise ClientDeletionRetryable(str(exc)) from exc

    # --- Фаза C: снос команды + анонимизация аккаунта (та же txn2) ---
    members, team_size = await accounts.list_members(account.id, limit=1000)
    audit = AuditRepository(session)
    await audit.log(
        "client_deleted",
        user_id=actor.id,
        account_id=account.id,
        affected_entity=f"account:{account.id}",
        after={
            "status": ClientAccountStatus.archived.value,
            "team_removed": team_size,
            "cancelled": len(unsent),
        },
    )
    await SupportRepository(session).close_open_for_account(account.id)
    for member in members:
        # ПИБ/телефон/Telegram вычищаем ДО удаления: после FK обнулится и payload'ы
        # с PII осиротеют неочищенными.
        await audit.scrub_user_pii(member.user_id)
    for member in members:
        await session.delete(member.user)  # членство, ФОП+ключи, настройки — каскадом
    account.name = DELETED_CLIENT_NAME
    account.status = ClientAccountStatus.archived
    account.stock_sheet_key = None
    account.stock_view_book_id = None
    await session.flush()
    await session.commit()
    return ClientDeletionResult(
        account_id=account.id, cancelled=len(unsent), team_removed=team_size, already_done=False
    )
