from dataclasses import dataclass, field

from app.bot.services import StartService
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


async def test_register_contact_creates_pending_client():
    service = StartService(FakeUserStore())

    result = await service.register_contact(
        telegram_id=101,
        phone="+380501112233",
        full_name="Step User",
    )

    assert result.created is True
    assert result.user.role is UserRole.client
    assert result.user.status is UserStatus.pending


async def test_register_contact_keeps_existing_active_user():
    store = FakeUserStore()
    existing = User(
        telegram_id=202,
        phone="+380671234567",
        full_name="Manager",
        role=UserRole.manager,
        status=UserStatus.active,
        permissions={},
    )
    await store.save(existing)
    service = StartService(store)

    result = await service.register_contact(
        telegram_id=202,
        phone="+380671234567",
        full_name="Manager Updated",
    )

    assert result.created is False
    assert result.user.role is UserRole.manager
    assert result.user.status is UserStatus.active
    assert result.user.full_name == "Manager Updated"
