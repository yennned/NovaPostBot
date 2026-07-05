from dataclasses import dataclass, field

from app.bot.services import StartService
from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User


@dataclass(slots=True)
class FakeUserStore:
    users_by_telegram_id: dict[int, User] = field(default_factory=dict)
    users_by_phone: dict[str, User] = field(default_factory=dict)

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        return self.users_by_telegram_id.get(telegram_id)

    async def get_by_phone(self, phone: str) -> User | None:
        return self.users_by_phone.get(phone)

    async def create_pending_client(self, telegram_id: int, phone: str, full_name: str) -> User:
        user = User(
            telegram_id=telegram_id,
            phone=phone,
            full_name=full_name,
            role=UserRole.client,
            status=UserStatus.pending,
            permissions={},
        )
        await self.save(user)
        return user

    async def save(self, user: User) -> User:
        if user.telegram_id is not None:
            self.users_by_telegram_id[user.telegram_id] = user
        if user.phone:
            self.users_by_phone[user.phone] = user
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


async def test_register_contact_adopts_precreated_manager_by_phone():
    """Менеджер, заведённый владельцем по телефону (telegram_id пуст), при первом
    входе подхватывается по номеру и получает telegram_id, сохраняя роль/статус."""
    store = FakeUserStore()
    precreated = User(
        telegram_id=None,
        phone="380509998877",  # формат НП, как хранит add_manager
        role=UserRole.manager,
        status=UserStatus.active,
        permissions={},
    )
    await store.save(precreated)
    service = StartService(store)

    result = await service.register_contact(
        telegram_id=303,
        phone="+380509998877",  # тот же номер, ненормализованный ввод контакта
        full_name="New Manager",
    )

    assert result.created is False
    assert result.user.telegram_id == 303
    assert result.user.role is UserRole.manager
    assert result.user.status is UserStatus.active
    assert result.user.full_name == "New Manager"
