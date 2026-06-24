"""Тесты сервиса ФОП-профилей (`app/services/sender_profile.py`) — на Postgres."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from app.config import Settings
from app.db.models.enums import OrgType, UserRole, UserStatus
from app.db.repositories import UserRepository
from app.novaposhta.client import NovaPoshtaClient
from app.services import sender_profile as sp
from app.services.exceptions import (
    PermissionDenied,
    SenderProfileIncomplete,
    SenderProfileKeyInvalid,
    SenderProfileNotFound,
)
from sqlalchemy.ext.asyncio import AsyncSession

_PHONE = "+380501112233"  # телефон отправителя обязателен при создании профиля


async def _user(
    session: AsyncSession,
    telegram_id: int,
    role=UserRole.client,
    status: UserStatus = UserStatus.active,
):
    return await UserRepository(session).create(telegram_id=telegram_id, role=role, status=status)


def _np_client(routes: dict[tuple[str, str], object], **settings_over) -> NovaPoshtaClient:
    """NovaPoshtaClient на MockTransport, диспатчащий по (modelName, calledMethod)."""
    settings = Settings(_env_file=None)
    settings.np_retry_backoff = 0.0
    for key, value in settings_over.items():
        setattr(settings, key, value)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        result = routes[(body["modelName"], body["calledMethod"])]
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(
            200, json={"success": True, "data": result, "errors": [], "errorCodes": []}
        )

    return NovaPoshtaClient(settings=settings, transport=httpx.MockTransport(handler))


_VALID_KEY_ROUTES = {
    ("Counterparty", "getCounterparties"): [{"Ref": "sender-cp"}],
    ("Counterparty", "getCounterpartyContactPersons"): [{"Ref": "sender-contact"}],
}


async def test_create_first_profile_is_default(db_session: AsyncSession):
    client = await _user(db_session, 100)
    view = await sp.create_profile(
        db_session,
        actor=client,
        client_id=client.id,
        name="ФОП Іванов",
        np_api_key="np-key-1",
        sender_phone=_PHONE,
    )
    assert view.is_default is True
    assert view.has_api_key is True
    assert view.org_type is OrgType.fop
    # ключ наружу не отдаётся
    assert not hasattr(view, "np_api_key")


async def test_second_profile_and_set_default(db_session: AsyncSession):
    client = await _user(db_session, 101)
    first = await sp.create_profile(
        db_session,
        actor=client,
        client_id=client.id,
        name="ФОП-1",
        np_api_key="k1",
        sender_phone=_PHONE,
    )
    second = await sp.create_profile(
        db_session,
        actor=client,
        client_id=client.id,
        name="ФОП-2",
        np_api_key="k2",
        sender_phone=_PHONE,
    )
    assert first.is_default is True
    assert second.is_default is False

    promoted = await sp.set_default(db_session, actor=client, profile_id=second.id)
    assert promoted.is_default is True
    views = {v.id: v for v in await sp.list_profiles(db_session, actor=client, client_id=client.id)}
    assert views[second.id].is_default is True
    assert views[first.id].is_default is False


async def test_manager_can_manage_client_profiles(db_session: AsyncSession):
    client = await _user(db_session, 102)
    manager = await _user(db_session, 9, role=UserRole.manager)
    view = await sp.create_profile(
        db_session,
        actor=manager,
        client_id=client.id,
        name="ФОП",
        np_api_key="k",
        sender_phone=_PHONE,
    )
    assert view.client_id == client.id


async def test_foreign_client_denied(db_session: AsyncSession):
    client = await _user(db_session, 103)
    other = await _user(db_session, 104)
    with pytest.raises(PermissionDenied):
        await sp.create_profile(
            db_session,
            actor=other,
            client_id=client.id,
            name="ФОП",
            np_api_key="k",
            sender_phone=_PHONE,
        )


async def test_blocked_client_cannot_manage_own_profiles(db_session: AsyncSession):
    client = await _user(db_session, 110, status=UserStatus.blocked)
    with pytest.raises(PermissionDenied):
        await sp.create_profile(
            db_session,
            actor=client,
            client_id=client.id,
            name="ФОП",
            np_api_key="k",
            sender_phone=_PHONE,
        )


async def test_blocked_manager_cannot_manage_client_profiles(db_session: AsyncSession):
    client = await _user(db_session, 111)
    manager = await _user(db_session, 112, role=UserRole.manager, status=UserStatus.blocked)
    with pytest.raises(PermissionDenied):
        await sp.create_profile(
            db_session,
            actor=manager,
            client_id=client.id,
            name="ФОП",
            np_api_key="k",
            sender_phone=_PHONE,
        )


async def test_create_without_phone_rejected(db_session: AsyncSession):
    client = await _user(db_session, 113)
    # телефон отправителя обязателен — без него профиль не создаётся
    with pytest.raises(SenderProfileIncomplete):
        await sp.create_profile(
            db_session, actor=client, client_id=client.id, name="ФОП", np_api_key="k"
        )
    assert await sp.list_profiles(db_session, actor=client, client_id=client.id) == []


async def test_update_clearing_phone_rejected(db_session: AsyncSession):
    client = await _user(db_session, 114)
    created = await sp.create_profile(
        db_session,
        actor=client,
        client_id=client.id,
        name="ФОП",
        np_api_key="k",
        sender_phone=_PHONE,
    )
    with pytest.raises(SenderProfileIncomplete):
        await sp.update_profile(db_session, actor=client, profile_id=created.id, sender_phone="  ")


async def test_update_masks_api_key_in_audit(db_session: AsyncSession):
    from app.db.models.audit import AuditLog
    from sqlalchemy import select

    client = await _user(db_session, 105)
    created = await sp.create_profile(
        db_session,
        actor=client,
        client_id=client.id,
        name="Старе",
        np_api_key="k",
        sender_phone=_PHONE,
    )
    await sp.update_profile(
        db_session, actor=client, profile_id=created.id, name="Нове", np_api_key="new-key"
    )
    entry = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "sender_profile_updated")
    )
    assert entry.after["name"] == "Нове"
    assert entry.after["np_api_key"] == "***"  # секрет в аудит не пишем


async def test_profile_not_found(db_session: AsyncSession):
    client = await _user(db_session, 106)
    with pytest.raises(SenderProfileNotFound):
        await sp.get_profile(db_session, actor=client, profile_id=uuid.uuid4())


async def test_create_with_np_client_validates_and_fills_refs(db_session: AsyncSession):
    client = await _user(db_session, 107)
    settings = Settings(_env_file=None)
    settings.np_sender_warehouse_ref = "wh-ref"
    view = await sp.create_profile(
        db_session,
        actor=client,
        client_id=client.id,
        name="ФОП",
        np_api_key="good-key",
        sender_phone=_PHONE,
        np_client=_np_client(_VALID_KEY_ROUTES),
        settings=settings,
    )
    assert view.is_np_validated is True
    # Ref'ы отправителя осели в БД
    profiles = await sp.SenderProfileRepository(db_session).list_for_client(client.id)
    assert profiles[0].np_sender_ref == "sender-cp"
    assert profiles[0].np_contact_ref == "sender-contact"
    assert profiles[0].np_sender_warehouse == "wh-ref"  # из конфига


async def test_create_with_invalid_key_raises_and_creates_no_row(db_session: AsyncSession):
    client = await _user(db_session, 108)
    bad = _np_client(
        {
            ("Counterparty", "getCounterparties"): httpx.Response(
                200,
                json={"success": False, "data": [], "errors": [], "errorCodes": ["20000200068"]},
            )
        }
    )
    with pytest.raises(SenderProfileKeyInvalid):
        await sp.create_profile(
            db_session,
            actor=client,
            client_id=client.id,
            name="ФОП",
            np_api_key="bad-key",
            sender_phone=_PHONE,
            np_client=bad,
        )
    # профиль не создан (валидация до записи)
    assert await sp.list_profiles(db_session, actor=client, client_id=client.id) == []


async def test_create_without_np_client_skips_validation(db_session: AsyncSession):
    client = await _user(db_session, 109)
    view = await sp.create_profile(
        db_session,
        actor=client,
        client_id=client.id,
        name="ФОП",
        np_api_key="k",
        sender_phone=_PHONE,
    )
    assert view.is_np_validated is False  # ключ не валидировали — Ref'ов нет


async def test_update_key_revalidates_and_updates_refs(db_session: AsyncSession):
    client = await _user(db_session, 110)
    created = await sp.create_profile(
        db_session,
        actor=client,
        client_id=client.id,
        name="ФОП",
        np_api_key="k",
        sender_phone=_PHONE,
    )
    assert created.is_np_validated is False
    updated = await sp.update_profile(
        db_session,
        actor=client,
        profile_id=created.id,
        np_client=_np_client(_VALID_KEY_ROUTES),
        np_api_key="new-good-key",
    )
    assert updated.is_np_validated is True
    profiles = await sp.SenderProfileRepository(db_session).list_for_client(client.id)
    assert profiles[0].np_sender_ref == "sender-cp"


async def test_key_only_update_keeps_existing_warehouse(db_session: AsyncSession):
    client = await _user(db_session, 111)
    with_wh = Settings(_env_file=None)
    with_wh.np_sender_warehouse_ref = "wh-ref"
    created = await sp.create_profile(
        db_session,
        actor=client,
        client_id=client.id,
        name="ФОП",
        np_api_key="k1",
        sender_phone=_PHONE,
        np_client=_np_client(_VALID_KEY_ROUTES),
        settings=with_wh,
    )
    repo = sp.SenderProfileRepository(db_session)
    assert (await repo.get_by_id(created.id)).np_sender_warehouse == "wh-ref"

    # ротация ключа без склада в конфиге не должна обнулить склад
    no_wh = Settings(_env_file=None)  # np_sender_warehouse_ref = ""
    await sp.update_profile(
        db_session,
        actor=client,
        profile_id=created.id,
        np_client=_np_client(_VALID_KEY_ROUTES),
        settings=no_wh,
        np_api_key="k2",
    )
    assert (await repo.get_by_id(created.id)).np_sender_warehouse == "wh-ref"  # сохранён


async def test_update_empty_key_rejected(db_session: AsyncSession):
    client = await _user(db_session, 112)
    created = await sp.create_profile(
        db_session,
        actor=client,
        client_id=client.id,
        name="ФОП",
        np_api_key="k",
        sender_phone=_PHONE,
    )
    with pytest.raises(SenderProfileKeyInvalid):
        await sp.update_profile(db_session, actor=client, profile_id=created.id, np_api_key="  ")
