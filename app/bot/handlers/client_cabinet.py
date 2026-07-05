"""Разделы кабинета клиента (Фаза 3): товары, отправления, статистика, настройки."""

from __future__ import annotations

import contextlib
import uuid
from datetime import date

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.types.base import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.client import (
    NOTIFICATION_CALLBACK_TOKENS,
    PRODUCTS_PAGE_SIZE,
    SENDER_PROFILE_FIELD_TOKENS,
    SHIPMENTS_PAGE_SIZE,
    build_inventory_kb,
    build_sender_profile_kb,
    build_sender_profiles_kb,
    build_settings_kb,
    build_shipment_card_kb,
    build_shipments_kb,
    build_stats_kb,
)
from app.bot.screen import edit_stored_screen, remember_screen
from app.bot.states import ClientCabinetState, SenderProfileCreateState
from app.bot.texts.client_cabinet import (
    new_profile_created_text,
    new_profile_invalid_phone_text,
    new_profile_key_invalid_text,
    new_profile_key_prompt,
    new_profile_name_prompt,
    new_profile_np_unavailable_text,
    new_profile_phone_prompt,
    new_profile_sender_name_prompt,
    product_search_prompt,
    products_text,
    profile_edit_prompt,
    sender_profile_text,
    sender_profiles_text,
    settings_text,
    shipment_card_text,
    shipment_search_prompt,
    shipments_text,
    stats_text,
)
from app.bot.types import EffectiveContext
from app.db.models.enums import OrgType
from app.novaposhta.client import NovaPoshtaClient
from app.novaposhta.exceptions import NovaPoshtaError
from app.services import client_settings, sender_profile
from app.services.exceptions import (
    InvalidNotificationSetting,
    PermissionDenied,
    PhoneAlreadyTaken,
    SenderProfileIncomplete,
    SenderProfileKeyInvalid,
    SenderProfileNotFound,
    ShipmentActionForbidden,
    ShipmentNotFound,
    TtnCancelFailed,
)
from app.services.inventory import list_inventory
from app.services.shipment import cancel_shipment
from app.services.shipments import get_shipment_card, list_shipments
from app.services.stats import get_client_stats
from app.utils.dates import USER_DATE_HINT, parse_user_date
from app.utils.phone import normalize_phone

router = Router(name="client_cabinet")

PRODUCTS_BUTTON = "📦 Товари"
SHIPMENTS_BUTTON = "📬 Відправлення"
STATS_BUTTON = "📊 Статистика"
SETTINGS_BUTTON = "⚙️ Налаштування"
_STALE_BUTTON = "Кнопка застаріла, відкрийте розділ заново."
_SELF_EDIT_FIELDS = {"full_name", "phone"}
_CLEARABLE_FIELDS = {"full_name", "sender_full_name", "edrpou"}  # sender_phone обязателен
_NOTIFICATION_KEYS_BY_TOKEN = {value: key for key, value in NOTIFICATION_CALLBACK_TOKENS.items()}
_SENDER_PROFILE_FIELDS_BY_TOKEN = {value: key for key, value in SENDER_PROFILE_FIELD_TOKENS.items()}


def _effective_client(context: EffectiveContext):
    return context.effective_user or context.actor_user


async def _remember_if_possible(state: FSMContext, target: Message | TelegramObject) -> None:
    if isinstance(target, Message):
        await remember_screen(state, target)


def _normalize_edit_value(field: str, raw: str) -> str | None:
    value = raw.strip()
    if not value:
        raise ValueError
    if value == "-" and field in _CLEARABLE_FIELDS:
        return None
    return value


async def _product_filters(state: FSMContext) -> tuple[str | None, str | None]:
    data = await state.get_data()
    return data.get("product_query"), data.get("product_category")


async def _shipment_filters(state: FSMContext) -> tuple[str | None, str]:
    data = await state.get_data()
    return data.get("shipment_query"), data.get("shipment_bucket", "created")


async def _show_inventory(
    target: Message | TelegramObject,
    session: AsyncSession,
    context: EffectiveContext,
    *,
    state: FSMContext,
    offset: int = 0,
) -> None:
    client = _effective_client(context)
    if client is None:
        await target.answer("Спочатку авторизуйтесь через /start.")
        return
    query, category = await _product_filters(state)
    page = await list_inventory(
        session,
        client=client,
        query=query,
        category=category,
        limit=PRODUCTS_PAGE_SIZE,
        offset=offset,
    )
    await state.update_data(product_categories=page.categories)
    await target.answer(
        products_text(page),
        reply_markup=build_inventory_kb(page, active_category=category, query=query),
        parse_mode="HTML",
    )
    await _remember_if_possible(state, target)


async def _edit_inventory(
    message: Message,
    session: AsyncSession,
    context: EffectiveContext,
    *,
    state: FSMContext,
    offset: int = 0,
) -> None:
    client = _effective_client(context)
    if client is None:
        return
    query, category = await _product_filters(state)
    page = await list_inventory(
        session,
        client=client,
        query=query,
        category=category,
        limit=PRODUCTS_PAGE_SIZE,
        offset=offset,
    )
    await state.update_data(product_categories=page.categories)
    await message.edit_text(
        products_text(page),
        reply_markup=build_inventory_kb(page, active_category=category, query=query),
        parse_mode="HTML",
    )
    await remember_screen(state, message)


async def _show_shipments(
    target: Message | TelegramObject,
    session: AsyncSession,
    context: EffectiveContext,
    *,
    state: FSMContext,
    bucket: str,
    offset: int = 0,
) -> None:
    client = _effective_client(context)
    if client is None:
        await target.answer("Спочатку авторизуйтесь через /start.")
        return
    query, _ = await _shipment_filters(state)
    page = await list_shipments(
        session,
        client=client,
        bucket=bucket,
        query=query,
        limit=SHIPMENTS_PAGE_SIZE,
        offset=offset,
    )
    await state.update_data(shipment_bucket=bucket)
    await target.answer(
        shipments_text(page, bucket),
        reply_markup=build_shipments_kb(page, bucket, query=query),
        parse_mode="HTML",
    )
    await _remember_if_possible(state, target)


async def _edit_shipments(
    message: Message,
    session: AsyncSession,
    context: EffectiveContext,
    *,
    state: FSMContext,
    bucket: str,
    offset: int = 0,
) -> None:
    client = _effective_client(context)
    if client is None:
        return
    query, _ = await _shipment_filters(state)
    page = await list_shipments(
        session,
        client=client,
        bucket=bucket,
        query=query,
        limit=SHIPMENTS_PAGE_SIZE,
        offset=offset,
    )
    await state.update_data(shipment_bucket=bucket)
    await message.edit_text(
        shipments_text(page, bucket),
        reply_markup=build_shipments_kb(page, bucket, query=query),
        parse_mode="HTML",
    )
    await remember_screen(state, message)


async def _show_settings(
    target: Message | TelegramObject,
    session: AsyncSession,
    context: EffectiveContext,
) -> None:
    client = _effective_client(context)
    if client is None:
        await target.answer("Спочатку авторизуйтесь через /start.")
        return
    view = await client_settings.get_client_settings(session, client=client)
    await target.answer(
        settings_text(view),
        reply_markup=build_settings_kb(view),
        parse_mode="HTML",
    )


async def _edit_settings(
    message: Message,
    session: AsyncSession,
    context: EffectiveContext,
) -> None:
    client = _effective_client(context)
    if client is None:
        return
    view = await client_settings.get_client_settings(session, client=client)
    await message.edit_text(
        settings_text(view),
        reply_markup=build_settings_kb(view),
        parse_mode="HTML",
    )


async def _edit_inventory_screen(
    bot: Bot,
    state: FSMContext,
    session: AsyncSession,
    context: EffectiveContext,
    *,
    offset: int = 0,
) -> bool:
    client = _effective_client(context)
    if client is None:
        return False
    query, category = await _product_filters(state)
    page = await list_inventory(
        session,
        client=client,
        query=query,
        category=category,
        limit=PRODUCTS_PAGE_SIZE,
        offset=offset,
    )
    await state.update_data(product_categories=page.categories)
    return await edit_stored_screen(
        bot,
        state,
        text=products_text(page),
        reply_markup=build_inventory_kb(page, active_category=category, query=query),
        parse_mode="HTML",
    )


async def _edit_shipments_screen(
    bot: Bot,
    state: FSMContext,
    session: AsyncSession,
    context: EffectiveContext,
    *,
    bucket: str,
    offset: int = 0,
) -> bool:
    client = _effective_client(context)
    if client is None:
        return False
    query, _ = await _shipment_filters(state)
    page = await list_shipments(
        session,
        client=client,
        bucket=bucket,
        query=query,
        limit=SHIPMENTS_PAGE_SIZE,
        offset=offset,
    )
    await state.update_data(shipment_bucket=bucket)
    return await edit_stored_screen(
        bot,
        state,
        text=shipments_text(page, bucket),
        reply_markup=build_shipments_kb(page, bucket, query=query),
        parse_mode="HTML",
    )


async def _edit_stats_screen(
    bot: Bot,
    state: FSMContext,
    snapshot,
    *,
    selected: str,
) -> bool:
    return await edit_stored_screen(
        bot,
        state,
        text=stats_text(snapshot),
        reply_markup=build_stats_kb(selected),
        parse_mode="HTML",
    )


async def _edit_settings_screen(
    bot: Bot,
    state: FSMContext,
    view,
) -> bool:
    return await edit_stored_screen(
        bot,
        state,
        text=settings_text(view),
        reply_markup=build_settings_kb(view),
        parse_mode="HTML",
    )


async def _edit_sender_profile_screen(
    bot: Bot,
    state: FSMContext,
    profile,
) -> bool:
    return await edit_stored_screen(
        bot,
        state,
        text=sender_profile_text(profile),
        reply_markup=build_sender_profile_kb(profile),
        parse_mode="HTML",
    )


async def _edit_sender_profiles(
    message: Message,
    session: AsyncSession,
    context: EffectiveContext,
) -> None:
    client = _effective_client(context)
    if client is None:
        return
    profiles = await sender_profile.list_profiles(
        session,
        actor=client,
        client_id=client.id,
    )
    await message.edit_text(
        sender_profiles_text(profiles),
        reply_markup=build_sender_profiles_kb(profiles),
        parse_mode="HTML",
    )


async def _show_sender_profile(
    target: Message | TelegramObject,
    session: AsyncSession,
    context: EffectiveContext,
    *,
    profile_id: uuid.UUID,
) -> None:
    client = _effective_client(context)
    if client is None:
        await target.answer("Спочатку авторизуйтесь через /start.")
        return
    profile = await sender_profile.get_profile(session, actor=client, profile_id=profile_id)
    await target.answer(
        sender_profile_text(profile),
        reply_markup=build_sender_profile_kb(profile),
        parse_mode="HTML",
    )


async def _edit_sender_profile(
    message: Message,
    session: AsyncSession,
    context: EffectiveContext,
    *,
    profile_id: uuid.UUID,
) -> None:
    client = _effective_client(context)
    if client is None:
        return
    profile = await sender_profile.get_profile(session, actor=client, profile_id=profile_id)
    await message.edit_text(
        sender_profile_text(profile),
        reply_markup=build_sender_profile_kb(profile),
        parse_mode="HTML",
    )


@router.message(F.text == PRODUCTS_BUTTON)
async def open_products(
    message: Message,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    await state.clear()
    await state.update_data(product_query=None, product_category=None)
    try:
        await _show_inventory(message, db_session, effective_context, state=state)
    except PermissionDenied as exc:
        await message.answer(str(exc))


@router.callback_query(F.data == "home:products")
async def open_products_home(
    callback: CallbackQuery,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    await state.clear()
    await state.update_data(product_query=None, product_category=None)
    try:
        await _edit_inventory(callback.message, db_session, effective_context, state=state)
    except PermissionDenied as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await remember_screen(state, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("cab:products:"))
async def cb_products(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    try:
        offset = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    try:
        await _edit_inventory(
            callback.message,
            db_session,
            effective_context,
            state=state,
            offset=offset,
        )
    except PermissionDenied as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data == "cab:psearch")
async def cb_product_search(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    await state.set_state(ClientCabinetState.waiting_for_product_search)
    await callback.message.answer(product_search_prompt())
    await callback.answer()


@router.callback_query(F.data == "cab:pclear")
async def cb_product_clear(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    await state.update_data(product_query=None, product_category=None)
    try:
        await _edit_inventory(callback.message, db_session, effective_context, state=state)
    except PermissionDenied as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("Фільтри скинуто.")


@router.callback_query(F.data.startswith("cab:pcat:"))
async def cb_product_category(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    category_code = callback.data.split(":")[2]
    if category_code == "all":
        await state.update_data(product_category=None)
    else:
        try:
            idx = int(category_code)
        except ValueError:
            await callback.answer(_STALE_BUTTON, show_alert=True)
            return
        categories = (await state.get_data()).get("product_categories", [])
        if idx < 0 or idx >= len(categories):
            await callback.answer(_STALE_BUTTON, show_alert=True)
            return
        await state.update_data(product_category=categories[idx])
    try:
        await _edit_inventory(
            callback.message,
            db_session,
            effective_context,
            state=state,
            offset=0,
        )
    except PermissionDenied as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()


@router.message(ClientCabinetState.waiting_for_product_search, F.text, ~F.text.startswith("/"))
async def receive_product_search(
    message: Message,
    bot: Bot,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    await state.update_data(product_query=message.text, product_category=None)
    await state.set_state(None)
    try:
        if not await _edit_inventory_screen(bot, state, db_session, effective_context):
            await _show_inventory(message, db_session, effective_context, state=state)
    except PermissionDenied as exc:
        await message.answer(str(exc))


@router.message(F.text == SHIPMENTS_BUTTON)
async def open_shipments(
    message: Message,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    await state.clear()
    await state.update_data(shipment_query=None, shipment_bucket="created")
    try:
        await _show_shipments(
            message,
            db_session,
            effective_context,
            state=state,
            bucket="created",
        )
    except PermissionDenied as exc:
        await message.answer(str(exc))


@router.callback_query(F.data == "home:shipments")
async def open_shipments_home(
    callback: CallbackQuery,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    await state.clear()
    await state.update_data(shipment_query=None, shipment_bucket="created")
    try:
        await _edit_shipments(
            callback.message,
            db_session,
            effective_context,
            state=state,
            bucket="created",
        )
    except PermissionDenied as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await remember_screen(state, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("cab:shipments:"))
async def cb_shipments(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    _, _, bucket, offset_raw = parts
    try:
        offset = int(offset_raw)
    except ValueError:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    try:
        await _edit_shipments(
            callback.message,
            db_session,
            effective_context,
            state=state,
            bucket=bucket,
            offset=offset,
        )
    except PermissionDenied as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("cab:ssearch:"))
async def cb_shipment_search(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    bucket = callback.data.split(":")[2]
    await state.update_data(shipment_bucket=bucket)
    await state.set_state(ClientCabinetState.waiting_for_shipment_search)
    await callback.message.answer(shipment_search_prompt())
    await callback.answer()


@router.callback_query(F.data.startswith("cab:sclear:"))
async def cb_shipment_clear(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    bucket = callback.data.split(":")[2]
    await state.update_data(shipment_query=None, shipment_bucket=bucket)
    try:
        await _edit_shipments(
            callback.message,
            db_session,
            effective_context,
            state=state,
            bucket=bucket,
            offset=0,
        )
    except PermissionDenied as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("Пошук скинуто.")


@router.message(ClientCabinetState.waiting_for_shipment_search, F.text, ~F.text.startswith("/"))
async def receive_shipment_search(
    message: Message,
    bot: Bot,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    _, bucket = await _shipment_filters(state)
    await state.update_data(shipment_query=message.text, shipment_bucket=bucket)
    await state.set_state(None)
    try:
        if not await _edit_shipments_screen(
            bot, state, db_session, effective_context, bucket=bucket
        ):
            await _show_shipments(
                message,
                db_session,
                effective_context,
                state=state,
                bucket=bucket,
            )
    except PermissionDenied as exc:
        await message.answer(str(exc))


@router.callback_query(F.data.startswith("cab:shipment:"))
async def cb_shipment_card(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) not in {4, 5}:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    if len(parts) == 5:
        _, _, bucket, offset_raw, shipment_raw = parts
    else:
        _, _, bucket, shipment_raw = parts
        offset_raw = "0"
    try:
        offset = int(offset_raw)
        shipment_id = uuid.UUID(shipment_raw)
    except ValueError:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    try:
        card = await get_shipment_card(db_session, client=client, shipment_id=shipment_id)
    except (PermissionDenied, ShipmentNotFound) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.message.edit_text(
        shipment_card_text(card),
        reply_markup=build_shipment_card_kb(
            bucket,
            offset,
            card.id,
            can_cancel=card.can_cancel,
        ),
        parse_mode="HTML",
    )
    await remember_screen(state, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("cab:cancel:"))
async def cb_cancel_shipment(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    np_client: NovaPoshtaClient,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 5:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    _, _, bucket, offset_raw, shipment_raw = parts
    try:
        offset = int(offset_raw)
        shipment_id = uuid.UUID(shipment_raw)
    except ValueError:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    try:
        # NP-aware отмена: удаляет ТТН в НП, затем снимает резерв (release у статуса).
        await cancel_shipment(
            db_session,
            client=client,
            shipment_id=shipment_id,
            np_client=np_client,
        )
    except ShipmentActionForbidden:
        await callback.answer("Це відправлення вже не можна скасувати.", show_alert=True)
        return
    except TtnCancelFailed:
        await callback.answer(
            "Не вдалося скасувати ТТН у НП. Спробуйте за хвилину.", show_alert=True
        )
        return
    except (PermissionDenied, ShipmentNotFound) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.update_data(shipment_bucket=bucket)
    await _edit_shipments(
        callback.message,
        db_session,
        effective_context,
        state=state,
        bucket=bucket,
        offset=offset,
    )
    await remember_screen(state, callback.message)
    await callback.answer("ТТН видалено.")


@router.message(F.text == STATS_BUTTON)
async def open_stats(
    message: Message,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    await state.clear()
    client = _effective_client(effective_context)
    if client is None:
        await message.answer("Спочатку авторизуйтесь через /start.")
        return
    try:
        snapshot = await get_client_stats(db_session, client=client)
    except PermissionDenied as exc:
        await message.answer(str(exc))
        return
    await message.answer(
        stats_text(snapshot),
        reply_markup=build_stats_kb("today"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "home:stats")
async def open_stats_home(
    callback: CallbackQuery,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    await state.clear()
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    try:
        snapshot = await get_client_stats(db_session, client=client)
    except PermissionDenied as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.message.edit_text(
        stats_text(snapshot),
        reply_markup=build_stats_kb("today"),
        parse_mode="HTML",
    )
    await remember_screen(state, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("cab:stats:"))
async def cb_stats(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    period = parts[2]
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    try:
        snapshot = await get_client_stats(db_session, client=client, period=period)
    except PermissionDenied as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.message.edit_text(
        stats_text(snapshot),
        reply_markup=build_stats_kb(period),
        parse_mode="HTML",
    )
    await remember_screen(state, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("cab:statsday:"))
async def cb_stats_day(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    try:
        day = date.fromisoformat(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    try:
        snapshot = await get_client_stats(db_session, client=client, day=day)
    except PermissionDenied as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.message.edit_text(
        stats_text(snapshot),
        reply_markup=build_stats_kb("today"),
        parse_mode="HTML",
    )
    await remember_screen(state, callback.message)
    await callback.answer()


@router.callback_query(F.data == "cab:statspick")
async def cb_stats_pick(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    await state.set_state(ClientCabinetState.waiting_for_stats_date)
    await callback.message.answer("Введіть дату у форматі ДД.ММ.РРРР або РРРР-ММ-ДД.")
    await callback.answer()


@router.message(ClientCabinetState.waiting_for_stats_date, F.text, ~F.text.startswith("/"))
async def receive_stats_date(
    message: Message,
    bot: Bot,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    day = parse_user_date(message.text)
    if day is None:
        await message.answer(f"❌ Невірна дата. Використайте {USER_DATE_HINT}.")
        return
    await state.set_state(None)
    client = _effective_client(effective_context)
    if client is None:
        await message.answer("Спочатку авторизуйтесь через /start.")
        return
    try:
        snapshot = await get_client_stats(db_session, client=client, day=day)
    except PermissionDenied as exc:
        await message.answer(str(exc))
        return
    if not await _edit_stats_screen(bot, state, snapshot, selected="today"):
        await message.answer(
            stats_text(snapshot),
            reply_markup=build_stats_kb("today"),
            parse_mode="HTML",
        )


@router.message(F.text == SETTINGS_BUTTON)
async def open_settings(
    message: Message,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    await state.clear()
    try:
        await _show_settings(message, db_session, effective_context)
    except PermissionDenied as exc:
        await message.answer(str(exc))


@router.callback_query(F.data == "home:settings")
async def open_settings_home(
    callback: CallbackQuery,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    await state.clear()
    try:
        await _edit_settings(callback.message, db_session, effective_context)
    except PermissionDenied as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await remember_screen(state, callback.message)
    await callback.answer()


@router.callback_query(F.data == "cab:set:back")
async def cb_settings_back(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    await state.set_state(None)
    try:
        await _edit_settings(callback.message, db_session, effective_context)
    except PermissionDenied as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await remember_screen(state, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("cab:set:toggle:"))
async def cb_settings_toggle(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    token = callback.data.split(":")[3]
    key = _NOTIFICATION_KEYS_BY_TOKEN.get(token)
    if key is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    try:
        view = await client_settings.toggle_notification(db_session, client=client, key=key)
    except (PermissionDenied, InvalidNotificationSetting) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.message.edit_text(
        settings_text(view),
        reply_markup=build_settings_kb(view),
        parse_mode="HTML",
    )
    await remember_screen(state, callback.message)
    await callback.answer("Налаштування оновлено.")


@router.callback_query(F.data.startswith("cab:set:edit:"))
async def cb_settings_edit_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    field = callback.data.split(":")[3]
    if field not in _SELF_EDIT_FIELDS:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    await state.update_data(settings_field=field)
    await state.set_state(ClientCabinetState.waiting_for_settings_profile)
    await callback.message.answer(profile_edit_prompt(field))
    await callback.answer()


@router.message(
    ClientCabinetState.waiting_for_settings_profile,
    F.text,
    ~F.text.startswith("/"),
)
async def receive_settings_profile(
    message: Message,
    bot: Bot,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    field = (await state.get_data()).get("settings_field")
    if field not in _SELF_EDIT_FIELDS:
        await state.set_state(None)
        await message.answer("Спробуйте відкрити налаштування ще раз.")
        return
    try:
        value = _normalize_edit_value(field, message.text)
    except ValueError:
        await message.answer("Порожнє значення не можна зберегти.")
        return
    if field == "phone":
        normalized = normalize_phone(value or "")
        if normalized is None:
            # Лишаємось у стані очікування — користувач введе номер ще раз.
            await message.answer(
                "❌ Невірний номер. Введіть у форматі 0XXXXXXXXX або +380XXXXXXXXX."
            )
            return
        value = normalized
    client = _effective_client(effective_context)
    if client is None:
        await state.set_state(None)
        await message.answer("Спочатку авторизуйтесь через /start.")
        return
    try:
        view = await client_settings.update_self_profile(
            db_session,
            client=client,
            **{field: value},
        )
    except PhoneAlreadyTaken:
        await message.answer("Цей номер вже використовується іншим користувачем.")
        return
    except PermissionDenied as exc:
        await message.answer(str(exc))
        return
    await state.update_data(settings_field=None)
    await state.set_state(None)
    if not await _edit_settings_screen(bot, state, view):
        await message.answer(
            settings_text(view),
            reply_markup=build_settings_kb(view),
            parse_mode="HTML",
        )


@router.callback_query(F.data == "cab:set:profiles")
async def cb_sender_profiles(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    try:
        await _edit_sender_profiles(callback.message, db_session, effective_context)
    except PermissionDenied as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await remember_screen(state, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("cab:set:profile:"))
async def cb_sender_profile_card(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    try:
        profile_id = uuid.UUID(callback.data.split(":")[3])
    except (IndexError, ValueError):
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    try:
        await _edit_sender_profile(
            callback.message,
            db_session,
            effective_context,
            profile_id=profile_id,
        )
    except (PermissionDenied, SenderProfileNotFound) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await remember_screen(state, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("cab:set:pdefault:"))
async def cb_sender_profile_default(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    try:
        profile_id = uuid.UUID(callback.data.split(":")[3])
    except (IndexError, ValueError):
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    try:
        profile = await sender_profile.set_default(
            db_session,
            actor=client,
            profile_id=profile_id,
        )
    except (PermissionDenied, SenderProfileNotFound) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.message.edit_text(
        sender_profile_text(profile),
        reply_markup=build_sender_profile_kb(profile),
        parse_mode="HTML",
    )
    await remember_screen(state, callback.message)
    await callback.answer("Основний профіль оновлено.")


@router.callback_query(F.data.startswith("cab:set:pedit:"))
async def cb_sender_profile_edit_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 5:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    token = parts[3]
    field = _SENDER_PROFILE_FIELDS_BY_TOKEN.get(token)
    if field is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    try:
        profile_id = uuid.UUID(parts[4])
    except ValueError:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    await state.update_data(sender_profile_id=str(profile_id), sender_profile_field=field)
    await state.set_state(ClientCabinetState.waiting_for_sender_profile_edit)
    await callback.message.answer(profile_edit_prompt(field))
    await callback.answer()


@router.message(
    ClientCabinetState.waiting_for_sender_profile_edit,
    F.text,
    ~F.text.startswith("/"),
)
async def receive_sender_profile_edit(
    message: Message,
    bot: Bot,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    data = await state.get_data()
    field = data.get("sender_profile_field")
    profile_raw = data.get("sender_profile_id")
    if field is None or profile_raw is None:
        await state.set_state(None)
        await message.answer("Спробуйте відкрити профіль ФОП ще раз.")
        return
    try:
        profile_id = uuid.UUID(profile_raw)
        value = _normalize_edit_value(field, message.text)
    except ValueError:
        await message.answer("Порожнє значення не можна зберегти.")
        return
    client = _effective_client(effective_context)
    if client is None:
        await state.set_state(None)
        await message.answer("Спочатку авторизуйтесь через /start.")
        return
    try:
        profile = await sender_profile.update_profile(
            db_session,
            actor=client,
            profile_id=profile_id,
            **{field: value},
        )
    except (PermissionDenied, SenderProfileNotFound, SenderProfileIncomplete) as exc:
        await message.answer(str(exc))
        return
    await state.update_data(sender_profile_id=None, sender_profile_field=None)
    await state.set_state(None)
    if not await _edit_sender_profile_screen(bot, state, profile):
        await message.answer(
            sender_profile_text(profile),
            reply_markup=build_sender_profile_kb(profile),
            parse_mode="HTML",
        )


# --------------------------------------------- мастер «➕ Додати ФОП» (self-service)


@router.callback_query(F.data == "cab:set:padd")
async def cb_sender_profile_add(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE_BUTTON, show_alert=True)
        return
    await state.set_state(SenderProfileCreateState.entering_name)
    await state.update_data(new_profile={})
    await callback.message.answer(new_profile_name_prompt(), parse_mode="HTML")
    await callback.answer()


async def _new_profile_data(state: FSMContext) -> dict:
    return (await state.get_data()).get("new_profile") or {}


@router.message(SenderProfileCreateState.entering_name, F.text, ~F.text.startswith("/"))
async def receive_new_profile_name(message: Message, state: FSMContext) -> None:
    draft = await _new_profile_data(state)
    draft["name"] = message.text.strip()
    if not draft["name"]:
        await message.answer(new_profile_name_prompt(), parse_mode="HTML")
        return
    await state.update_data(new_profile=draft)
    await state.set_state(SenderProfileCreateState.entering_api_key)
    await message.answer(new_profile_key_prompt(), parse_mode="HTML")


@router.message(SenderProfileCreateState.entering_api_key, F.text, ~F.text.startswith("/"))
async def receive_new_profile_key(message: Message, state: FSMContext) -> None:
    draft = await _new_profile_data(state)
    draft["np_api_key"] = message.text.strip()
    await state.update_data(new_profile=draft)
    # Ключ — секрет: убираем сообщение из истории чата (best-effort: нет прав/старое —
    # не критично).
    with contextlib.suppress(Exception):
        await message.delete()
    await state.set_state(SenderProfileCreateState.entering_sender_full_name)
    await message.answer(new_profile_sender_name_prompt(), parse_mode="HTML")


@router.message(SenderProfileCreateState.entering_sender_full_name, F.text, ~F.text.startswith("/"))
async def receive_new_profile_sender_name(message: Message, state: FSMContext) -> None:
    draft = await _new_profile_data(state)
    draft["sender_full_name"] = message.text.strip()
    await state.update_data(new_profile=draft)
    await state.set_state(SenderProfileCreateState.entering_sender_phone)
    await message.answer(new_profile_phone_prompt(), parse_mode="HTML")


@router.message(SenderProfileCreateState.entering_sender_phone, F.text, ~F.text.startswith("/"))
async def receive_new_profile_phone(
    message: Message,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    np_client: NovaPoshtaClient | None,
) -> None:
    phone = normalize_phone(message.text or "")
    if phone is None:
        await message.answer(new_profile_invalid_phone_text())
        return
    client = _effective_client(effective_context)
    if client is None:
        await state.set_state(None)
        await message.answer("Спочатку авторизуйтесь через /start.")
        return
    draft = await _new_profile_data(state)
    try:
        profile = await sender_profile.create_profile(
            db_session,
            actor=client,
            client_id=client.id,
            name=draft.get("name", ""),
            np_api_key=draft.get("np_api_key", ""),
            org_type=OrgType.fop,
            sender_full_name=draft.get("sender_full_name"),
            sender_phone=phone,
            np_client=np_client,
        )
    except SenderProfileKeyInvalid:
        # Ключ не прошёл проверку НП — вернёмся на шаг ключа, данные сохранены.
        await state.set_state(SenderProfileCreateState.entering_api_key)
        await message.answer(new_profile_key_invalid_text())
        return
    except NovaPoshtaError:
        # НП недоступна (не «ключ невалиден», а сеть/5xx) — остаёмся на шаге
        # телефона с сохранённым черновиком, клиент повторит отправку позже.
        await message.answer(new_profile_np_unavailable_text())
        return
    except (SenderProfileIncomplete, PermissionDenied) as exc:
        await state.set_state(None)
        await message.answer(str(exc))
        return
    await state.update_data(new_profile=None)
    await state.set_state(None)
    profiles = await sender_profile.list_profiles(db_session, actor=client, client_id=client.id)
    await message.answer(new_profile_created_text(profile))
    await message.answer(
        sender_profiles_text(profiles),
        reply_markup=build_sender_profiles_kb(profiles),
        parse_mode="HTML",
    )
