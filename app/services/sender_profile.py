"""Сервис ФОП-профилей отправителя (Фаза 2, backend-ready) — без aiogram.

Управление `sender_profiles`: создание/список/правка/дефолт/удаление. Ключ НП
шифруется прозрачно (`EncryptedString` в модели) — сервис принимает plaintext и
наружу его НЕ отдаёт. **Валидация ключа в API НП здесь НЕ делается** — это Фаза 4
(создание ТТН), где ключ реально используется. UI появится тогда же; сейчас слой
готов к использованию.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import permissions
from app.config import Settings
from app.db.models.enums import OrgType, UserRole
from app.db.models.sender_profile import SenderProfile
from app.db.models.user import User
from app.db.repositories import AuditRepository, SenderProfileRepository
from app.services.exceptions import PermissionDenied, SenderProfileNotFound


@dataclass(frozen=True, slots=True)
class SenderProfileView:
    id: uuid.UUID
    client_id: uuid.UUID
    name: str
    org_type: OrgType
    edrpou: str | None
    sender_full_name: str | None
    sender_phone: str | None
    is_default: bool
    has_api_key: bool  # ключ НП есть/нет — само значение наружу не отдаём
    created_at: datetime


def _view(profile: SenderProfile) -> SenderProfileView:
    return SenderProfileView(
        id=profile.id,
        client_id=profile.client_id,
        name=profile.name,
        org_type=profile.org_type,
        edrpou=profile.edrpou,
        sender_full_name=profile.sender_full_name,
        sender_phone=profile.sender_phone,
        is_default=profile.is_default,
        has_api_key=bool(profile.np_api_key),
        created_at=profile.created_at,
    )


def _require_can_manage_profiles(
    actor: User, client_id: uuid.UUID, settings: Settings | None
) -> None:
    """ФОП клиента может вести сам клиент или персонал (manager+/dev)."""
    if permissions.is_dev(actor.telegram_id, settings):
        return
    if actor.id == client_id:
        return
    if not permissions.role_at_least(actor.role, UserRole.manager):
        raise PermissionDenied("нет прав управлять ФОП этого клиента")


async def _get_profile(repo: SenderProfileRepository, profile_id: uuid.UUID) -> SenderProfile:
    profile = await repo.get_by_id(profile_id)
    if profile is None:
        raise SenderProfileNotFound(str(profile_id))
    return profile


async def list_profiles(
    session: AsyncSession, *, actor: User, client_id: uuid.UUID, settings: Settings | None = None
) -> list[SenderProfileView]:
    _require_can_manage_profiles(actor, client_id, settings)
    repo = SenderProfileRepository(session)
    return [_view(p) for p in await repo.list_for_client(client_id)]


async def get_profile(
    session: AsyncSession, *, actor: User, profile_id: uuid.UUID, settings: Settings | None = None
) -> SenderProfileView:
    repo = SenderProfileRepository(session)
    profile = await _get_profile(repo, profile_id)
    _require_can_manage_profiles(actor, profile.client_id, settings)
    return _view(profile)


async def create_profile(
    session: AsyncSession,
    *,
    actor: User,
    client_id: uuid.UUID,
    name: str,
    np_api_key: str,
    org_type: OrgType = OrgType.fop,
    edrpou: str | None = None,
    sender_full_name: str | None = None,
    sender_phone: str | None = None,
    make_default: bool = False,
    settings: Settings | None = None,
) -> SenderProfileView:
    _require_can_manage_profiles(actor, client_id, settings)
    repo = SenderProfileRepository(session)
    # Первый профиль клиента делаем дефолтным автоматически.
    existing = await repo.list_for_client(client_id)
    is_default = make_default or not existing
    profile = await repo.create(
        client_id=client_id,
        name=name,
        np_api_key=np_api_key,
        org_type=org_type,
        edrpou=edrpou,
        sender_full_name=sender_full_name,
        sender_phone=sender_phone,
        is_default=is_default,
    )
    await AuditRepository(session).log(
        "sender_profile_created",
        user_id=actor.id,
        affected_entity=f"sender_profile:{profile.id}",
        after={"client_id": str(client_id), "name": name, "is_default": is_default},
    )
    return _view(profile)


async def update_profile(
    session: AsyncSession,
    *,
    actor: User,
    profile_id: uuid.UUID,
    settings: Settings | None = None,
    **fields: object,
) -> SenderProfileView:
    """Обновить поля профиля. Допустимые ключи — колонки `SenderProfile`
    (`name`, `np_api_key`, `org_type`, `edrpou`, `sender_full_name`,
    `sender_phone`). `is_default` менять только через `set_default`."""
    repo = SenderProfileRepository(session)
    profile = await _get_profile(repo, profile_id)
    _require_can_manage_profiles(actor, profile.client_id, settings)

    allowed = {"name", "np_api_key", "org_type", "edrpou", "sender_full_name", "sender_phone"}
    changes = {k: v for k, v in fields.items() if k in allowed}
    if changes:
        await repo.update(profile, **changes)
        await AuditRepository(session).log(
            "sender_profile_updated",
            user_id=actor.id,
            affected_entity=f"sender_profile:{profile.id}",
            after={k: ("***" if k == "np_api_key" else v) for k, v in changes.items()},
        )
    return _view(profile)


async def set_default(
    session: AsyncSession, *, actor: User, profile_id: uuid.UUID, settings: Settings | None = None
) -> SenderProfileView:
    repo = SenderProfileRepository(session)
    profile = await _get_profile(repo, profile_id)
    _require_can_manage_profiles(actor, profile.client_id, settings)
    await repo.set_default(profile)
    await AuditRepository(session).log(
        "sender_profile_set_default",
        user_id=actor.id,
        affected_entity=f"sender_profile:{profile.id}",
    )
    return _view(profile)
