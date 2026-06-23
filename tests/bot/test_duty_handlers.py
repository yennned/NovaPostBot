"""Интеграционные тесты хендлера дежурства (Фаза 6) — на Postgres."""

from __future__ import annotations

from app.bot import permissions as perm
from app.bot.handlers.duty import open_shift
from app.bot.types import EffectiveContext
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import UserRepository
from sqlalchemy.ext.asyncio import AsyncSession


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[dict] = []

    async def answer(self, text, reply_markup=None, parse_mode=None) -> None:
        self.answers.append({"text": text, "reply_markup": reply_markup})


def _ctx(user, role: UserRole) -> EffectiveContext:
    return EffectiveContext(actor_user=user, effective_user=user, effective_role=role, is_dev=False)


async def test_open_shift_denies_manager_without_support_permission(db_session: AsyncSession):
    manager = await UserRepository(db_session).create(
        telegram_id=21,
        role=UserRole.manager,
        status=UserStatus.active,
        permissions={perm.CAN_HANDLE_SUPPORT: False},
    )
    msg = FakeMessage()

    await open_shift(msg, _ctx(manager, UserRole.manager), db_session)

    assert msg.answers
    assert "Чергування недоступне" in str(msg.answers[0]["text"])
    assert manager.on_duty is False
