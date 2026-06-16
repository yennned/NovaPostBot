"""Сервисы bot-layer поверх реальных репозиториев трека A."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.bot.types import DevSession, EffectiveContext, KillSwitchRequest, KillSwitchStop
from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User
from app.db.repositories import AuditRepository, UserRepository


class UserStore(Protocol):
    async def get_by_telegram_id(self, telegram_id: int) -> User | None: ...

    async def get_by_phone(self, phone: str) -> User | None: ...

    async def create_pending_client(self, telegram_id: int, phone: str, full_name: str) -> User: ...

    async def save(self, user: User) -> User: ...


class AuditLog(Protocol):
    async def record(
        self, action: str, actor_user: User | None, payload: dict[str, object]
    ) -> None: ...


@dataclass(slots=True)
class InMemoryDevState:
    sessions: dict[int, DevSession] = field(default_factory=dict)
    kill_switch_request: KillSwitchRequest | None = None
    kill_switch_stop: KillSwitchStop | None = None


@dataclass(slots=True)
class StartResult:
    user: User
    created: bool


@dataclass(slots=True)
class BotServices:
    user_store: UserStore
    audit_log: AuditLog
    dev_ids: frozenset[int]
    dev_state: InMemoryDevState


class RepositoryUserStore:
    def __init__(self, repo: UserRepository) -> None:
        self.repo = repo

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        return await self.repo.get_by_telegram_id(telegram_id)

    async def get_by_phone(self, phone: str) -> User | None:
        return await self.repo.get_by_phone(phone)

    async def create_pending_client(self, telegram_id: int, phone: str, full_name: str) -> User:
        return await self.repo.create(
            telegram_id=telegram_id,
            phone=phone,
            full_name=full_name,
            role=UserRole.client,
            status=UserStatus.pending,
        )

    async def save(self, user: User) -> User:
        await self.repo.session.flush()
        return user


class RepositoryAuditLog:
    def __init__(self, repo: AuditRepository) -> None:
        self.repo = repo

    async def record(
        self, action: str, actor_user: User | None, payload: dict[str, object]
    ) -> None:
        affected_entity = None
        target_id = payload.get("target_telegram_id")
        if isinstance(target_id, int):
            affected_entity = f"telegram:{target_id}"
        elif actor_user is not None and actor_user.id is not None:
            affected_entity = f"user:{actor_user.id}"

        await self.repo.log(
            action,
            user_id=actor_user.id if actor_user is not None else None,
            affected_entity=affected_entity,
            after=payload,
        )


class StartService:
    def __init__(self, user_store: UserStore) -> None:
        self.user_store = user_store

    async def register_contact(self, telegram_id: int, phone: str, full_name: str) -> StartResult:
        existing = await self.user_store.get_by_telegram_id(telegram_id)
        if existing is not None:
            if existing.phone != phone:
                existing.phone = phone
            if full_name and existing.full_name != full_name:
                existing.full_name = full_name
            await self.user_store.save(existing)
            return StartResult(user=existing, created=False)

        by_phone = await self.user_store.get_by_phone(phone)
        if by_phone is not None:
            by_phone.telegram_id = telegram_id
            if full_name:
                by_phone.full_name = full_name
            await self.user_store.save(by_phone)
            return StartResult(user=by_phone, created=False)

        user = await self.user_store.create_pending_client(telegram_id, phone, full_name)
        return StartResult(user=user, created=True)


class DevService:
    def __init__(self, services: BotServices) -> None:
        self.services = services

    def is_dev(self, telegram_id: int) -> bool:
        return telegram_id in self.services.dev_ids

    def get_session(self, telegram_id: int) -> DevSession:
        return self.services.dev_state.sessions.setdefault(telegram_id, DevSession())

    async def _actor_user(self, telegram_id: int) -> User | None:
        return await self.services.user_store.get_by_telegram_id(telegram_id)

    async def set_role(self, telegram_id: int, role: UserRole) -> DevSession:
        session = self.get_session(telegram_id)
        session.role_override = role
        session.impersonated_user_id = None
        await self.services.audit_log.record(
            "dev_as_role",
            await self._actor_user(telegram_id),
            {"role": role.value},
        )
        return session

    async def impersonate(self, telegram_id: int, target: User) -> DevSession:
        session = self.get_session(telegram_id)
        session.impersonated_user_id = target.telegram_id
        session.role_override = target.role
        await self.services.audit_log.record(
            "dev_impersonate",
            await self._actor_user(telegram_id),
            {"target_telegram_id": target.telegram_id, "target_role": target.role.value},
        )
        return session

    async def clear_context(self, telegram_id: int) -> None:
        self.services.dev_state.sessions[telegram_id] = DevSession()
        await self.services.audit_log.record(
            "dev_as_off",
            await self._actor_user(telegram_id),
            {},
        )

    async def request_kill_switch(
        self,
        telegram_id: int,
        *,
        now: datetime | None = None,
    ) -> KillSwitchRequest:
        now = now or datetime.now(UTC)
        self.expire_requests(now=now)
        if self.services.dev_state.kill_switch_stop is not None:
            raise ValueError("kill-switch уже активирован")

        request = self.services.dev_state.kill_switch_request
        if request is not None and request.requested_by != telegram_id:
            return request
        if request is not None and request.requested_by == telegram_id:
            raise ValueError("запрос уже создан этим dev")

        request = KillSwitchRequest(
            requested_by=telegram_id,
            requested_at=now,
            expires_at=now + timedelta(hours=1),
        )
        self.services.dev_state.kill_switch_request = request
        await self.services.audit_log.record(
            "dev_killswitch_request",
            await self._actor_user(telegram_id),
            {"expires_at": request.expires_at.isoformat()},
        )
        return request

    async def confirm_kill_switch(
        self,
        telegram_id: int,
        *,
        now: datetime | None = None,
    ) -> KillSwitchStop:
        now = now or datetime.now(UTC)
        self.expire_requests(now=now)
        request = self.services.dev_state.kill_switch_request
        if request is None:
            raise ValueError("активного запроса нет")
        if request.requested_by == telegram_id:
            raise ValueError("инициатор не может подтвердить сам")

        stop = KillSwitchStop(
            requested_by=request.requested_by,
            confirmed_by=telegram_id,
            stopped_at=now,
            cancel_until=now + timedelta(hours=3),
        )
        self.services.dev_state.kill_switch_request = None
        self.services.dev_state.kill_switch_stop = stop
        await self.services.audit_log.record(
            "dev_killswitch_stop",
            await self._actor_user(telegram_id),
            {
                "requested_by": stop.requested_by,
                "cancel_until": stop.cancel_until.isoformat(),
            },
        )
        return stop

    async def cancel_kill_switch(
        self,
        telegram_id: int,
        *,
        now: datetime | None = None,
    ) -> KillSwitchStop:
        now = now or datetime.now(UTC)
        stop = self.services.dev_state.kill_switch_stop
        if stop is None:
            raise ValueError("kill-switch не активирован")
        if now > stop.cancel_until:
            raise ValueError("окно отмены истекло")

        self.services.dev_state.kill_switch_stop = None
        await self.services.audit_log.record(
            "dev_killswitch_cancel",
            await self._actor_user(telegram_id),
            {"stopped_at": stop.stopped_at.isoformat()},
        )
        return stop

    def expire_requests(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        request = self.services.dev_state.kill_switch_request
        if request is not None and now > request.expires_at:
            self.services.dev_state.kill_switch_request = None


def build_effective_context(
    *,
    actor_user: User | None,
    impersonated_user: User | None,
    is_dev: bool,
    dev_session: DevSession | None,
) -> EffectiveContext:
    if not is_dev:
        return EffectiveContext(
            actor_user=actor_user,
            effective_user=actor_user,
            effective_role=actor_user.role if actor_user else None,
            is_dev=False,
        )

    effective_user = impersonated_user or actor_user
    effective_role = None
    if dev_session and dev_session.role_override is not None:
        effective_role = dev_session.role_override
    elif effective_user is not None:
        effective_role = effective_user.role

    return EffectiveContext(
        actor_user=actor_user,
        effective_user=effective_user,
        effective_role=effective_role,
        is_dev=True,
        dev_session=dev_session,
    )
