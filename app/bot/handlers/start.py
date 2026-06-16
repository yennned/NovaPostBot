"""Хендлеры `/start` и первичной авторизации."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.keyboards import build_contact_keyboard, build_role_menu
from app.bot.services import StartService
from app.bot.states import StartStates
from app.bot.texts import (
    ask_contact_text,
    blocked_text,
    contact_mismatch_text,
    dev_help_text,
    dev_mode_banner,
    pending_text,
    registered_pending_text,
    welcome_text,
)
from app.bot.types import EffectiveContext
from app.db.models.enums import UserStatus

router = Router(name="start")


async def _render_home(message: Message, context: EffectiveContext) -> None:
    if context.effective_role is None:
        await message.answer(dev_help_text())
        return

    banner = dev_mode_banner(
        context.effective_role,
        impersonated=context.effective_user is not None
        and context.actor_user is not None
        and context.effective_user.telegram_id != context.actor_user.telegram_id,
    )
    parts: list[str] = []
    if context.is_dev:
        parts.append(banner)
    if context.effective_user is not None:
        parts.append(welcome_text(context.effective_user, context.effective_role))
    else:
        parts.append(f"Відкриваю меню {context.effective_role.value}.")

    await message.answer(
        "\n".join(parts),
        reply_markup=build_role_menu(context.effective_role),
    )


@router.message(CommandStart())
async def start_command(
    message: Message,
    state: FSMContext,
    effective_context: EffectiveContext,
) -> None:
    if effective_context.is_dev and (
        effective_context.effective_role is not None or effective_context.effective_user is not None
    ):
        await _render_home(message, effective_context)
        return

    user = effective_context.actor_user
    if user is None:
        await state.set_state(StartStates.waiting_for_contact)
        await message.answer(ask_contact_text(), reply_markup=build_contact_keyboard())
        return

    if user.status is UserStatus.blocked:
        await message.answer(blocked_text())
        return

    if user.status is UserStatus.pending:
        await message.answer(pending_text(user), reply_markup=build_contact_keyboard())
        return

    role = effective_context.effective_role or user.role
    await message.answer(welcome_text(user, role), reply_markup=build_role_menu(role))


@router.message(StartStates.waiting_for_contact, F.contact)
@router.message(F.contact)
async def receive_contact(
    message: Message,
    state: FSMContext,
    start_service: StartService,
) -> None:
    if message.from_user is None or message.contact is None:
        return

    if message.contact.user_id != message.from_user.id:
        await message.answer(contact_mismatch_text(), reply_markup=build_contact_keyboard())
        return

    full_name = message.from_user.full_name
    result = await start_service.register_contact(
        telegram_id=message.from_user.id,
        phone=message.contact.phone_number,
        full_name=full_name,
    )
    await state.clear()

    if result.user.status is UserStatus.blocked:
        await message.answer(blocked_text())
        return

    if result.user.status is UserStatus.active:
        await message.answer(
            welcome_text(result.user, result.user.role),
            reply_markup=build_role_menu(result.user.role),
        )
        return

    text = registered_pending_text(result.user) if result.created else pending_text(result.user)
    await message.answer(text, reply_markup=build_contact_keyboard())
