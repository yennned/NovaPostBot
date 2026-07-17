"""Команда клієнтського акаунта: запрошення, блокування та відновлення."""

from __future__ import annotations

import uuid

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import account_team as kb
from app.bot.keyboards.menus import MENU_TEXTS
from app.bot.states import AccountTeamState
from app.bot.texts import account_team as texts
from app.bot.types import EffectiveContext
from app.db.models.enums import MembershipStatus
from app.services import account_team
from app.services.exceptions import ClientServiceError

router = Router(name="account_team")
TEAM_BUTTON = "👥 Команда"


async def _render(
    message: Message, context: EffectiveContext, session: AsyncSession, *, offset: int = 0
) -> None:
    if context.account_context is None:
        await message.answer("Команда доступна лише головному клієнту.")
        return
    try:
        items, total = await account_team.list_team(
            session, context=context.account_context, offset=offset, limit=8
        )
    except ClientServiceError as exc:
        await message.answer(str(exc))
        return
    await message.answer(
        texts.team_list_text(total),
        reply_markup=kb.build_team_kb(items, offset=offset, total=total, limit=8),
        parse_mode="HTML",
    )


@router.message(F.text == TEAM_BUTTON)
async def open_team(
    message: Message, effective_context: EffectiveContext, db_session: AsyncSession
) -> None:
    await _render(message, effective_context, db_session)


@router.callback_query(F.data.startswith("team:list:"))
async def team_page(
    callback: CallbackQuery, effective_context: EffectiveContext, db_session: AsyncSession
) -> None:
    if callback.message is None:
        return
    try:
        offset = max(0, int(callback.data.split(":")[2]))
    except (IndexError, ValueError):
        await callback.answer("Кнопка застаріла", show_alert=True)
        return
    await _render(callback.message, effective_context, db_session, offset=offset)
    await callback.answer()


@router.callback_query(F.data == "team:invite")
async def invite_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        return
    await state.set_state(AccountTeamState.waiting_for_phone)
    await callback.message.answer(
        "Введіть номер телефону працівника у форматі 0XXXXXXXXX або +380XXXXXXXXX."
    )
    await callback.answer()


@router.callback_query(F.data.startswith("team:view:"))
async def view_member(
    callback: CallbackQuery, effective_context: EffectiveContext, db_session: AsyncSession
) -> None:
    if callback.message is None or effective_context.account_context is None:
        return
    try:
        user_id = uuid.UUID(callback.data.split(":")[2])
        item = await account_team.get_member(
            db_session, context=effective_context.account_context, user_id=user_id
        )
    except (ValueError, ClientServiceError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.message.edit_text(
        texts.member_card_text(item),
        reply_markup=kb.build_member_kb(item),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(
    AccountTeamState.waiting_for_phone,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(MENU_TEXTS),
)
async def invite_submit(
    message: Message,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    if effective_context.account_context is None:
        await state.clear()
        await message.answer("Команда доступна лише головному клієнту.")
        return
    try:
        member = await account_team.invite_employee(
            db_session, context=effective_context.account_context, phone=message.text
        )
    except ClientServiceError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await message.answer(texts.invite_result_text(member))


async def _mutate(
    callback: CallbackQuery,
    context: EffectiveContext,
    session: AsyncSession,
    *,
    status: MembershipStatus,
) -> None:
    if callback.message is None or context.account_context is None:
        return
    try:
        user_id = uuid.UUID(callback.data.split(":")[2])
        action = (
            account_team.block_employee
            if status is MembershipStatus.blocked
            else account_team.restore_employee
        )
        item = await action(session, context=context.account_context, user_id=user_id)
    except (ValueError, ClientServiceError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.message.edit_text(
        texts.member_card_text(item, with_phone=False),
        reply_markup=kb.build_member_kb(item),
        parse_mode="HTML",
    )
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("team:block:"))
async def block(
    callback: CallbackQuery, effective_context: EffectiveContext, db_session: AsyncSession
) -> None:
    await _mutate(callback, effective_context, db_session, status=MembershipStatus.blocked)


@router.callback_query(F.data.startswith("team:restore:"))
async def restore(
    callback: CallbackQuery, effective_context: EffectiveContext, db_session: AsyncSession
) -> None:
    await _mutate(callback, effective_context, db_session, status=MembershipStatus.active)
