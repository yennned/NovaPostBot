"""Типы bot-layer, которые не живут в data-layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.db.models.enums import UserRole
from app.db.models.user import User


@dataclass(slots=True)
class DevSession:
    role_override: UserRole | None = None
    impersonated_user_id: int | None = None


@dataclass(slots=True)
class KillSwitchRequest:
    requested_by: int
    requested_at: datetime
    expires_at: datetime
    confirmed_by: int | None = None


@dataclass(slots=True)
class KillSwitchStop:
    requested_by: int
    confirmed_by: int
    stopped_at: datetime
    cancel_until: datetime


@dataclass(slots=True)
class EffectiveContext:
    actor_user: User | None
    effective_user: User | None
    effective_role: UserRole | None
    is_dev: bool
    dev_session: DevSession | None = None
