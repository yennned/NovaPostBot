"""Сервис ФОП-профилей отправителя — без aiogram.

Управление `sender_profiles`: создание/список/правка/дефолт/удаление. Ключ НП
шифруется прозрачно (`EncryptedString` в модели) — сервис принимает plaintext и
наружу его НЕ отдаёт.

**Фаза 4:** при заданном `np_client` ключ ФОП валидируется в API НП **до**
сохранения; успех подтягивает Ref контрагента-отправителя/контакта в профиль
(`np_sender_ref`/`np_contact_ref`), склад-отправитель — из конфига. Невалидный
ключ → `SenderProfileKeyInvalid`, профиль не сохраняется. Без `np_client`
(напр. в части тестов) валидация пропускается — профиль остаётся «непровалидирован»
(`is_np_validated=False`), создание ТТН такой ФОП не пропустит (PR 6).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import permissions
from app.config import Settings, get_settings
from app.db.models.enums import OrgType, UserRole, UserStatus
from app.db.models.sender_profile import SenderProfile
from app.db.models.user import User
from app.db.repositories import AuditRepository, SenderProfileRepository, UserRepository
from app.novaposhta import methods
from app.novaposhta.client import NovaPoshtaClient
from app.novaposhta.exceptions import NovaPoshtaAuthError, NovaPoshtaValidationError
from app.services.client_sheet_sync import sync_client_sheets
from app.services.exceptions import (
    PermissionDenied,
    SenderProfileIncomplete,
    SenderProfileKeyInvalid,
    SenderProfileNotFound,
)


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
    is_np_validated: bool  # ключ проверен в НП, Ref отправителя подтянут
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
        is_np_validated=bool(profile.np_sender_ref),
        created_at=profile.created_at,
    )


async def _resolve_sender_refs(
    np_client: NovaPoshtaClient, api_key: str, settings: Settings | None
) -> dict[str, str | None]:
    """Проверить ключ в НП и собрать Ref-поля отправителя для сохранения.

    `NovaPoshtaAuthError` (плохой ключ) и `NovaPoshtaValidationError` (ключ есть,
    но контрагент-отправитель не настроен) → `SenderProfileKeyInvalid`. Транзитный
    `NovaPoshtaUnavailable` НЕ ловим — пробрасываем (ключ не виноват).
    """
    try:
        validation = await methods.validate_key_and_get_sender(np_client, api_key=api_key)
    except (NovaPoshtaAuthError, NovaPoshtaValidationError) as exc:
        raise SenderProfileKeyInvalid(str(exc)) from exc
    refs: dict[str, str | None] = {
        "np_sender_ref": validation.counterparty_ref,
        "np_contact_ref": validation.contact_ref,
    }
    # Склад-отправитель — из конфига. Кладём только если задан, иначе не
    # затёрли бы ранее сохранённый при правке одного лишь ключа.
    warehouse = (settings or get_settings()).np_sender_warehouse_ref
    if warehouse:
        refs["np_sender_warehouse"] = warehouse
    return refs


def _require_can_manage_profiles(
    actor: User, client_id: uuid.UUID, settings: Settings | None
) -> None:
    """ФОП клиента может вести сам клиент или персонал (manager+/dev)."""
    if permissions.is_dev(actor.telegram_id, settings):
        return
    if actor.id == client_id:
        if actor.role is not UserRole.client:
            raise PermissionDenied("нет прав управлять ФОП этого клиента")
        if actor.status is not UserStatus.active:
            raise PermissionDenied("налаштування ФОП доступні після підтвердження")
        return
    if actor.status is not UserStatus.active:
        raise PermissionDenied("учётная запись неактивна")
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
    np_client: NovaPoshtaClient | None = None,
    settings: Settings | None = None,
) -> SenderProfileView:
    _require_can_manage_profiles(actor, client_id, settings)
    # Телефон отправителя обязателен: он уходит в НП как `SendersPhone` при создании
    # ТТН. Требуем его уже на сохранении, чтобы не возникало состояние «ключ валиден,
    # но телефона нет» (тогда `create_shipment` отбил бы ТТН гейтом отправителя).
    sender_phone = (sender_phone or "").strip()
    if not sender_phone:
        raise SenderProfileIncomplete("телефон відправника обовʼязковий")
    # Валидируем ключ ДО записи: плохой ключ → исключение, профиль не создаётся.
    refs = await _resolve_sender_refs(np_client, np_api_key, settings) if np_client else {}
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
        **refs,
    )
    await AuditRepository(session).log(
        "sender_profile_created",
        user_id=actor.id,
        affected_entity=f"sender_profile:{profile.id}",
        after={
            "client_id": str(client_id),
            "name": name,
            "is_default": is_default,
            "np_validated": bool(refs),
        },
    )
    return _view(profile)


async def update_profile(
    session: AsyncSession,
    *,
    actor: User,
    profile_id: uuid.UUID,
    np_client: NovaPoshtaClient | None = None,
    settings: Settings | None = None,
    **fields: object,
) -> SenderProfileView:
    """Обновить поля профиля. Допустимые ключи — колонки `SenderProfile`
    (`name`, `np_api_key`, `org_type`, `edrpou`, `sender_full_name`,
    `sender_phone`). `is_default` менять только через `set_default`.

    При смене `np_api_key` и заданном `np_client` новый ключ валидируется в НП до
    записи; успех обновляет Ref'ы отправителя, плохой → `SenderProfileKeyInvalid`.
    """
    repo = SenderProfileRepository(session)
    profile = await _get_profile(repo, profile_id)
    _require_can_manage_profiles(actor, profile.client_id, settings)

    allowed = {"name", "np_api_key", "org_type", "edrpou", "sender_full_name", "sender_phone"}
    changes = {k: v for k, v in fields.items() if k in allowed}
    if "np_api_key" in changes:
        new_key = str(changes["np_api_key"]).strip()
        if not new_key:  # очистка ключа — не поддерживается (ФОП без ключа бесполезен)
            raise SenderProfileKeyInvalid("порожній ключ ФОП")
        if np_client is not None:
            # Валидируем новый ключ ДО записи; Ref'ы перезаписываем.
            changes.update(await _resolve_sender_refs(np_client, new_key, settings))
    if "sender_phone" in changes:
        # Телефон обязателен (см. create_profile) — очистить его нельзя.
        phone = changes["sender_phone"]
        if phone is None or not str(phone).strip():
            raise SenderProfileIncomplete("телефон відправника не можна очистити")
        changes["sender_phone"] = str(phone).strip()
    if changes:
        await repo.update(profile, **changes)
        await AuditRepository(session).log(
            "sender_profile_updated",
            user_id=actor.id,
            affected_entity=f"sender_profile:{profile.id}",
            after={k: ("***" if k == "np_api_key" else v) for k, v in changes.items()},
        )
        if "name" in changes:
            client = await UserRepository(session).get_by_id(profile.client_id)
            if client is not None:
                await sync_client_sheets(session, client=client)
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
