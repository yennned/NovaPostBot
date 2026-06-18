"""Раздел «Клієнти» (Фаза 2): список, карточка, действия над статусом.

Хендлеры зовут доменный `app/services/clients.py` с `db_session` (инъекция
middleware) и `effective_context.actor_user`; доменные ошибки
(`ClientServiceError`) маппятся в uk-тексты. Пуши шлёт `BotNotifier` поверх `Bot`.
Правка профиля клиента — в отдельном PR (этот PR: список/карточка/статусы/поиск).
"""

from __future__ import annotations

import uuid

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.clients import (
    PAGE_SIZE,
    build_client_card_kb,
    build_clients_list_kb,
    parse_status_token,
    status_token,
)
from app.bot.notify import BotNotifier
from app.bot.states import ClientManageState
from app.bot.texts.clients import (
    action_done_text,
    client_card_text,
    client_error_text,
    clients_header,
    empty_list_text,
    search_prompt_text,
)
from app.bot.types import EffectiveContext
from app.db.models.user import User
from app.db.repositories import UserRepository
from app.services import clients, notifications
from app.services.exceptions import ClientServiceError, PermissionDenied

router = Router(name="clients")

MANAGE_CLIENTS_BUTTON = "👥 Клієнти"

_ACTIONS = {
    "approve": clients.approve_client,
    "block": clients.block_client,
    "unblock": clients.unblock_client,
    "archive": clients.archive_client,
    "restore": clients.restore_client,
}


async def _list_payload(
    db_session: AsyncSession, actor: User, token: str, offset: int, query: str | None = None
):
    status = parse_status_token(token)
    page = await clients.list_clients(
        db_session, actor=actor, status=status, query=query, limit=PAGE_SIZE, offset=offset
    )
    text = clients_header(page.total)
    if not page.items:
        text += "\n\n" + empty_list_text()
    return text, build_clients_list_kb(page, token)


async def _edit_or_ignore(callback: CallbackQuery, text: str, reply_markup) -> None:
    """edit_text, проглатывая 'message is not modified'."""
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            raise


@router.message(F.text == MANAGE_CLIENTS_BUTTON)
async def open_clients(
    message: Message,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    await state.clear()
    actor = effective_context.actor_user
    if actor is None:
        await message.answer("Спочатку авторизуйтесь через /start.")
        return
    try:
        text, kb = await _list_payload(db_session, actor, "pending", 0)
    except PermissionDenied:
        await message.answer("Недостатньо прав для розділу «Клієнти».")
        return
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("cl:list:"))
async def cb_list(
    callback: CallbackQuery, effective_context: EffectiveContext, db_session: AsyncSession
) -> None:
    _, _, token, offset_raw = callback.data.split(":")
    actor = effective_context.actor_user
    if actor is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    try:
        text, kb = await _list_payload(db_session, actor, token, int(offset_raw))
    except PermissionDenied:
        await callback.answer("Немає прав.", show_alert=True)
        return
    await _edit_or_ignore(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("cl:card:"))
async def cb_card(
    callback: CallbackQuery, effective_context: EffectiveContext, db_session: AsyncSession
) -> None:
    _, _, token, client_raw = callback.data.split(":")
    actor = effective_context.actor_user
    if actor is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    try:
        card = await clients.get_client_card(
            db_session, actor=actor, client_id=uuid.UUID(client_raw)
        )
    except ClientServiceError as exc:
        await callback.answer(client_error_text(exc), show_alert=True)
        return
    await _edit_or_ignore(callback, client_card_text(card), build_client_card_kb(card, token))
    await callback.answer()


@router.callback_query(F.data.startswith("cl:act:"))
async def cb_action(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    bot: Bot,
) -> None:
    _, _, action, client_raw = callback.data.split(":")
    actor = effective_context.actor_user
    if actor is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    handler = _ACTIONS.get(action)
    if handler is None:
        await callback.answer()
        return

    client_id = uuid.UUID(client_raw)
    try:
        card = await handler(db_session, actor=actor, client_id=client_id)
    except ClientServiceError as exc:
        await callback.answer(client_error_text(exc), show_alert=True)
        return

    # Пуш клиенту при подтверждении.
    if action == "approve":
        client = await UserRepository(db_session).get_by_id(client_id)
        if client is not None:
            await notifications.notify_client_approved(BotNotifier(bot), client=client)

    token = status_token(card.status)
    await _edit_or_ignore(callback, client_card_text(card), build_client_card_kb(card, token))
    await callback.answer(action_done_text(card))


@router.callback_query(F.data.startswith("cl:search:"))
async def cb_search(callback: CallbackQuery, state: FSMContext) -> None:
    token = callback.data.split(":")[2]
    await state.set_state(ClientManageState.waiting_for_search)
    await state.update_data(token=token)
    await callback.message.answer(search_prompt_text())
    await callback.answer()


@router.message(ClientManageState.waiting_for_search, F.text)
async def receive_search(
    message: Message,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    data = await state.get_data()
    token = data.get("token", "all")
    await state.clear()
    actor = effective_context.actor_user
    if actor is None:
        await message.answer("Спочатку авторизуйтесь через /start.")
        return
    try:
        text, kb = await _list_payload(db_session, actor, token, 0, query=message.text)
    except PermissionDenied:
        await message.answer("Недостатньо прав для розділу «Клієнти».")
        return
    await message.answer(text, reply_markup=kb, parse_mode="HTML")
