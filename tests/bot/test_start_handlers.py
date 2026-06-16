from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from app.bot.handlers.start import receive_contact, start_command
from app.bot.services import StartResult
from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User


@dataclass
class FakeState:
    state: object | None = None
    cleared: bool = False

    async def set_state(self, value: object) -> None:
        self.state = value

    async def clear(self) -> None:
        self.cleared = True


class FakeMessage:
    def __init__(self, from_user, contact=None, text="/start") -> None:
        self.from_user = from_user
        self.contact = contact
        self.text = text
        self.answers: list[dict[str, object]] = []

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answers.append({"text": text, "reply_markup": reply_markup})


class FakeStartService:
    def __init__(self, result: StartResult) -> None:
        self.result = result

    async def register_contact(self, telegram_id: int, phone: str, full_name: str) -> StartResult:
        assert telegram_id
        assert phone
        assert full_name
        return self.result


def make_user(*, status: UserStatus, role: UserRole = UserRole.client) -> User:
    return User(
        telegram_id=123,
        phone="+380501112233",
        full_name="Step User",
        role=role,
        status=status,
        permissions={},
    )


@pytest.mark.asyncio
async def test_start_command_handles_blocked_user_status() -> None:
    message = FakeMessage(SimpleNamespace(id=123, full_name="Step User"))
    state = FakeState()
    context = SimpleNamespace(
        is_dev=False,
        effective_role=None,
        effective_user=None,
        actor_user=make_user(status=UserStatus.blocked),
    )

    await start_command(message, state, context)

    assert message.answers
    assert "заблоковано" in str(message.answers[0]["text"])


@pytest.mark.asyncio
async def test_receive_contact_handles_active_user_status() -> None:
    active_user = make_user(status=UserStatus.active, role=UserRole.manager)
    message = FakeMessage(
        SimpleNamespace(id=123, full_name="Step User"),
        contact=SimpleNamespace(user_id=123, phone_number="+380501112233"),
    )
    state = FakeState()
    service = FakeStartService(StartResult(user=active_user, created=False))

    await receive_contact(message, state, service)

    assert state.cleared is True
    assert message.answers
    assert "Вітаю" in str(message.answers[0]["text"])
