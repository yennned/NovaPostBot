"""Хендлеры управления персоналом (👔 Персонал, owner-only, Фаза 6).

Владелец/dev: список менеджеров, карточка с per-flag правами (тоглы из реестра
`permissions.PERMISSION_FLAGS`), найм по телефону/Telegram-ID и удаление
менеджера. Все мутации идут через `services/staff` (RBAC + audit).
"""

from __future__ import annotations

import uuid

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import staff as kb
from app.bot.notify import BotNotifier
from app.bot.states import StaffState
from app.bot.texts import staff as texts
from app.bot.types import EffectiveContext
from app.db.models.enums import UserRole
from app.services import notifications, staff
from app.services.exceptions import ClientServiceError, StaffNotFound
from app.utils.phone import normalize_phone

router = Router(name="staff")

STAFF_BUTTON = "👔 Персонал"
_STALE = "Кнопка застаріла, відкрийте розділ заново."


def _is_owner(ctx: EffectiveContext) -> bool:
    return ctx.effective_role is UserRole.owner or ctx.is_dev


def _actor(ctx: EffectiveContext):
    return ctx.effective_user


def _parse_id(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        return None


async def _render_list(
    target: Message,
    session: AsyncSession,
    ctx: EffectiveContext,
    *,
    offset: int,
    query: str | None,
    edit: bool,
) -> None:
    page = await staff.list_staff(
        session, actor=_actor(ctx), query=query, limit=kb.PAGE_SIZE, offset=offset
    )
    text, markup = texts.list_text(page), kb.build_list_kb(page)
    if edit:
        await target.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=markup, parse_mode="HTML")


async def _render_card(
    target: Message, session: AsyncSession, ctx: EffectiveContext, manager_id: uuid.UUID
) -> None:
    card = await staff.get_staff_card(session, actor=_actor(ctx), manager_id=manager_id)
    await target.edit_text(
        texts.card_text(card), reply_markup=kb.build_card_kb(card), parse_mode="HTML"
    )


@router.message(F.text == STAFF_BUTTON)
async def staff_open(
    message: Message,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if not _is_owner(effective_context):
        raise SkipHandler()
    await state.clear()
    await _render_list(message, db_session, effective_context, offset=0, query=None, edit=False)


@router.callback_query(F.data.startswith("stf:list:"))
async def cb_list(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None or not _is_owner(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        offset = int(callback.data.split(":")[2])
    except (ValueError, IndexError):
        await callback.answer(_STALE, show_alert=True)
        return
    await state.clear()
    await _render_list(
        callback.message, db_session, effective_context, offset=offset, query=None, edit=True
    )
    await callback.answer()


@router.callback_query(F.data.startswith("stf:card:"))
async def cb_card(
    callback: CallbackQuery, effective_context: EffectiveContext, db_session: AsyncSession
) -> None:
    if callback.message is None or not _is_owner(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    manager_id = _parse_id(callback.data.split(":")[-1])
    if manager_id is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        await _render_card(callback.message, db_session, effective_context, manager_id)
    except StaffNotFound:
        await callback.answer(_STALE, show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("stf:flag:"))
async def cb_flag(
    callback: CallbackQuery, effective_context: EffectiveContext, db_session: AsyncSession
) -> None:
    if callback.message is None or not _is_owner(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    parts = callback.data.split(":")
    manager_id = _parse_id(parts[-1])
    try:
        index = int(parts[2])
    except (ValueError, IndexError):
        manager_id = None
    if manager_id is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        card = await staff.get_staff_card(
            db_session, actor=_actor(effective_context), manager_id=manager_id
        )
        if not 0 <= index < len(card.permissions):
            await callback.answer(_STALE, show_alert=True)
            return
        flag = card.permissions[index]
        await staff.set_permission(
            db_session,
            actor=_actor(effective_context),
            manager_id=manager_id,
            flag=flag.key,
            enabled=not flag.enabled,
        )
        await db_session.commit()
        await _render_card(callback.message, db_session, effective_context, manager_id)
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("Збережено")


async def _status_action(
    callback: CallbackQuery,
    ctx: EffectiveContext,
    session: AsyncSession,
    action,
) -> None:
    if callback.message is None or not _is_owner(ctx):
        await callback.answer(_STALE, show_alert=True)
        return
    manager_id = _parse_id(callback.data.split(":")[-1])
    if manager_id is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        await action(session, actor=_actor(ctx), manager_id=manager_id)
        await session.commit()
        await _render_card(callback.message, session, ctx, manager_id)
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("stf:delete:"))
async def cb_delete(
    callback: CallbackQuery, effective_context: EffectiveContext, db_session: AsyncSession
) -> None:
    if callback.message is None or not _is_owner(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    manager_id = _parse_id(callback.data.split(":")[-1])
    if manager_id is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        card = await staff.get_staff_card(
            db_session, actor=_actor(effective_context), manager_id=manager_id
        )
    except StaffNotFound:
        await callback.answer(_STALE, show_alert=True)
        return
    await callback.message.edit_text(
        texts.delete_confirm_text(card),
        reply_markup=kb.build_delete_confirm_kb(card),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("stf:deleteok:"))
async def cb_delete_ok(
    callback: CallbackQuery, effective_context: EffectiveContext, db_session: AsyncSession
) -> None:
    if callback.message is None or not _is_owner(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    manager_id = _parse_id(callback.data.split(":")[-1])
    if manager_id is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        await staff.delete_manager(
            db_session,
            actor=_actor(effective_context),
            manager_id=manager_id,
        )
        await db_session.commit()
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("Менеджера видалено")
    await _render_list(
        callback.message, db_session, effective_context, offset=0, query=None, edit=True
    )


@router.callback_query(F.data == "stf:add")
async def cb_add(
    callback: CallbackQuery, effective_context: EffectiveContext, state: FSMContext
) -> None:
    if callback.message is None or not _is_owner(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    await state.set_state(StaffState.waiting_for_add)
    await callback.message.answer(texts.add_prompt_text(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "home:staff")
async def staff_open_home(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None or not _is_owner(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    await state.clear()
    await _render_list(
        callback.message,
        db_session,
        effective_context,
        offset=0,
        query=None,
        edit=True,
    )
    await callback.answer()


@router.callback_query(F.data == "stf:search")
async def cb_search(
    callback: CallbackQuery, effective_context: EffectiveContext, state: FSMContext
) -> None:
    if callback.message is None or not _is_owner(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    await state.set_state(StaffState.waiting_for_search)
    await callback.message.answer(texts.search_prompt_text())
    await callback.answer()


@router.message(StaffState.waiting_for_search, F.text)
async def staff_search_input(
    message: Message,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    await state.clear()
    if not _is_owner(effective_context):
        return
    await _render_list(
        message, db_session, effective_context, offset=0, query=message.text.strip(), edit=False
    )


@router.message(StaffState.waiting_for_add, F.text)
async def staff_add_input(
    message: Message,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
    bot: Bot,
) -> None:
    await state.clear()
    if not _is_owner(effective_context):
        return
    raw = message.text.strip()
    telegram_id: int | None = None
    phone: str | None = None
    normalized_phone = normalize_phone(raw)
    if normalized_phone is not None:
        # Украинский номер (0…/380…/+380…, с разделителями) → телефон в формате НП.
        # По телефону найм работает и для тех, кто ещё не запускал бота.
        phone = normalized_phone
    elif raw.isdigit():
        # Голые цифры, не телефон → Telegram-ID (нового создаём на лету).
        telegram_id = int(raw)
    else:
        await message.answer(texts.invalid_add_input_text())
        return
    try:
        result = await staff.add_manager(
            db_session,
            actor=_actor(effective_context),
            telegram_id=telegram_id,
            phone=phone,
        )
    except ClientServiceError as exc:
        await message.answer(str(exc))
        return
    await db_session.commit()
    # Пуш приветствия — только если менеджер уже входил в бота (есть telegram_id);
    # заведённый по телефону получит его при первом входе.
    if result.telegram_id is not None:
        await BotNotifier(bot).send_message(result.telegram_id, notifications.manager_added_text())
    await message.answer(texts.added_text(result.card))
    await _render_list(message, db_session, effective_context, offset=0, query=None, edit=False)
