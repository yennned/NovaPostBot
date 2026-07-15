"""Manager/owner shipment queue screen."""

from __future__ import annotations

import html
import uuid

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.manager_shipments import (
    PAGE_SIZE,
    build_card_kb,
    build_queue_kb,
    build_return_inspection_kb,
)
from app.bot.keyboards.menus import MENU_TEXTS
from app.bot.notify import BotNotifier
from app.bot.states import ManagerShipmentState
from app.bot.texts.manager_shipments import (
    action_done_text,
    card_text,
    queue_text,
    return_inspection_text,
    search_prompt_text,
)
from app.bot.types import EffectiveContext
from app.config import get_settings
from app.db.models.client_account import ClientAccount
from app.db.models.enums import ClientAccountStatus, ShipmentStatus, UserRole
from app.novaposhta.client import NovaPoshtaClient
from app.services import inventory, manager_shipments
from app.services.exceptions import ClientServiceError
from app.services.returns import ReturnDecision

router = Router(name="manager_shipments")

SHIPMENTS_BUTTON = "📬 Відправлення"
WAREHOUSE_BUTTON = "📦 Склад"
_STALE = "Кнопка застаріла, відкрийте розділ заново."


def _is_staff(context: EffectiveContext) -> bool:
    role = context.effective_role
    return role in {UserRole.manager, UserRole.owner} or context.is_dev


def _book_link(book_id: str, title: str) -> str:
    return f'🔗 <a href="https://docs.google.com/spreadsheets/d/{html.escape(book_id)}">{title}</a>'


async def _warehouse_text(db_session: AsyncSession) -> str:
    """Текст экрана «📦 Склад»: ссылки на книги + сводка остатков по аккаунтам.

    Сводка идёт по **аккаунтам**, а не по `User`: лист склада принадлежит
    аккаунту. Раньше выбирались все `role=client`, и каждый работник аккаунта
    попадал сюда отдельной строкой «лист недоступний» (своего листа у него нет,
    `users.stock_sheet_key` работника — это его телефон), а его аккаунт при этом
    дублировался строкой владельца.
    """
    settings = get_settings()
    lines = ["📦 <b>Склад</b>"]

    links = [
        _book_link(book_id, title)
        for book_id, title in (
            (settings.sheets_stock_book_id, "Книга «Склад»"),
            (settings.sheets_intake_book_id, "Книга «Приймання»"),
        )
        if book_id
    ]
    if links:
        lines += ["", *links]

    accounts = (
        (
            await db_session.execute(
                select(ClientAccount)
                .where(ClientAccount.status == ClientAccountStatus.active)
                .order_by(ClientAccount.name)
            )
        )
        .scalars()
        .all()
    )
    lines += ["", "<b>Залишки по клієнтах:</b>"]
    if not accounts:
        lines.append("• активних клієнтів немає")
        return "\n".join(lines)

    total_positions = total_units = 0
    read_ok = False
    for account, totals in await inventory.stock_summary(list(accounts)):
        # name приходит из ПІБ клиента (Telegram/ввод) → экранируем под parse_mode=HTML.
        label = html.escape(account.name or str(account.id))
        if totals is None:
            lines.append(f"• {label} — лист недоступний")
            continue
        read_ok = True
        total_positions += totals.positions
        total_units += totals.units
        lines.append(f"• {label} — {totals.positions} поз. / {totals.units} од.")
    if read_ok:
        lines += ["", f"<b>Разом:</b> {total_positions} поз. / {total_units} од."]
    else:
        lines += ["", "⚠️ Залишки тимчасово недоступні (немає доступу до листів)."]
    return "\n".join(lines)


@router.message(F.text == WAREHOUSE_BUTTON)
async def open_warehouse(
    message: Message,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    """📦 Склад менеджера: посилання на книги + зведення залишків по клієнтах."""
    if not _is_staff(effective_context):
        raise SkipHandler()
    await message.answer(
        await _warehouse_text(db_session), parse_mode="HTML", disable_web_page_preview=True
    )


@router.callback_query(F.data == "home:warehouse")
async def open_warehouse_home(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    if not _is_staff(effective_context):
        raise SkipHandler()
    await callback.message.edit_text(
        await _warehouse_text(db_session), parse_mode="HTML", disable_web_page_preview=True
    )
    await callback.answer()


def _default_return_decisions(card) -> dict[str, bool]:
    return {item.sku: True for item in card.shipment.items}


async def _clear_return_inspection(state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(
        manager_return_shipment_id=None,
        manager_return_bucket=None,
        manager_return_offset=None,
        manager_return_decisions=None,
    )


async def _return_context(
    state: FSMContext,
) -> tuple[uuid.UUID, str, int, dict[str, bool]] | None:
    data = await state.get_data()
    shipment_raw = data.get("manager_return_shipment_id")
    bucket = data.get("manager_return_bucket")
    offset = data.get("manager_return_offset")
    decisions = data.get("manager_return_decisions")
    if not shipment_raw or not bucket or offset is None or not isinstance(decisions, dict):
        return None
    try:
        shipment_id = uuid.UUID(str(shipment_raw))
    except ValueError:
        return None
    parsed = {str(key): bool(value) for key, value in decisions.items()}
    return shipment_id, str(bucket), int(offset), parsed


async def _show_queue(
    target: Message,
    *,
    session: AsyncSession,
    context: EffectiveContext,
    state: FSMContext,
    bucket: str,
    offset: int = 0,
) -> None:
    data = await state.get_data()
    page = await manager_shipments.list_queue(
        session,
        actor=context.effective_user or context.actor_user,
        bucket=bucket,
        query=data.get("manager_shipment_query"),
        limit=PAGE_SIZE,
        offset=offset,
    )
    await state.update_data(manager_shipment_bucket=bucket)
    await target.answer(queue_text(page), reply_markup=build_queue_kb(page), parse_mode="HTML")


async def _edit_queue(
    message: Message,
    *,
    session: AsyncSession,
    context: EffectiveContext,
    state: FSMContext,
    bucket: str,
    offset: int = 0,
) -> None:
    data = await state.get_data()
    page = await manager_shipments.list_queue(
        session,
        actor=context.effective_user or context.actor_user,
        bucket=bucket,
        query=data.get("manager_shipment_query"),
        limit=PAGE_SIZE,
        offset=offset,
    )
    await state.update_data(manager_shipment_bucket=bucket)
    await message.edit_text(queue_text(page), reply_markup=build_queue_kb(page), parse_mode="HTML")


@router.message(F.text == SHIPMENTS_BUTTON)
async def open_queue(
    message: Message,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if not _is_staff(effective_context):
        raise SkipHandler()
    await state.clear()
    await state.update_data(manager_shipment_query=None, manager_shipment_bucket="created")
    try:
        await _show_queue(
            message,
            session=db_session,
            context=effective_context,
            state=state,
            bucket="created",
        )
    except ClientServiceError as exc:
        await message.answer(str(exc))


@router.callback_query(F.data == "home:manager_shipments")
async def open_queue_home(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    if not _is_staff(effective_context):
        raise SkipHandler()
    await state.clear()
    await state.update_data(manager_shipment_query=None, manager_shipment_bucket="created")
    try:
        await _edit_queue(
            callback.message,
            session=db_session,
            context=effective_context,
            state=state,
            bucket="created",
        )
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("mq:list:"))
async def cb_queue(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        _, _, bucket, offset_raw = callback.data.split(":")
        offset = int(offset_raw)
    except (ValueError, AttributeError):
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        await _edit_queue(
            callback.message,
            session=db_session,
            context=effective_context,
            state=state,
            bucket=bucket,
            offset=offset,
        )
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("mq:card:"))
async def cb_card(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        _, _, bucket, offset_raw, shipment_raw = callback.data.split(":")
        offset = int(offset_raw)
        shipment_id = uuid.UUID(shipment_raw)
    except (ValueError, AttributeError):
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        card = await manager_shipments.get_card(
            db_session,
            actor=effective_context.effective_user or effective_context.actor_user,
            shipment_id=shipment_id,
        )
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.message.edit_text(
        card_text(card),
        reply_markup=build_card_kb(bucket, offset, card),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mq:search:"))
async def cb_search(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        bucket = callback.data.split(":")[2]
    except IndexError:
        await callback.answer(_STALE, show_alert=True)
        return
    await state.set_state(ManagerShipmentState.waiting_for_search)
    await state.update_data(manager_shipment_bucket=bucket)
    await callback.message.answer(search_prompt_text())
    await callback.answer()


@router.callback_query(F.data.startswith("mq:clear:"))
async def cb_clear(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        bucket = callback.data.split(":")[2]
    except IndexError:
        await callback.answer(_STALE, show_alert=True)
        return
    data = await state.get_data()
    if not data.get("manager_shipment_query"):
        # Нечего сбрасывать — не редактируем (иначе «message is not modified»).
        await callback.answer("Активного пошуку немає.")
        return
    await state.update_data(manager_shipment_query=None)
    await _edit_queue(
        callback.message,
        session=db_session,
        context=effective_context,
        state=state,
        bucket=bucket,
        offset=0,
    )
    await callback.answer("Пошук скинуто.")


@router.message(
    ManagerShipmentState.waiting_for_search,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(MENU_TEXTS),
)
async def receive_search(
    message: Message,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    data = await state.get_data()
    bucket = data.get("manager_shipment_bucket", "created")
    await state.clear()
    await state.update_data(manager_shipment_query=message.text, manager_shipment_bucket=bucket)
    await _show_queue(
        message,
        session=db_session,
        context=effective_context,
        state=state,
        bucket=bucket,
    )


@router.callback_query(F.data.startswith("mq:confirm:"))
async def cb_confirm(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    bot: Bot,
) -> None:
    await _act_on_shipment(
        callback,
        effective_context=effective_context,
        db_session=db_session,
        bot=bot,
        action="confirm",
    )


@router.callback_query(F.data.startswith("mq:cancel:"))
async def cb_cancel(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    bot: Bot,
    np_client: NovaPoshtaClient,
) -> None:
    await _act_on_shipment(
        callback,
        effective_context=effective_context,
        db_session=db_session,
        bot=bot,
        action="cancel",
        np_client=np_client,
    )


@router.callback_query(F.data.startswith("mq:return:"))
async def cb_return(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        _, _, bucket, offset_raw, shipment_raw = callback.data.split(":")
        offset = int(offset_raw)
        shipment_id = uuid.UUID(shipment_raw)
    except (ValueError, AttributeError):
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        card = await manager_shipments.get_card(
            db_session,
            actor=effective_context.effective_user or effective_context.actor_user,
            shipment_id=shipment_id,
        )
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    decisions = _default_return_decisions(card)
    await state.set_state(ManagerShipmentState.inspecting_return)
    await state.update_data(
        manager_return_shipment_id=str(shipment_id),
        manager_return_bucket=bucket,
        manager_return_offset=offset,
        manager_return_decisions=decisions,
    )
    await callback.message.edit_text(
        return_inspection_text(card, decisions),
        reply_markup=build_return_inspection_kb(bucket, offset, card, decisions),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mq:rit:"))
async def cb_return_toggle(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    ctx = await _return_context(state)
    if ctx is None:
        await callback.answer(_STALE, show_alert=True)
        return
    shipment_id, bucket, offset, decisions = ctx
    try:
        index = int(callback.data.split(":")[2])
    except (IndexError, ValueError, AttributeError):
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        card = await manager_shipments.get_card(
            db_session,
            actor=effective_context.effective_user or effective_context.actor_user,
            shipment_id=shipment_id,
        )
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    if index < 0 or index >= len(card.shipment.items):
        await callback.answer(_STALE, show_alert=True)
        return
    sku = card.shipment.items[index].sku
    decisions[sku] = not decisions.get(sku, True)
    await state.update_data(manager_return_decisions=decisions)
    await callback.message.edit_text(
        return_inspection_text(card, decisions),
        reply_markup=build_return_inspection_kb(bucket, offset, card, decisions),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "mq:rib")
async def cb_return_back(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    ctx = await _return_context(state)
    if ctx is None:
        await callback.answer(_STALE, show_alert=True)
        return
    shipment_id, bucket, offset, _ = ctx
    try:
        card = await manager_shipments.get_card(
            db_session,
            actor=effective_context.effective_user or effective_context.actor_user,
            shipment_id=shipment_id,
        )
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await _clear_return_inspection(state)
    await callback.message.edit_text(
        card_text(card),
        reply_markup=build_card_kb(bucket, offset, card),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "mq:ria")
async def cb_return_apply(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
    bot: Bot,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    ctx = await _return_context(state)
    if ctx is None:
        await callback.answer(_STALE, show_alert=True)
        return
    shipment_id, bucket, offset, decisions = ctx
    actor = effective_context.effective_user or effective_context.actor_user
    try:
        existing = await manager_shipments.get_card(
            db_session,
            actor=actor,
            shipment_id=shipment_id,
        )
        service_decisions = [
            ReturnDecision(
                sku=item.sku,
                accepted_quantity=item.quantity if decisions.get(item.sku, True) else 0,
                rejected_quantity=0 if decisions.get(item.sku, True) else item.quantity,
                comment=(
                    None
                    if decisions.get(item.sku, True)
                    else "Позначено як брак під час приймання повернення"
                ),
            )
            for item in existing.shipment.items
        ]
        card = await manager_shipments.receive_return(
            db_session,
            actor=actor,
            shipment_id=shipment_id,
            decisions=service_decisions,
        )
        await db_session.commit()
        await manager_shipments.notify_client_about_status(
            db_session,
            BotNotifier(bot),
            shipment_id=shipment_id,
        )
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await _clear_return_inspection(state)
    await callback.message.edit_text(
        card_text(card),
        reply_markup=build_card_kb(bucket, offset, card),
        parse_mode="HTML",
    )
    await callback.answer(action_done_text("return"))


@router.callback_query(F.data.startswith("mq:lost:"))
async def cb_lost(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    bot: Bot,
) -> None:
    await _act_on_shipment(
        callback,
        effective_context=effective_context,
        db_session=db_session,
        bot=bot,
        action="lost",
    )


@router.callback_query(F.data.startswith("mq:damaged:"))
async def cb_damaged(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    bot: Bot,
) -> None:
    await _act_on_shipment(
        callback,
        effective_context=effective_context,
        db_session=db_session,
        bot=bot,
        action="damaged",
    )


async def _act_on_shipment(
    callback: CallbackQuery,
    *,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    action: str,
    bot: Bot | None,
    np_client: NovaPoshtaClient | None = None,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        _, _, bucket, offset_raw, shipment_raw = callback.data.split(":")
        offset = int(offset_raw)
        shipment_id = uuid.UUID(shipment_raw)
    except (ValueError, AttributeError):
        await callback.answer(_STALE, show_alert=True)
        return

    actor = effective_context.effective_user or effective_context.actor_user
    try:
        if action == "confirm":
            card = await manager_shipments.confirm_shipment(
                db_session,
                actor=actor,
                shipment_id=shipment_id,
            )
            if bot is not None:
                await db_session.commit()
                await manager_shipments.notify_client_about_status(
                    db_session,
                    BotNotifier(bot),
                    shipment_id=shipment_id,
                )
        elif action == "cancel":
            card = await manager_shipments.cancel_shipment(
                db_session,
                actor=actor,
                shipment_id=shipment_id,
                np_client=np_client,
            )
            if bot is not None:
                await db_session.commit()
                await manager_shipments.notify_client_about_status(
                    db_session,
                    BotNotifier(bot),
                    shipment_id=shipment_id,
                )
        elif action == "lost":
            card = await manager_shipments.mark_nonstandard(
                db_session,
                actor=actor,
                shipment_id=shipment_id,
                status=ShipmentStatus.lost,
            )
            if bot is not None:
                await db_session.commit()
                await manager_shipments.notify_client_about_nonstandard(
                    db_session,
                    BotNotifier(bot),
                    shipment_id=shipment_id,
                )
        else:
            card = await manager_shipments.mark_nonstandard(
                db_session,
                actor=actor,
                shipment_id=shipment_id,
                status=ShipmentStatus.damaged,
            )
            if bot is not None:
                await db_session.commit()
                await manager_shipments.notify_client_about_nonstandard(
                    db_session,
                    BotNotifier(bot),
                    shipment_id=shipment_id,
                )
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    await callback.message.edit_text(
        card_text(card),
        reply_markup=build_card_kb(bucket, offset, card),
        parse_mode="HTML",
    )
    await callback.answer(action_done_text(action))
