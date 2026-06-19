"""Поток создания ТТН — каркас + кошик (Фаза 4, PR 9a). Namespace `cab:ttn:*`.

Express-картка: короткий happy-path кошик → параметри → отримувач → … → картка.
PR 9a покрывает вход (ранний резолв ФОП с разведёнными uk-текстами), набор корзины
(степпер + ввод числа), экран «Параметри посилки» (вага+габарити) и розвилку типа
отримувача. Шаги получателя/адреса/карточки добавят PR 9b–9d.

Длинные значения (sku) в callback_data не кладём — резолвим по индексу страницы из
`list_inventory` (re-fetch на каждый тап), корзину держим в FSM-data. 🚚-кнопку
меню к `start_create_ttn` привяжет PR 9d (пока поток не самодостаточен для юзера).
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.types.base import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.ttn import (
    DEFAULT_SIZE_TOKEN,
    SIZE_PRESETS,
    TTN_PAGE_SIZE,
    build_cart_picker_kb,
    build_cart_review_kb,
    build_parcel_kb,
    build_recipient_kind_kb,
    build_stepper_kb,
)
from app.bot.states import CreateTtnState
from app.bot.texts import ttn as texts
from app.bot.types import EffectiveContext
from app.services import sender_profile
from app.services.exceptions import PermissionDenied
from app.services.inventory import InventoryItem, list_inventory

router = Router(name="create_ttn")

_STALE = "Кнопка застаріла, почніть створення ТТН заново."
_MAX_WEIGHT = Decimal("1000")
_RECIPIENT_KINDS = {"p": "person", "o": "organization"}


def _effective_client(context: EffectiveContext):
    return context.effective_user or context.actor_user


# ---------------------------------------------------------------- вход + ФОП-гейт


async def start_create_ttn(
    message: Message,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    """Вход в поток: ранний резолв ФОП (configured/validated), затем кошик."""
    client = _effective_client(effective_context)
    if client is None:
        await message.answer("Спочатку авторизуйтесь через /start.")
        return
    try:
        profiles = await sender_profile.list_profiles(db_session, actor=client, client_id=client.id)
    except PermissionDenied as exc:
        await message.answer(str(exc))
        return
    default = next((p for p in profiles if p.is_default), None)
    if default is None:
        await message.answer(texts.no_profile_text(), parse_mode="HTML")
        return
    if not default.is_np_validated:
        await message.answer(texts.not_validated_text(), parse_mode="HTML")
        return

    await state.clear()
    await state.set_state(CreateTtnState.picking_items)
    await state.update_data(
        sender_profile_id=str(default.id),
        cart={},
        cart_offset=0,
        size_token=DEFAULT_SIZE_TOKEN,
        nonce=uuid.uuid4().hex,
    )
    try:
        await _show_picker(message, db_session, client, state, offset=0, edit=False)
    except PermissionDenied as exc:
        await message.answer(str(exc))


# --------------------------------------------------------------------- рендеры


async def _show_picker(
    target: Message | TelegramObject,
    session: AsyncSession,
    client,
    state: FSMContext,
    *,
    offset: int,
    edit: bool,
) -> None:
    page = await list_inventory(session, client=client, limit=TTN_PAGE_SIZE, offset=offset)
    await state.update_data(cart_offset=page.offset)
    data = await state.get_data()
    cart_count = len(data.get("cart", {}))
    text = texts.cart_picker_text(page, cart_count=cart_count)
    kb = build_cart_picker_kb(page, cart_count=cart_count)
    if edit:
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")


async def _show_stepper(message: Message, state: FSMContext, *, edit: bool) -> None:
    pending = (await state.get_data()).get("pending")
    item = InventoryItem(
        sku=pending["sku"],
        name=pending["name"],
        category=None,
        stock=pending["available"],
        reserved=0,
        available=pending["available"],
        price=Decimal(pending["price"]) if pending["price"] is not None else None,
    )
    text = texts.stepper_text(item, pending["qty"])
    kb = build_stepper_kb(qty=pending["qty"], available=pending["available"])
    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


def _cart_lines(cart: dict) -> list[tuple[str, int, Decimal | None]]:
    lines: list[tuple[str, int, Decimal | None]] = []
    for entry in cart.values():
        price = Decimal(entry["price"]) if entry["price"] is not None else None
        lines.append((entry["name"], entry["qty"], price))
    return lines


async def _show_cart(message: Message, state: FSMContext) -> None:
    cart = (await state.get_data()).get("cart", {})
    text = texts.cart_review_text(_cart_lines(cart))
    kb = build_cart_review_kb(list(cart.keys()))
    await message.edit_text(text, reply_markup=kb, parse_mode="HTML")


async def _show_parcel(message: Message, state: FSMContext, *, edit: bool = True) -> None:
    data = await state.get_data()
    size_token = data.get("size_token", DEFAULT_SIZE_TOKEN)
    text = texts.parcel_text(weight=data.get("weight"), size_token=size_token)
    kb = build_parcel_kb(size_token=size_token, weight_set=bool(data.get("weight")))
    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ------------------------------------------------------------------- кошик: набор


@router.callback_query(F.data.startswith("cab:ttn:page:"))
async def cb_page(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        offset = int(callback.data.split(":")[3])
    except (IndexError, ValueError):
        await callback.answer(_STALE, show_alert=True)
        return
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    await state.set_state(CreateTtnState.picking_items)
    try:
        await _show_picker(callback.message, db_session, client, state, offset=offset, edit=True)
    except PermissionDenied as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("cab:ttn:pick:"))
async def cb_pick(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        idx = int(callback.data.split(":")[3])
    except (IndexError, ValueError):
        await callback.answer(_STALE, show_alert=True)
        return
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    offset = (await state.get_data()).get("cart_offset", 0)
    page = await list_inventory(db_session, client=client, limit=TTN_PAGE_SIZE, offset=offset)
    if idx >= len(page.items):
        await callback.answer(_STALE, show_alert=True)
        return
    item = page.items[idx]
    if item.available <= 0:
        await callback.answer(f"«{item.name}» немає на залишку.", show_alert=True)
        return
    await state.update_data(
        pending={
            "sku": item.sku,
            "name": item.name,
            "available": item.available,
            "price": str(item.price) if item.price is not None else None,
            "qty": 1,
        }
    )
    await state.set_state(CreateTtnState.picking_items)
    await _show_stepper(callback.message, state, edit=True)
    await callback.answer()


@router.callback_query(F.data == "cab:ttn:qnoop")
async def cb_qty_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("cab:ttn:qd:"))
async def cb_qty_delta(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    pending = (await state.get_data()).get("pending")
    if pending is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        delta = int(callback.data.split(":")[3])
    except (IndexError, ValueError):
        await callback.answer(_STALE, show_alert=True)
        return
    new_qty = max(1, min(pending["qty"] + delta, pending["available"]))
    pending["qty"] = new_qty
    await state.update_data(pending=pending)
    await _show_stepper(callback.message, state, edit=True)
    await callback.answer()


@router.callback_query(F.data == "cab:ttn:qmax")
async def cb_qty_max(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    pending = (await state.get_data()).get("pending")
    if pending is None:
        await callback.answer(_STALE, show_alert=True)
        return
    pending["qty"] = pending["available"]
    await state.update_data(pending=pending)
    await _show_stepper(callback.message, state, edit=True)
    await callback.answer()


@router.callback_query(F.data == "cab:ttn:qnum")
async def cb_qty_num(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    pending = (await state.get_data()).get("pending")
    if pending is None:
        await callback.answer(_STALE, show_alert=True)
        return
    item = InventoryItem(
        sku=pending["sku"],
        name=pending["name"],
        category=None,
        stock=pending["available"],
        reserved=0,
        available=pending["available"],
        price=None,
    )
    await state.set_state(CreateTtnState.entering_qty)
    await callback.message.answer(texts.qty_prompt_text(item))
    await callback.answer()


@router.message(CreateTtnState.entering_qty, F.text, ~F.text.startswith("/"))
async def receive_qty(message: Message, state: FSMContext) -> None:
    pending = (await state.get_data()).get("pending")
    if pending is None:
        await state.set_state(CreateTtnState.picking_items)
        await message.answer(_STALE)
        return
    try:
        qty = int((message.text or "").strip())
    except ValueError:
        await message.answer(f"❌ Введіть ціле число 1–{pending['available']}.")
        return
    if qty < 1 or qty > pending["available"]:
        await message.answer(f"❌ Кількість має бути 1–{pending['available']}.")
        return
    pending["qty"] = qty
    await state.update_data(pending=pending)
    await state.set_state(CreateTtnState.picking_items)
    await _show_stepper(message, state, edit=False)


@router.callback_query(F.data == "cab:ttn:qok")
async def cb_qty_ok(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    data = await state.get_data()
    pending = data.get("pending")
    if pending is None:
        await callback.answer(_STALE, show_alert=True)
        return
    cart = dict(data.get("cart", {}))
    sku = pending["sku"]
    prev = cart.get(sku, {}).get("qty", 0)
    # Сумма в корзине не должна превышать остаток (пред-проверка; create_shipment
    # всё равно валидирует InsufficientStock на отправке).
    total = min(prev + pending["qty"], pending["available"])
    cart[sku] = {"qty": total, "name": pending["name"], "price": pending["price"]}
    await state.update_data(cart=cart, pending=None)
    # Возвращаем состояние в picking_items: если пользователь до этого жал «Ввести
    # число» (entering_qty), без сброса последующий текст ушёл бы в receive_qty.
    await state.set_state(CreateTtnState.picking_items)
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    try:
        await _show_picker(
            callback.message,
            db_session,
            client,
            state,
            offset=data.get("cart_offset", 0),
            edit=True,
        )
    except PermissionDenied as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(f"Додано: {pending['name']} ×{total}")


# ------------------------------------------------------------------ кошик: перегляд


@router.callback_query(F.data == "cab:ttn:cart")
async def cb_cart(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    await state.set_state(CreateTtnState.picking_items)
    await _show_cart(callback.message, state)
    await callback.answer()


@router.callback_query(F.data.startswith("cab:ttn:crm:"))
async def cb_cart_remove(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        idx = int(callback.data.split(":")[3])
    except (IndexError, ValueError):
        await callback.answer(_STALE, show_alert=True)
        return
    cart = dict((await state.get_data()).get("cart", {}))
    skus = list(cart.keys())
    if idx >= len(skus):
        await callback.answer(_STALE, show_alert=True)
        return
    removed = cart.pop(skus[idx])
    await state.update_data(cart=cart)
    await _show_cart(callback.message, state)
    await callback.answer(f"Прибрано: {removed['name']}")


@router.callback_query(F.data.startswith("cab:ttn:cedit:"))
async def cb_cart_edit(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        idx = int(callback.data.split(":")[3])
    except (IndexError, ValueError):
        await callback.answer(_STALE, show_alert=True)
        return
    cart = (await state.get_data()).get("cart", {})
    skus = list(cart.keys())
    if idx >= len(skus):
        await callback.answer(_STALE, show_alert=True)
        return
    sku = skus[idx]
    entry = cart[sku]
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    # Остаток для редактирования берём актуальный (а не сохранённый в корзине).
    page = await list_inventory(db_session, client=client, query=sku, limit=TTN_PAGE_SIZE, offset=0)
    match = next((it for it in page.items if it.sku == sku), None)
    available = match.available if match else entry["qty"]
    await state.update_data(
        pending={
            "sku": sku,
            "name": entry["name"],
            "available": max(available, entry["qty"]),
            "price": entry["price"],
            "qty": min(entry["qty"], max(available, entry["qty"])),
        }
    )
    await state.set_state(CreateTtnState.picking_items)
    await _show_stepper(callback.message, state, edit=True)
    await callback.answer()


# -------------------------------------------------------- параметри посилки (вага+габарити)


@router.callback_query(F.data == "cab:ttn:next")
async def cb_next_to_parcel(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    cart = (await state.get_data()).get("cart", {})
    if not cart:
        await callback.answer("Кошик порожній — додайте товар.", show_alert=True)
        return
    await state.set_state(CreateTtnState.picking_parcel)
    await _show_parcel(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "cab:ttn:parcel")
async def cb_parcel(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    await state.set_state(CreateTtnState.picking_parcel)
    await _show_parcel(callback.message, state)
    await callback.answer()


@router.callback_query(F.data.startswith("cab:ttn:sz:"))
async def cb_size(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    token = callback.data.split(":")[3]
    if token not in SIZE_PRESETS:
        await callback.answer(_STALE, show_alert=True)
        return
    await state.update_data(size_token=token)
    await _show_parcel(callback.message, state)
    await callback.answer(f"Габарити: {SIZE_PRESETS[token]}")


@router.callback_query(F.data == "cab:ttn:wt")
async def cb_weight_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    await state.set_state(CreateTtnState.entering_weight)
    await callback.message.answer(texts.weight_prompt_text())
    await callback.answer()


@router.message(CreateTtnState.entering_weight, F.text, ~F.text.startswith("/"))
async def receive_weight(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(",", ".")
    try:
        weight = Decimal(raw)
    except InvalidOperation:
        await message.answer(texts.weight_invalid_text())
        return
    if weight <= 0 or weight > _MAX_WEIGHT:
        await message.answer(texts.weight_invalid_text())
        return
    # Нормализуем (строкой — JSON-safe для FSM-data). Экран параметрів — новым
    # сообщением (текстовый ввод нельзя редактировать как inline-экран).
    await state.update_data(weight=f"{weight.normalize():f}")
    await state.set_state(CreateTtnState.picking_parcel)
    await _show_parcel(message, state, edit=False)


# ----------------------------------------------------------------- тип отримувача


@router.callback_query(F.data == "cab:ttn:torcpt")
async def cb_to_recipient(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    if not (await state.get_data()).get("weight"):
        await callback.answer("Спочатку вкажіть вагу.", show_alert=True)
        return
    await state.set_state(CreateTtnState.picking_recipient_kind)
    await callback.message.edit_text(
        texts.recipient_kind_text(), reply_markup=build_recipient_kind_kb(), parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cab:ttn:rk:"))
async def cb_recipient_kind(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    kind = _RECIPIENT_KINDS.get(callback.data.split(":")[3])
    if kind is None:
        await callback.answer(_STALE, show_alert=True)
        return
    await state.update_data(recipient_kind=kind)
    # Сбор данных получателя (ПІБ/ЄДРПОУ/телефон) и адреса — PR 9b.
    await callback.answer("Обрано. Далі — дані отримувача (скоро).")


# --------------------------------------------------------------------- скасування


@router.callback_query(F.data == "cab:ttn:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text("Створення ТТН скасовано.")
    await callback.answer()
