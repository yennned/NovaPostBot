"""Тесты RBAC-ядра (`app/bot/permissions.py`) — чистая логика, без БД."""

from __future__ import annotations

import uuid

import pytest
from app.bot import permissions as perm
from app.config import get_settings
from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User


def _user(role: UserRole, telegram_id: int, permissions: dict | None = None) -> User:
    u = User(
        telegram_id=telegram_id,
        role=role,
        status=UserStatus.active,
        permissions=permissions or {},
    )
    u.id = uuid.uuid4()  # в памяти, без сессии
    return u


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("DEV_TELEGRAM_IDS", "900900")
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "100100")
    return get_settings()


def test_role_hierarchy_ranking():
    assert perm.role_at_least(UserRole.owner, UserRole.manager)
    assert perm.role_at_least(UserRole.manager, UserRole.client)
    assert not perm.role_at_least(UserRole.client, UserRole.manager)


def test_can_manage_top_down(settings):
    owner = _user(UserRole.owner, 1)
    manager = _user(UserRole.manager, 2)
    client = _user(UserRole.client, 3)

    assert perm.can_manage(owner, manager, settings)
    assert perm.can_manage(owner, client, settings)
    assert perm.can_manage(manager, client, settings)


def test_can_manage_forbidden_cases(settings):
    manager_a = _user(UserRole.manager, 2)
    manager_b = _user(UserRole.manager, 4)
    client = _user(UserRole.client, 3)
    owner = _user(UserRole.owner, 1)

    assert not perm.can_manage(manager_a, manager_b, settings)  # менеджеры не управляют друг другом
    assert not perm.can_manage(client, manager_a, settings)  # клиент никем
    assert not perm.can_manage(manager_a, owner, settings)  # снизу вверх нельзя
    assert not perm.can_manage(owner, owner, settings)  # собой нельзя


def test_dev_bypasses_hierarchy(settings):
    dev = _user(UserRole.client, 900900)  # роль неважна — он в allowlist
    owner = _user(UserRole.owner, 1)

    assert perm.is_dev(900900, settings)
    assert perm.can_manage(dev, owner, settings)  # dev управляет даже владельцем


def test_has_permission_manager_default_enabled(settings):
    manager = _user(UserRole.manager, 2)
    assert perm.has_permission(manager, "can_export_reports", settings)  # по умолчанию включено


def test_has_permission_manager_revoked(settings):
    manager = _user(UserRole.manager, 2, {"can_export_reports": False})
    assert not perm.has_permission(manager, "can_export_reports", settings)
    assert perm.has_permission(manager, "can_edit_clients", settings)  # другой флаг — включён


def test_has_permission_owner_and_dev_always_true(settings):
    owner = _user(UserRole.owner, 1, {"can_export_reports": False})
    dev = _user(UserRole.manager, 900900, {"can_export_reports": False})
    assert perm.has_permission(owner, "can_export_reports", settings)
    assert perm.has_permission(dev, "can_export_reports", settings)


def test_has_permission_client_denied(settings):
    client = _user(UserRole.client, 3)
    assert not perm.has_permission(client, "can_export_reports", settings)


def test_is_configured_owner(settings):
    assert perm.is_configured_owner(100100, settings)
    assert not perm.is_configured_owner(123, settings)
