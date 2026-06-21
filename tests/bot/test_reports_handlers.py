"""Интеграционные тесты хендлеров «📊 Звіти» / «📈 Аналітика» (Фаза 6) — на Postgres."""

from __future__ import annotations

from app.bot.handlers.analytics import open_analytics
from app.bot.handlers.reports import open_reports
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


async def test_open_reports_for_manager(db_session: AsyncSession):
    manager = await UserRepository(db_session).create(
        telegram_id=10, role=UserRole.manager, status=UserStatus.active
    )
    msg = FakeMessage()
    await open_reports(msg, _ctx(manager, UserRole.manager), db_session)
    assert msg.answers
    assert "Звіт" in str(msg.answers[0]["text"])
    assert msg.answers[0]["reply_markup"] is not None  # период-переключатель


async def test_open_analytics_for_owner(db_session: AsyncSession):
    owner = await UserRepository(db_session).create(
        telegram_id=1, role=UserRole.owner, status=UserStatus.active
    )
    msg = FakeMessage()
    await open_analytics(msg, _ctx(owner, UserRole.owner), db_session)
    assert msg.answers
    text = str(msg.answers[0]["text"])
    assert "Фінанси" in text  # аналитика включает финотчёт
