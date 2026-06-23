"""Хендлер дежурства: кнопка «🟢 Я на зв'язку» (Фаза 6).

Открывает смену менеджера/владельца — утренняя авторизация на день. `/start`
лишь показывает меню; смену открывает только эта кнопка. Снимается смена
автоматически воркером при закрытии отделения (`app/jobs.clear_expired_duty_job`).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import permissions
from app.bot.texts import duty as texts
from app.bot.types import EffectiveContext
from app.db.models.enums import UserRole, UserStatus
from app.services import duty
from app.services.exceptions import ClientServiceError, OfficeClosed

router = Router(name="duty")

DUTY_BUTTON = "🟢 Я на зв'язку"


def _is_staff(context: EffectiveContext) -> bool:
    return context.effective_role in {UserRole.manager, UserRole.owner} or context.is_dev


def _can_handle_support(context: EffectiveContext) -> bool:
    user = context.effective_user
    if user is None:
        return False
    if context.is_dev:
        return True
    if user.status is not UserStatus.active:
        return False
    if context.effective_role is UserRole.owner:
        return True
    if context.effective_role is UserRole.manager:
        return permissions.has_permission(user, permissions.CAN_HANDLE_SUPPORT)
    return False


@router.message(F.text == DUTY_BUTTON)
async def open_shift(
    message: Message,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    ) -> None:
    if not _is_staff(effective_context):
        raise SkipHandler()
    if not _can_handle_support(effective_context):
        await message.answer(texts.duty_unavailable_text())
        return
    user = effective_context.effective_user
    if user is None:
        await message.answer(texts.not_staff_text())
        return
    try:
        result = await duty.go_on_duty(db_session, user=user)
    except OfficeClosed as exc:
        await message.answer(texts.office_closed_text(exc))
        return
    except ClientServiceError as exc:
        await message.answer(str(exc))
        return
    await message.answer(texts.on_duty_text(result))
