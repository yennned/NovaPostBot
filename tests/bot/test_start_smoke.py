"""Smoke-тест хендлера /start — ловит рассинхрон контрактов enum/модели."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.bot.handlers.start import start_command
from app.bot.types import EffectiveContext
from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User


def _ctx(status: UserStatus) -> EffectiveContext:
    user = User(
        telegram_id=1,
        role=UserRole.client,
        status=status,
        permissions={},
    )
    return EffectiveContext(
        actor_user=user,
        effective_user=user,
        effective_role=user.role,
        is_dev=False,
        dev_session=None,
    )


@pytest.mark.parametrize(
    "status",
    [UserStatus.active, UserStatus.pending, UserStatus.blocked],
)
@pytest.mark.asyncio
async def test_start_command_runs_for_each_status(status: UserStatus) -> None:
    message = SimpleNamespace(
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=1, full_name="Step User"),
    )
    state = SimpleNamespace(set_state=AsyncMock(), clear=AsyncMock())

    await start_command(message, state, _ctx(status))

    message.answer.assert_awaited()
