"""Хендлеры поддержки (Фаза 6): релей-чат клиент↔дежурный менеджер.

Клиент пишет через «💬 Звернення до менеджера» — сообщения релеятся дежурному.
Поддержка — функция менеджера (владелец её не имеет): менеджер видит «💬 Підтримка»
— свой инбокс + очередь без дежурного. Полный лог с поиском — только dev god-mode.
Релей идёт только через бота, прямых Telegram-контактов нет ([docs/10-support-duty.md]).

Уведомления — по паттерну «commit, потім notify»: тексты/получатели вычисляются
до commit (живые ORM-атрибуты), отправка — после.
"""

from __future__ import annotations

import uuid

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import permissions
from app.bot.keyboards import support as kb
from app.bot.keyboards.menus import build_role_menu
from app.bot.notify import BotNotifier
from app.bot.screen import remember_screen
from app.bot.states import SupportState
from app.bot.texts import support as texts
from app.bot.types import EffectiveContext
from app.db.models.enums import SupportThreadStatus, UserRole, UserStatus
from app.db.models.user import User
from app.db.repositories import SupportRepository
from app.services import notifications, support
from app.services.exceptions import ClientServiceError

router = Router(name="support")

SUPPORT_CLIENT_BUTTON = "💬 Звернення до менеджера"
SUPPORT_STAFF_BUTTON = "💬 Підтримка"
_ACTIVE = {SupportThreadStatus.open, SupportThreadStatus.waiting}
_STALE = "Кнопка застаріла, відкрийте розділ заново."


def _is_staff(ctx: EffectiveContext) -> bool:
    # Подержка — функция менеджера; владелец её не имеет (dev — god-mode).
    return ctx.effective_role is UserRole.manager or ctx.is_dev


def _is_dev(ctx: EffectiveContext) -> bool:
    # Полный лог всех тредов + поиск — только dev god-mode.
    return ctx.is_dev


def _can_handle_support(ctx: EffectiveContext) -> bool:
    user = ctx.effective_user
    if user is None:
        return False
    if ctx.is_dev:
        return True
    if user.status is not UserStatus.active:
        return False
    if ctx.effective_role is UserRole.manager:
        return permissions.has_permission(user, permissions.CAN_HANDLE_SUPPORT)
    return False


def _can_access_thread(ctx: EffectiveContext, thread) -> bool:
    user = ctx.effective_user
    if not _can_handle_support(ctx) or user is None:
        return False
    if _is_dev(ctx):
        return True
    return thread.assigned_manager_id == user.id or (
        thread.assigned_manager_id is None and thread.status is SupportThreadStatus.waiting
    )


def _client_user(ctx: EffectiveContext) -> User | None:
    return ctx.effective_user if ctx.effective_role is UserRole.client else None


def _staff_sender_role(ctx: EffectiveContext) -> str:
    return ctx.effective_role.value if ctx.effective_role is not None else "dev"


def _parse_id(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        return None


def _client_label(client: User) -> str:
    return f"{client.full_name or 'без імені'} ({client.phone or '—'})"


async def _exit_chat_to_home(
    message: Message, ctx: EffectiveContext, text: str, *, default_role: UserRole
) -> None:
    """Закрыть чат/ответ и вернуть нижнюю панель меню роли.

    Во время чата висела ReplyKeyboardMarkup («Вийти з чату»/«Завершити
    відповідь»). Новая reply-панель роли заменяет её одним сообщением — НЕ
    ReplyKeyboardRemove, иначе нижнее меню исчезнет до наступного /start.
    """
    role = ctx.effective_role or default_role
    await message.answer(text, reply_markup=build_role_menu(role))


async def _thread_id_from_state(state: FSMContext) -> uuid.UUID | None:
    data = await state.get_data()
    return _parse_id(data.get("support_thread_id", ""))


# --- Клиент ----------------------------------------------------------------


@router.message(SupportState.client_chatting, F.text == kb.CLIENT_CHAT_EXIT)
async def client_chat_exit(
    message: Message,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
    bot: Bot,
) -> None:
    """«Завершити чат» реально закрывает обращение (не только выходит из чата).

    Раньше кнопка лишь чистила стейт, тред оставался `open`/`waiting` и «воскресал»
    при повторном открытии — клиенту казалось, что чат не завершается. Теперь тред
    переходит в `closed`, дежурный (если был) уведомляется.
    """
    thread_id = await _thread_id_from_state(state)
    thread = await SupportRepository(db_session).get_with_messages(thread_id) if thread_id else None
    manager_tid: int | None = None
    # Сначала закрываем тред и коммитим — только потом чистим стейт: если БД
    # упадёт, стейт не потеряется, клиент сможет повторить, а не останется с
    # «воскресающим» открытым тредом и залипшей клавиатурой без подтверждения.
    if thread is not None and thread.status is not SupportThreadStatus.closed:
        manager_tid = thread.assigned_manager.telegram_id if thread.assigned_manager else None
        closed_note = notifications.support_thread_closed_by_client_text(thread.client)
        await support.close_thread(db_session, thread=thread)
        await db_session.commit()
        if manager_tid is not None:
            await BotNotifier(bot).send_message(manager_tid, closed_note)
    await state.clear()
    await _exit_chat_to_home(
        message, effective_context, texts.chat_closed_text(), default_role=UserRole.client
    )


@router.message(StateFilter(None), F.text == kb.CLIENT_CHAT_EXIT)
async def client_chat_exit_stale(
    message: Message, effective_context: EffectiveContext, state: FSMContext
) -> None:
    """Fallback: «Завершити чат» вне активного чата (стейт потерян после рестарта).

    Reply-клавиатура в Telegram «залипает» и переживает рестарт бота; без этого
    хендлера нажатие после потери стейта не матчилось ни с чем и молча
    игнорировалось. `StateFilter(None)` — срабатывает только когда FSM-стейта нет
    (именно этот случай), чтобы не перехватывать текст в состояниях менеджера/dev,
    где та же строка могла быть отправлена осмысленно. Снимаем клавиатуру и
    возвращаем на головну.
    """
    await state.clear()
    await _exit_chat_to_home(
        message, effective_context, texts.chat_exited_text(), default_role=UserRole.client
    )


@router.message(SupportState.client_chatting, F.text)
async def client_chat_message(
    message: Message,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
    bot: Bot,
) -> None:
    client = _client_user(effective_context)
    if client is None or client.status is not UserStatus.active:
        await state.clear()
        await _exit_chat_to_home(
            message,
            effective_context,
            texts.thread_unavailable_text(),
            default_role=UserRole.client,
        )
        return

    thread_id = await _thread_id_from_state(state)
    thread = await SupportRepository(db_session).get_with_messages(thread_id) if thread_id else None
    if (
        thread is None
        or thread.status is SupportThreadStatus.closed
        or (
            effective_context.account is not None
            and thread.account_id != effective_context.account.id
        )
    ):
        await state.clear()
        await _exit_chat_to_home(
            message,
            effective_context,
            texts.thread_unavailable_text(),
            default_role=UserRole.client,
        )
        return

    manager_tid = thread.assigned_manager.telegram_id if thread.assigned_manager else None
    relay_text = notifications.support_message_for_manager_text(thread.client, message.text)
    await support.post_message(
        db_session,
        thread=thread,
        sender_role="client",
        sender_user_id=(
            effective_context.effective_user.id if effective_context.effective_user else None
        ),
        text=message.text,
    )
    await db_session.commit()
    if manager_tid is not None:
        await BotNotifier(bot).send_message(manager_tid, relay_text)
        # Подтверждаем клиенту каждое сообщение: без ack чат выглядит «одноразовым»
        # (после отправки ничего не происходит) — это и рождало жалобу «застряли».
        await message.answer(texts.client_message_ack_text())
    else:
        await message.answer(texts.queued_ack_text())


@router.message(F.text == SUPPORT_CLIENT_BUTTON)
async def client_open(
    message: Message,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    client = _client_user(effective_context)
    if client is None:
        raise SkipHandler()
    await state.clear()
    existing = (
        await SupportRepository(db_session).get_active_thread_for_client(client.id)
        if effective_context.account is None
        else await SupportRepository(db_session).get_active_thread_for_account(
            effective_context.account.id
        )
    )
    if existing is not None:
        await state.set_state(SupportState.client_chatting)
        await state.update_data(support_thread_id=str(existing.id))
        await message.answer(texts.client_resume_text(), reply_markup=kb.build_client_chat_kb())
        return
    contact = await support.get_duty_contact(db_session)
    await message.answer(
        texts.duty_card_text(contact), reply_markup=kb.build_client_start_kb(), parse_mode="HTML"
    )


@router.callback_query(F.data == "home:support_client")
async def client_open_home(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    client = _client_user(effective_context)
    if client is None or callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    await state.clear()
    existing = (
        await SupportRepository(db_session).get_active_thread_for_client(client.id)
        if effective_context.account is None
        else await SupportRepository(db_session).get_active_thread_for_account(
            effective_context.account.id
        )
    )
    if existing is not None:
        await callback.message.edit_text(
            texts.client_resume_text(),
            reply_markup=kb.build_client_start_kb(),
        )
        await remember_screen(state, callback.message)
        await callback.answer()
        return
    contact = await support.get_duty_contact(db_session)
    await callback.message.edit_text(
        texts.duty_card_text(contact),
        reply_markup=kb.build_client_start_kb(),
        parse_mode="HTML",
    )
    await remember_screen(state, callback.message)
    await callback.answer()


@router.callback_query(F.data == "sup:start")
async def client_start(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
    bot: Bot,
) -> None:
    client = _client_user(effective_context)
    if client is None or callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        result = await support.open_or_get_thread(
            db_session,
            client=client,
            account_id=effective_context.account.id if effective_context.account else None,
        )
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    client_label = _client_label(client)
    await db_session.commit()
    if result.notify_managers:
        await notifications.notify_support_queued_to_managers(
            db_session, BotNotifier(bot), client_label=client_label
        )
    await state.set_state(SupportState.client_chatting)
    await state.update_data(support_thread_id=str(result.thread.id))
    prompt = texts.chat_started_prompt_text() if result.routed else texts.queued_ack_text()
    await callback.message.answer(prompt, reply_markup=kb.build_client_chat_kb())
    await callback.answer()


# --- Персонал --------------------------------------------------------------


async def _show_inbox(
    target: Message,
    session: AsyncSession,
    ctx: EffectiveContext,
    *,
    offset: int,
    query: str | None,
    edit: bool,
) -> None:
    repo = SupportRepository(session)
    if _is_dev(ctx):
        statuses = None if query else _ACTIVE
        threads, total = await repo.list_all(
            query=query, statuses=statuses, limit=kb.PAGE_SIZE, offset=offset
        )
        show_search, scope = True, "усі звернення"
    else:
        threads, total = await repo.list_for_manager_inbox(
            ctx.effective_user.id, limit=kb.PAGE_SIZE, offset=offset
        )
        show_search, scope = False, "мої звернення"
    text = texts.inbox_text(total, scope=scope, query=query)
    markup = kb.build_inbox_kb(threads, offset=offset, total=total, show_search=show_search)
    if edit:
        await target.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=markup, parse_mode="HTML")


@router.message(SupportState.manager_replying, F.text == kb.STAFF_REPLY_EXIT)
async def staff_reply_exit(
    message: Message, effective_context: EffectiveContext, state: FSMContext
) -> None:
    await state.clear()
    await _exit_chat_to_home(
        message, effective_context, texts.reply_exited_text(), default_role=UserRole.manager
    )


@router.message(SupportState.manager_replying, F.text)
async def staff_reply_message(
    message: Message,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
    bot: Bot,
) -> None:
    if not _can_handle_support(effective_context):
        await state.clear()
        await _exit_chat_to_home(
            message,
            effective_context,
            texts.support_unavailable_text(),
            default_role=UserRole.manager,
        )
        return
    thread_id = await _thread_id_from_state(state)
    thread = await SupportRepository(db_session).get_with_messages(thread_id) if thread_id else None
    if thread is None or thread.status is SupportThreadStatus.closed:
        await state.clear()
        await _exit_chat_to_home(
            message,
            effective_context,
            texts.thread_unavailable_text(),
            default_role=UserRole.manager,
        )
        return
    if not _can_access_thread(effective_context, thread):
        await state.clear()
        await _exit_chat_to_home(
            message,
            effective_context,
            texts.thread_forbidden_text(),
            default_role=UserRole.manager,
        )
        return
    await support.claim_if_waiting(
        db_session, thread=thread, manager=effective_context.effective_user
    )
    client_tid = thread.client.telegram_id
    relay_text = notifications.support_message_for_client_text(message.text)
    await support.post_message(
        db_session,
        thread=thread,
        sender_role=_staff_sender_role(effective_context),
        sender_user_id=(
            effective_context.effective_user.id if effective_context.effective_user else None
        ),
        text=message.text,
    )
    await db_session.commit()
    await BotNotifier(bot).send_message(client_tid, relay_text)
    await message.answer(texts.reply_sent_text())


@router.message(F.text == SUPPORT_STAFF_BUTTON)
async def staff_open(
    message: Message,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if not _is_staff(effective_context):
        raise SkipHandler()
    if not _can_handle_support(effective_context):
        await state.clear()
        await message.answer(texts.support_unavailable_text())
        return
    await state.clear()
    await _show_inbox(message, db_session, effective_context, offset=0, query=None, edit=False)


@router.callback_query(F.data == "home:support_staff")
async def staff_open_home(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None or not _is_staff(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    if not _can_handle_support(effective_context):
        await callback.answer(texts.support_unavailable_text(), show_alert=True)
        return
    await state.clear()
    await _show_inbox(
        callback.message,
        db_session,
        effective_context,
        offset=0,
        query=None,
        edit=True,
    )
    await remember_screen(state, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("sup:inbox:"))
async def cb_inbox(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None or not _is_staff(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    if not _can_handle_support(effective_context):
        await callback.answer(texts.support_unavailable_text(), show_alert=True)
        return
    try:
        offset = int(callback.data.split(":")[2])
    except (ValueError, IndexError):
        await callback.answer(_STALE, show_alert=True)
        return
    await state.clear()
    await _show_inbox(
        callback.message, db_session, effective_context, offset=offset, query=None, edit=True
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sup:open:"))
async def cb_open(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    if callback.message is None or not _is_staff(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    if not _can_handle_support(effective_context):
        await callback.answer(texts.support_unavailable_text(), show_alert=True)
        return
    thread_id = _parse_id(callback.data.split(":")[-1])
    thread = await SupportRepository(db_session).get_with_messages(thread_id) if thread_id else None
    if thread is None:
        await callback.answer(_STALE, show_alert=True)
        return
    if not _can_access_thread(effective_context, thread):
        await callback.answer(texts.thread_forbidden_text(), show_alert=True)
        return
    can_reply = thread.status is not SupportThreadStatus.closed
    await callback.message.edit_text(
        texts.conversation_text(thread),
        reply_markup=kb.build_thread_kb(thread, can_reply=can_reply),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sup:reply:"))
async def cb_reply(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None or not _is_staff(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    if not _can_handle_support(effective_context):
        await callback.answer(texts.support_unavailable_text(), show_alert=True)
        return
    thread_id = _parse_id(callback.data.split(":")[-1])
    thread = await SupportRepository(db_session).get_with_messages(thread_id) if thread_id else None
    if thread is None:
        await callback.answer(_STALE, show_alert=True)
        return
    if not _can_access_thread(effective_context, thread):
        await callback.answer(texts.thread_forbidden_text(), show_alert=True)
        return
    await state.set_state(SupportState.manager_replying)
    await state.update_data(support_thread_id=str(thread_id))
    await callback.message.answer(texts.reply_prompt_text(), reply_markup=kb.build_staff_reply_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("sup:close:"))
async def cb_close(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    bot: Bot,
) -> None:
    if callback.message is None or not _is_staff(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    if not _can_handle_support(effective_context):
        await callback.answer(texts.support_unavailable_text(), show_alert=True)
        return
    thread_id = _parse_id(callback.data.split(":")[-1])
    thread = await SupportRepository(db_session).get_with_messages(thread_id) if thread_id else None
    if thread is None:
        await callback.answer(_STALE, show_alert=True)
        return
    if not _can_access_thread(effective_context, thread):
        await callback.answer(texts.thread_forbidden_text(), show_alert=True)
        return
    client_tid = thread.client.telegram_id
    await support.close_thread(db_session, thread=thread)
    await db_session.commit()
    await BotNotifier(bot).send_message(client_tid, notifications.support_thread_closed_text())
    await callback.answer(texts.thread_closed_text())
    await _show_inbox(
        callback.message, db_session, effective_context, offset=0, query=None, edit=True
    )


@router.callback_query(F.data == "sup:search")
async def cb_search(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    state: FSMContext,
) -> None:
    if callback.message is None or not _is_dev(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    await state.set_state(SupportState.log_search)
    await callback.message.answer(texts.search_prompt_text())
    await callback.answer()


@router.message(SupportState.log_search, F.text)
async def staff_search_input(
    message: Message,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    await state.clear()
    if not _is_dev(effective_context):
        return
    await _show_inbox(
        message, db_session, effective_context, offset=0, query=message.text.strip(), edit=False
    )
