from dataclasses import dataclass, field

from app.bot.services import (
    BotServices,
    DevService,
    InMemoryDevState,
    build_effective_context,
)
from app.bot.types import DevSession
from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User


@dataclass(slots=True)
class FakeUserStore:
    users_by_telegram_id: dict[int, User] = field(default_factory=dict)
    phone_to_telegram_id: dict[str, int] = field(default_factory=dict)

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        return self.users_by_telegram_id.get(telegram_id)

    async def get_by_phone(self, phone: str) -> User | None:
        telegram_id = self.phone_to_telegram_id.get(phone)
        if telegram_id is None:
            return None
        return self.users_by_telegram_id.get(telegram_id)

    async def create_pending_client(self, telegram_id: int, phone: str, full_name: str) -> User:
        user = User(
            telegram_id=telegram_id,
            phone=phone,
            full_name=full_name,
            role=UserRole.client,
            status=UserStatus.pending,
            permissions={},
        )
        self.users_by_telegram_id[telegram_id] = user
        self.phone_to_telegram_id[phone] = telegram_id
        return user

    async def save(self, user: User) -> User:
        self.users_by_telegram_id[user.telegram_id] = user
        if user.phone:
            self.phone_to_telegram_id[user.phone] = user.telegram_id
        return user


@dataclass(slots=True)
class FakeAuditLog:
    records: list[tuple[str, int | None, dict[str, object]]] = field(default_factory=list)

    async def record(
        self, action: str, actor_user: User | None, payload: dict[str, object]
    ) -> None:
        self.records.append((action, actor_user.telegram_id if actor_user else None, payload))


def make_dev_service() -> DevService:
    store = FakeUserStore()
    for user in (
        User(
            telegram_id=1,
            phone="+380501112233",
            full_name="Dev 1",
            role=UserRole.client,
            status=UserStatus.active,
            permissions={},
        ),
        User(
            telegram_id=2,
            phone="+380671112233",
            full_name="Dev 2",
            role=UserRole.client,
            status=UserStatus.active,
            permissions={},
        ),
    ):
        store.users_by_telegram_id[user.telegram_id] = user
        if user.phone:
            store.phone_to_telegram_id[user.phone] = user.telegram_id

    services = BotServices(
        user_store=store,
        audit_log=FakeAuditLog(),
        dev_ids=frozenset({1, 2}),
        dev_state=InMemoryDevState(),
    )
    return DevService(services)


def test_is_dev_uses_injected_allowlist():
    service = make_dev_service()

    assert service.is_dev(1) is True
    assert service.is_dev(2) is True
    assert service.is_dev(999) is False


def test_effective_context_prefers_dev_override_and_impersonation():
    actor = User(
        telegram_id=1,
        phone="+380501112233",
        full_name="Dev",
        role=UserRole.client,
        status=UserStatus.active,
        permissions={},
    )
    target = User(
        telegram_id=99,
        phone="+380671234567",
        full_name="Owner",
        role=UserRole.owner,
        status=UserStatus.active,
        permissions={},
    )

    context = build_effective_context(
        actor_user=actor,
        impersonated_user=target,
        is_dev=True,
        dev_session=DevSession(role_override=UserRole.manager, impersonated_user_id=99),
    )

    assert context.is_dev is True
    assert context.effective_user == target
    assert context.effective_role is UserRole.manager
