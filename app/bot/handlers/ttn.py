"""Поток создания ТТН (Фаза 4, Express-картка). Namespace `cab:ttn:*`.

Happy-path: 🚚 кнопка меню → кошик → параметри (вага+габарити) → отримувач
(тип/ПІБ/ЄДРПОУ/телефон) → місто → відділення → картка-зведення з ціною НП →
✅ Відправити (`create_shipment`, NP-first + резерв). Карточка редактируема (✏️ по
полям, COD, перерасчёт цены). Анти-дабл-тап на «Відправити» — `_SUBMITTING`.

Длинные значения (sku/ref) в callback_data не кладём — резолвим по индексу из
FSM-data (лимит 64 байта). FSM-состояние — `MemoryStorage` (теряется при рестарте
бота; для разовой операции приемлемо).
"""

from __future__ import annotations

import contextlib
import uuid
from decimal import Decimal, InvalidOperation

import structlog
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.types.base import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.client import build_sender_pick_kb
from app.bot.keyboards.menus import MENU_TEXTS
from app.bot.keyboards.ttn import (
    DEFAULT_SIZE_TOKEN,
    SIZE_DEFAULT_WEIGHT,
    SIZE_DIMENSIONS,
    SIZE_PRESETS,
    TTN_PAGE_SIZE,
    build_back_to_card_kb,
    build_cancel_kb,
    build_card_kb,
    build_cart_picker_kb,
    build_cart_review_kb,
    build_city_results_kb,
    build_cod_amount_kb,
    build_parcel_kb,
    build_payer_edit_kb,
    build_payment_edit_kb,
    build_recipient_kind_kb,
    build_sender_edit_kb,
    build_size_edit_kb,
    build_stepper_kb,
    build_success_kb,
    build_warehouse_results_kb,
)
from app.bot.notify import BotNotifier
from app.bot.screen import answer_latest_screen, remember_screen
from app.bot.states import CreateTtnState
from app.bot.texts import ttn as texts
from app.bot.types import EffectiveContext
from app.novaposhta.cache import NPReferenceCache
from app.novaposhta.client import NovaPoshtaClient
from app.novaposhta.exceptions import NovaPoshtaError
from app.services import address, pricing, sender_profile
from app.services.exceptions import (
    ClientServiceError,
    InsufficientStock,
    PermissionDenied,
    SenderDispatchNotConfigured,
    SenderProfileIncomplete,
    SenderProfileNotConfigured,
    SenderProfileNotValidated,
    TtnCreationFailed,
)
from app.services.inventory import (
    InventoryItem,
    InventoryPage,
    find_inventory_item,
    list_inventory,
)
from app.services.shipment import create_shipment, resolve_sender_id
from app.utils.phone import normalize_phone as _normalize_phone

router = Router(name="create_ttn")
logger = structlog.get_logger(__name__)

# Анти-дабл-тап «Відправити»: id клиентов с ТТН «в полёте». Проверка `in` + `add`
# без await между ними атомарна в однопоточном asyncio → надёжный single-flight.
#
# Множество живёт в памяти процесса, и это осознанно: межрепличную защиту даёт
# `events_isolation` в диспетчере (второй тап того же пользователя ждёт первого) и
# `submit_key` + `stock_holds` в сервисе (детерминированный ключ корзины). Здесь
# нужен быстрый путь, который отвечает человеку понятным «зачекайте…» вместо того,
# чтобы молча держать его в очереди.
_SUBMITTING: set[int] = set()

_STALE = "Кнопка застаріла, почніть створення ТТН заново."
_MAX_WEIGHT = Decimal("1000")
# Потолок денежной суммы (оголошена вартість, накладений платіж). Нужен не ради
# бизнес-правила, а чтобы `1e999` не уходил в НП числом на тысячу цифр.
_MAX_MONEY = Decimal("1000000")
_RECIPIENT_KINDS = {"p": "person", "o": "organization"}


def _effective_client(context: EffectiveContext):
    return context.effective_user or context.actor_user


def _account(context: EffectiveContext):
    return context.account


def _account_id(context: EffectiveContext):
    account = _account(context)
    return account.id if account is not None else None


def _profile_uuid(data: dict) -> uuid.UUID | None:
    raw = data.get("sender_profile_id")
    return uuid.UUID(raw) if raw else None


def _normalized_city_name(value: str) -> str:
    """Нормализовать название для безопасного точного совпадения с ответом НП."""
    return " ".join(value.casefold().replace("’", "'").split())


async def _typing(bot: Bot, chat_id: int) -> None:
    """Индикатор «печатает…» на время запроса в справочники НП.

    Справочники НП на холодном кэше отвечают ощутимо (round-trip к api.novaposhta),
    а текстовый шаг поиска иначе не даёт никакого отклика — бот выглядит зависшим.
    Best-effort: сбой экшена не должен ронять поиск.
    """
    with contextlib.suppress(TelegramAPIError):
        await bot.send_chat_action(chat_id=chat_id, action="typing")


def _valid_edrpou(raw: str) -> bool:
    """ЄДРПОУ — 8 цифр; ІПН ФОП — 10 цифр."""
    value = raw.strip()
    return value.isdigit() and len(value) in (8, 10)


def _parse_weight(raw: str) -> str | None:
    """Вес в кг (0 < w ≤ макс), строкой без научной нотации; иначе None."""
    try:
        weight = Decimal(raw.strip().replace(",", "."))
    except InvalidOperation:
        return None
    # NaN обязателен отдельной проверкой: сравнения с NaN всегда False, поэтому
    # `nan` проскочил бы обе границы и позже уронил бы quantize в маппере НП.
    if not weight.is_finite() or weight <= 0 or weight > _MAX_WEIGHT:
        return None
    return f"{weight.normalize():f}"


def _parse_money(raw: str, *, positive: bool = False) -> str | None:
    """Сумма в гривнах (0 ≤ s ≤ макс, либо > 0 при positive); строкой; иначе None."""
    try:
        value = Decimal(raw.strip().replace(",", "."))
    except InvalidOperation:
        return None
    # `is_finite()` обязателен первым: `Decimal("nan") < 0` не False, а сигнальное
    # сравнение — поднимает InvalidOperation уже вне `try`, и хендлер падает молча
    # (пользователь видит «кнопка не реагирует»). `inf` же прошёл бы обе границы и
    # ушёл в НП строкой "Infinity".
    if not value.is_finite() or value < 0 or value > _MAX_MONEY:
        return None
    if positive and value <= 0:
        return None
    return f"{value:f}"


# ---------------------------------------------------------------- вход + ФОП-гейт


async def start_create_ttn(
    message: Message,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    *,
    edit: bool = False,
) -> None:
    """Вход в поток. Если у клиента >1 ФОП — сначала выбор отправителя, иначе кошик."""
    client = _effective_client(effective_context)
    if client is None:
        await message.answer("Спочатку авторизуйтесь через /start.")
        return
    profiles = await sender_profile.list_profiles(
        db_session,
        actor=client,
        client_id=client.id,
        account_id=_account_id(effective_context),
    )
    if len(profiles) > 1:
        await state.clear()
        await state.set_state(CreateTtnState.picking_sender)
        kb = build_sender_pick_kb(profiles)
        if edit:
            await message.edit_text(texts.pick_sender_text(), reply_markup=kb, parse_mode="HTML")
            if isinstance(message, Message):
                await remember_screen(state, message)
        else:
            await message.answer(texts.pick_sender_text(), reply_markup=kb, parse_mode="HTML")
        return
    # 0/1 ФОП → прежний строгий гейт по дефолтному (единственному) профилю.
    await _resolve_sender_and_begin(
        message,
        state,
        client,
        db_session,
        profile_id=None,
        edit=edit,
        account_id=_account_id(effective_context),
        account=_account(effective_context),
    )


async def _resolve_sender_and_begin(
    target: Message | TelegramObject,
    state: FSMContext,
    client,
    session: AsyncSession,
    *,
    profile_id: uuid.UUID | None,
    edit: bool,
    account_id: uuid.UUID | None = None,
    account=None,
) -> None:
    """Гейт ФОП (предусловие create_shipment) → вход в кошик выбранным профилем."""
    try:
        sender_profile_id = await resolve_sender_id(
            session, client=client, profile_id=profile_id, account_id=account_id
        )
    except SenderProfileNotConfigured:
        await target.answer(texts.no_profile_text(), parse_mode="HTML")
        return
    except SenderProfileNotValidated:
        await target.answer(texts.not_validated_text(), parse_mode="HTML")
        return
    except SenderProfileIncomplete:
        await target.answer(texts.sender_incomplete_text(), parse_mode="HTML")
        return
    except SenderDispatchNotConfigured:
        await target.answer(texts.sender_dispatch_not_configured_text(), parse_mode="HTML")
        return

    try:
        profile_view = await sender_profile.get_profile(
            session,
            actor=client,
            profile_id=sender_profile_id,
            account_id=account_id,
        )
        profile_name = profile_view.name
    except ClientServiceError:
        # Проверка уже подтвердила профиль; имя — только UI-метаданные и не должно
        # блокировать создание ТТН при временной ошибке повторного чтения.
        profile_name = "—"

    await state.clear()
    await state.set_state(CreateTtnState.picking_items)
    await state.update_data(
        sender_profile_id=str(sender_profile_id),
        sender_profile_name=profile_name,
        cart={},
        cart_offset=0,
        ttn_query=None,
        size_token=DEFAULT_SIZE_TOKEN,
        nonce=uuid.uuid4().hex,
    )
    try:
        await _show_picker(
            target,
            session,
            client,
            state,
            offset=0,
            edit=edit,
            account_id=account_id,
            # `account` обязателен вместе с `account_id`: без него `list_inventory`
            # берёт ключ листа от `client`, а это User работника, а не аккаунт →
            # работник увидел бы свой склад вместо склада аккаунта.
            account=account,
        )
    except PermissionDenied as exc:
        await target.answer(str(exc))


@router.callback_query(CreateTtnState.picking_sender, F.data.startswith("ttn:sender:"))
async def cb_pick_sender(
    callback: CallbackQuery,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        profile_id = uuid.UUID(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer(_STALE, show_alert=True)
        return
    await _resolve_sender_and_begin(
        callback.message,
        state,
        client,
        db_session,
        profile_id=profile_id,
        edit=True,
        account_id=_account_id(effective_context),
        account=_account(effective_context),
    )
    await callback.answer()


# --------------------------------------------------------------------- рендеры


async def _prepare_picker(
    state: FSMContext, page: InventoryPage
) -> tuple[str, InlineKeyboardMarkup]:
    """Запомнить идентичность отрисованной страницы и собрать текст+клавиатуру.

    `picker_skus` — SKU строк ИМЕННО этой страницы: кнопка `cab:ttn:pick:{idx}`
    резолвится через него, а не повторным чтением склада. Индекс осмыслен только
    против того снапшота, который эту клавиатуру и нарисовал: стоит фильтру или
    offset разойтись (а `cb_pick` перечитывал список без активной категории) —
    и в корзину молча уезжает другой товар. Плюс это закрывает случай, который
    синхронизацией аргументов не закрыть в принципе: лист изменился между рендером
    и тапом, `items.sort()` пересортировал, и тот же индекс указывает на чужой SKU.

    Токен поколения в callback_data не нужен: `answer_latest_screen` снимает
    разметку со старого экрана, а `_show_picker`/`_show_stepper` редактируют то же
    сообщение — устаревшая клавиатура пикера в чате не остаётся.

    Единственная точка записи `picker_skus`/`cart_offset`/`ttn_categories`:
    рендеров пикера два (`_show_picker` и `receive_item_search`), и раньше каждый
    вёл это состояние сам — пропустить ключ в одном из них было слишком легко.
    """
    await state.update_data(
        cart_offset=page.offset,
        ttn_categories=page.categories,
        picker_skus=[item.sku for item in page.items],
    )
    data = await state.get_data()
    cart_count = len(data.get("cart", {}))
    category = data.get("ttn_category")
    # «🧹 Скинути» показываем, когда есть что сбрасывать — иначе повторный рендер
    # идентичен и Telegram отклоняет edit, кнопка выглядит битой.
    has_reset = cart_count > 0 or bool(data.get("ttn_query")) or bool(category)
    text = texts.cart_picker_text(page, cart_count=cart_count)
    kb = build_cart_picker_kb(
        page, cart_count=cart_count, active_category=category, has_reset=has_reset
    )
    return text, kb


async def _show_picker(
    target: Message | TelegramObject,
    session: AsyncSession,
    client,
    state: FSMContext,
    *,
    offset: int,
    edit: bool,
    account_id: uuid.UUID | None = None,
    account=None,
) -> None:
    data = await state.get_data()
    page = await list_inventory(
        session,
        client=client,
        query=data.get("ttn_query"),
        category=data.get("ttn_category"),
        limit=TTN_PAGE_SIZE,
        offset=offset,
        account_id=account_id,
        account=account,
    )
    text, kb = await _prepare_picker(state, page)
    if edit:
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")
    if edit and isinstance(target, Message):
        await remember_screen(state, target)


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
    editing = pending.get("mode") == "edit"
    text = texts.stepper_text(item, pending["qty"], edit=editing)
    kb = build_stepper_kb(qty=pending["qty"], available=pending["available"], edit=editing)
    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    if edit:
        await remember_screen(state, message)


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
    await remember_screen(state, message)


async def _show_parcel(message: Message, state: FSMContext, *, edit: bool = True) -> None:
    data = await state.get_data()
    size_token = data.get("size_token", DEFAULT_SIZE_TOKEN)
    # Frictionless: при входе сразу проставляем дефолтную коробку+вес, чтобы «Далі»
    # был доступен с порога. Коробку клиент меняет кнопками, точный вес — «Вказати вагу».
    if not data.get("weight"):
        default_weight = SIZE_DEFAULT_WEIGHT.get(
            size_token, SIZE_DEFAULT_WEIGHT[DEFAULT_SIZE_TOKEN]
        )
        await state.update_data(size_token=size_token, weight=default_weight)
        data = await state.get_data()
    text = texts.parcel_text(weight=data.get("weight"), size_token=size_token)
    kb = build_parcel_kb(size_token=size_token, weight_set=bool(data.get("weight")))
    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    if edit:
        await remember_screen(state, message)


def _cart_total(cart: dict) -> Decimal:
    total = Decimal("0")
    for entry in cart.values():
        if entry["price"] is not None:
            total += Decimal(entry["price"]) * entry["qty"]
    return total


def _cart_has_unpriced(cart: dict) -> bool:
    """Есть ли в корзине позиции без цены.

    Цена в «Складі» необязательна, и `_cart_total` такие позиции молча пропускает.
    Для накладного платежа это не критично, а для оголошеної вартості — да: сумма
    выйдет заниженной, а занижение здесь означает непокрытую посылку.
    """
    return any(entry["price"] is None for entry in cart.values())


def _apply_insured_defaults(data: dict, cart: dict, updates: dict) -> None:
    """Оголошена вартість = стоимость корзины, пока клиент не задал её сам.

    Ручной ввод пинит источник в `custom` — дальше корзина значение не перетирает
    (клиент вправе занизить сумму осознанно). Пустой итог (в «Складі» нет цен)
    оставляет сумму незаданной: молчаливый ноль — это и есть тот дефект, который
    чиним, поэтому лучше потребовать явного решения, чем подставить 0.
    """
    if data.get("insured_amount_source") == "custom":
        return
    total = _cart_total(cart)
    amount = f"{total:f}" if total > 0 else None
    source = "cart" if amount is not None else None
    # Пишем только при фактическом изменении: иначе каждый рендер карточки зря
    # дёргает FSM-хранилище.
    if data.get("insured_amount") != amount:
        updates["insured_amount"] = amount
    if data.get("insured_amount_source") != source:
        updates["insured_amount_source"] = source


async def _ensure_card_defaults(state: FSMContext) -> dict:
    """Дефолты карточки (страховка/опис/оплата/платник).

    Оголошена вартість, в отличие от остальных, пересчитывается на каждом показе
    карточки, пока клиент не задал её сам: НП компенсирует ущерб **в пределах
    оголошеної вартості**, поэтому добавленный после первого показа товар обязан её
    поднять. Раньше здесь стоял молчаливый `"0"` — посылка уходила незастрахованной,
    а страховой сбор не попадал в оценку, из-за чего цена в боте была ниже реальной.
    """
    data = await state.get_data()
    updates: dict = {}
    cart = data.get("cart", {})
    _apply_insured_defaults(data, cart, updates)
    if data.get("description") is None:
        names = list(dict.fromkeys(e["name"] for e in cart.values()))
        updates["description"] = (", ".join(names)[:100]) or "Товари"
    if data.get("payment_method") is None:
        updates["payment_method"] = "prepay"
    if data.get("payer_type") is None:
        updates["payer_type"] = "Recipient"
    if data.get("payment_method") == "cod":
        total = _cart_total(cart)
        # `== "cart"` в условии — тот же дефект, что чинится выше для страховки:
        # выведенная из корзины сумма обязана следовать за корзиной, иначе с
        # получателя возьмут деньги по устаревшему составу заказа. Своя сумма
        # (`custom`) не трогается.
        if data.get("cod_amount") is None or data.get("cod_amount_source") == "cart":
            if total > 0:
                updates["cod_amount"] = f"{total:f}"
                updates["cod_amount_source"] = "cart"
            else:
                # Корзина без цены → COD не на чём держать: откатываем на передоплату,
                # чтобы битое состояние (cod + None) не дошло до submit.
                updates["payment_method"] = "prepay"
                updates["cod_amount"] = None
                updates["cod_amount_source"] = None
    if updates:
        await state.update_data(**updates)
        data = await state.get_data()
    return data


def _price_key(data: dict) -> str:
    """Хэш влияющих на ориентировочный тариф полей.

    Размер пресета входит в ключ, потому что габариты влияют на цену напрямую:
    тариф считается по максимуму из фактического и объёмного веса, а объёмный
    берётся из габаритов коробки. Смена пресета обязана инвалидировать оценку.
    """
    parts = (
        data.get("sender_profile_id"),
        data.get("recipient_city_ref"),
        data.get("weight"),
        data.get("size_token"),
        data.get("insured_amount") or "",
        data.get("cod_amount") or "",
    )
    return "|".join(str(p) for p in parts)


async def _card_price(
    session: AsyncSession,
    client,
    data: dict,
    np_client: NovaPoshtaClient,
    *,
    force: bool,
    account_id: uuid.UUID | None = None,
) -> dict:
    """Цена НП с кэшем в FSM-data по `_price_key` + graceful-degradation."""
    key = _price_key(data)
    cached = data.get("price_cache")
    if not force and cached and cached.get("key") == key:
        return cached
    result: dict = {"key": key}
    # Защита от устаревшей кнопки (напр. recompute на сброшенном FSM): без обязательных
    # полей не дёргаем НП и не падаем KeyError — показываем «розрахунок недоступний».
    # Оголошеної вартості в этом списке нет намеренно: когда её вывести неоткуда
    # (в «Складі» нет цен), клиент всё равно должен видеть стоимость доставки —
    # иначе вместо понятного «вкажіть вартість» он получит «розрахунок недоступний».
    if not (data.get("recipient_city_ref") and data.get("weight")):
        result["unavailable"] = True
        return result
    cod = data.get("cod_amount")
    try:
        quote = await pricing.quote_ttn(
            session,
            client=client,
            sender_profile_id=_profile_uuid(data),
            account_id=account_id,
            city_recipient_ref=data["recipient_city_ref"],
            weight=Decimal(data["weight"]),
            cost=Decimal(data.get("insured_amount") or "0"),
            cod_amount=Decimal(cod) if cod else None,
            # Дефолт `"s"` тот же, что и на пути создания ТТН (`_do_create`): при
            # пустом токене оценка ушла бы без габаритов, объёмный вес не поднял бы
            # тариф, и она занизила бы цену относительно фактически созданной ТТН.
            dimensions_cm=SIZE_DIMENSIONS.get(data.get("size_token") or DEFAULT_SIZE_TOKEN),
            np_client=np_client,
        )
        result["cost"] = f"{quote.cost:f}"
        result["redelivery"] = (
            f"{quote.cost_redelivery:f}" if quote.cost_redelivery is not None else None
        )
        result["eta"] = quote.estimated_delivery_date
        # Тарифный вес показываем только когда объёмный перебил фактический —
        # иначе клиент не поймёт, почему цена выше, чем он ждал по весу коробки.
        if quote.billable_weight is not None and quote.billable_weight > Decimal(data["weight"]):
            result["billable_weight"] = f"{quote.billable_weight:f}"
    except (ClientServiceError, NovaPoshtaError):
        # НП не дала Cost / недоступна — не блокируем оформление (подтвердит менеджер).
        result["unavailable"] = True
    return result


async def _show_card(
    message: Message,
    state: FSMContext,
    *,
    session: AsyncSession,
    client,
    np_client: NovaPoshtaClient,
    edit: bool,
    force_price: bool = False,
    account_id: uuid.UUID | None = None,
) -> None:
    data = await _ensure_card_defaults(state)
    price = await _card_price(
        session, client, data, np_client, force=force_price, account_id=account_id
    )
    await state.update_data(price_cache=price)
    text = texts.card_text(data, price)
    kb = build_card_kb(is_org=data.get("recipient_kind") == "organization")
    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    if edit:
        await remember_screen(state, message)


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
        offset = max(0, int(callback.data.split(":")[3]))
    except (IndexError, ValueError):
        await callback.answer(_STALE, show_alert=True)
        return
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    await state.set_state(CreateTtnState.picking_items)
    try:
        await _show_picker(
            callback.message,
            db_session,
            client,
            state,
            offset=offset,
            edit=True,
            account_id=_account_id(effective_context),
            account=_account(effective_context),
        )
    except PermissionDenied as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data == "cab:ttn:search")
async def cb_search_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    await state.set_state(CreateTtnState.entering_item_search)
    await callback.message.answer(
        "Введіть SKU, назву або категорію товару.", reply_markup=build_cancel_kb(back="items")
    )
    await callback.answer()


@router.callback_query(F.data == "cab:ttn:searchclear")
async def cb_search_clear(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    # Сбрасываем и фильтры, и корзину (выбранные товары) — раньше чистились только
    # фильтры, из-за чего кнопка «Скинути» не очищала набор и казалась нерабочей.
    await state.update_data(ttn_query=None, ttn_category=None, cart={}, pending=None)
    await state.set_state(CreateTtnState.picking_items)
    await _show_picker(
        callback.message,
        db_session,
        client,
        state,
        offset=0,
        edit=True,
        account_id=_account_id(effective_context),
        account=_account(effective_context),
    )
    await callback.answer("Кошик і фільтри очищено.")


@router.callback_query(F.data.startswith("cab:ttn:pcat:"))
async def cb_pick_category(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    """Фильтр товаров по категории в пикере ТТН (как `cab:pcat` в «Товари»)."""
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    code = callback.data.split(":")[3]
    if code == "all":
        await state.update_data(ttn_category=None)
    else:
        try:
            idx = int(code)
        except ValueError:
            await callback.answer(_STALE, show_alert=True)
            return
        categories = (await state.get_data()).get("ttn_categories", [])
        if idx < 0 or idx >= len(categories):
            await callback.answer(_STALE, show_alert=True)
            return
        await state.update_data(ttn_category=categories[idx])
    await state.set_state(CreateTtnState.picking_items)
    await _show_picker(
        callback.message,
        db_session,
        client,
        state,
        offset=0,
        edit=True,
        account_id=_account_id(effective_context),
        account=_account(effective_context),
    )
    await callback.answer()


@router.message(
    # Экран пикера прямо предлагает «Шукайте за SKU/назвою/категорією», но раньше
    # текст принимался ТОЛЬКО после кнопки «🔎 Пошук» (`entering_item_search`).
    # Человек, набравший артикул на самом экране выбора, не получал ничего: ни
    # результатов, ни отказа — экран обещал то, чего не делал. Найдено E2E-прогоном
    # (13 случаев «текст → тишина» у всех ролей).
    StateFilter(CreateTtnState.entering_item_search, CreateTtnState.picking_items),
    F.text,
    ~F.text.startswith("/"),
    # Кнопка нижней панели — не поисковый запрос. `menu_escape` чистит состояние, но
    # `raw_state` для фильтров уже вычислен, поэтому исключаем явно (паттерн `802022a`).
    ~F.text.in_(MENU_TEXTS),
)
async def receive_item_search(
    message: Message,
    bot: Bot,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    client = _effective_client(effective_context)
    if client is None:
        await message.answer("Авторизуйтесь через /start.")
        return
    # Поиск сбрасывает фильтр-категорию (как в «Товари»): ищем по всему складу.
    await state.update_data(ttn_query=(message.text or "").strip(), ttn_category=None)
    await state.set_state(CreateTtnState.picking_items)
    query = (await state.get_data()).get("ttn_query")
    page = await list_inventory(
        db_session,
        client=client,
        query=query,
        limit=TTN_PAGE_SIZE,
        offset=0,
        account_id=_account_id(effective_context),
        account=_account(effective_context),
    )
    text, kb = await _prepare_picker(state, page)
    await answer_latest_screen(bot, message, state, text, reply_markup=kb, parse_mode="HTML")


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
    # Товар берём из `picker_skus` — списка SKU той страницы, которую и нарисовали.
    # Раньше здесь шло повторное чтение склада БЕЗ активной категории, и индекс
    # отфильтрованной страницы резолвился против нефильтрованной: тап по товару в
    # наличии отвечал «немає на залишку» про совсем другой товар (или, что хуже,
    # молча клал в корзину не тот SKU).
    skus = (await state.get_data()).get("picker_skus", [])
    if idx < 0 or idx >= len(skus):
        await callback.answer(_STALE, show_alert=True)
        return
    item = await find_inventory_item(
        db_session,
        client=client,
        sku=skus[idx],
        account_id=_account_id(effective_context),
        account=_account(effective_context),
    )
    if item is None:
        # Позиция исчезла со склада между рендером и тапом. Молчать нельзя:
        # перерисовываем пикер, чтобы кнопки снова совпадали со складом.
        await state.set_state(CreateTtnState.picking_items)
        await _show_picker(
            callback.message,
            db_session,
            client,
            state,
            offset=(await state.get_data()).get("cart_offset", 0),
            edit=True,
            account_id=_account_id(effective_context),
            account=_account(effective_context),
        )
        await callback.answer("Товар більше не доступний — список оновлено.", show_alert=True)
        return
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
    await callback.message.answer(
        texts.qty_prompt_text(item), reply_markup=build_cancel_kb(back="qty")
    )
    await callback.answer()


@router.message(CreateTtnState.entering_qty, F.text, ~F.text.startswith("/"))
async def receive_qty(message: Message, bot: Bot, state: FSMContext) -> None:
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
    item = InventoryItem(
        sku=pending["sku"],
        name=pending["name"],
        category=None,
        stock=pending["available"],
        reserved=0,
        available=pending["available"],
        price=Decimal(pending["price"]) if pending["price"] is not None else None,
    )
    # Режим правки переживает ввод числа: иначе после «Ввести число» степпер снова
    # предлагал бы «✓ Додати» и количество прибавилось бы к тому, что уже в кошику.
    editing = pending.get("mode") == "edit"
    await answer_latest_screen(
        bot,
        message,
        state,
        texts.stepper_text(item, pending["qty"], edit=editing),
        reply_markup=build_stepper_kb(
            qty=pending["qty"], available=pending["available"], edit=editing
        ),
        parse_mode="HTML",
    )


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
    # Правка позиции ЗАМЕНЯЕТ количество, добор из пикера — прибавляет. Без этого
    # различия «✏️» работала как «додати»: правка 2 шт при уже лежащих 2 давала 4,
    # а вместе с ней завышались оголошена вартість и накладений платіж — обе суммы
    # считаются из `_cart_total`. Старые `pending` из Redis ключа `mode` не имеют →
    # прежнее поведение «додати», незавершённые сессии не ломаются.
    editing = pending.get("mode") == "edit"
    # Сумма в корзине не должна превышать остаток (пред-проверка; create_shipment
    # всё равно валидирует InsufficientStock на отправке).
    total = min(pending["qty"] if editing else prev + pending["qty"], pending["available"])
    cart[sku] = {"qty": total, "name": pending["name"], "price": pending["price"]}
    await state.update_data(cart=cart, pending=None)
    # Возвращаем состояние в picking_items: если пользователь до этого жал «Ввести
    # число» (entering_qty), без сброса последующий текст ушёл бы в receive_qty.
    await state.set_state(CreateTtnState.picking_items)
    if editing:
        # Правку затевают из кошика — туда и возвращаемся: результат правки виден
        # сразу, а не после отдельного тапа по «🧺 Кошик».
        await _show_cart(callback.message, state)
        await callback.answer(f"Оновлено: {pending['name']} ×{total}")
        return
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
            account_id=_account_id(effective_context),
            account=_account(effective_context),
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
    if idx < 0 or idx >= len(skus):
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
    if idx < 0 or idx >= len(skus):
        await callback.answer(_STALE, show_alert=True)
        return
    sku = skus[idx]
    entry = cart[sku]
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    # Остаток для редактирования берём актуальный (а не сохранённый в корзине).
    # Именно точным поиском по SKU: `list_inventory(query=sku)` матчит подстроку и
    # ограничен страницей — при общем префиксе нужная позиция в выдачу не попадала,
    # и `available` тихо откатывался к количеству из корзины (устаревший максимум).
    match = await find_inventory_item(
        db_session,
        client=client,
        sku=sku,
        account_id=_account_id(effective_context),
        account=_account(effective_context),
    )
    if match is None or match.available <= 0:
        # Позиция исчезла со склада или кончилась, пока лежала в кошику. Редактировать
        # нечего: степпер с максимумом 0 нерабочий (шаг «−1» не опускается ниже 1), а
        # `None` раньше подставлял в остаток количество из корзины — правка «успешно»
        # подтверждала недоступный товар, и ошибка вылезала только на создании
        # отправления. Как и `cb_pick`, не молчим: перерисовываем кошик и говорим,
        # что делать.
        await _show_cart(callback.message, state)
        await callback.answer(
            f"«{entry['name']}» більше немає на залишку — приберіть позицію з кошика.",
            show_alert=True,
        )
        return
    # Потолок — фактический остаток. Раньше здесь стоял `max(available, entry["qty"])`:
    # он был нужен аддитивной семантике, но при замене не даёт опустить позицию до
    # реального наличия.
    available = match.available
    await state.update_data(
        pending={
            "sku": sku,
            "name": entry["name"],
            "available": available,
            "price": entry["price"],
            "qty": max(1, min(entry["qty"], available)),
            "mode": "edit",
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
    # Выбор коробки подставляет вес (верхняя граница тира) → активирует «Далі».
    # Точный вес клиент при желании задаёт кнопкой «⚖️ Вказати вагу».
    box_weight = SIZE_DEFAULT_WEIGHT.get(token, SIZE_DEFAULT_WEIGHT[DEFAULT_SIZE_TOKEN])
    await state.update_data(size_token=token, weight=box_weight)
    await _show_parcel(callback.message, state)
    await callback.answer(f"{SIZE_PRESETS[token]} · {box_weight} кг")


@router.callback_query(F.data == "cab:ttn:wt")
async def cb_weight_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    await state.set_state(CreateTtnState.entering_weight)
    await callback.message.answer(
        texts.weight_prompt_text(), reply_markup=build_cancel_kb(back="parcel")
    )
    await callback.answer()


@router.message(CreateTtnState.entering_weight, F.text, ~F.text.startswith("/"))
async def receive_weight(message: Message, bot: Bot, state: FSMContext) -> None:
    weight = _parse_weight(message.text or "")
    if weight is None:
        await message.answer(texts.weight_invalid_text())
        return
    await state.update_data(weight=weight)
    await state.set_state(CreateTtnState.picking_parcel)
    data = await state.get_data()
    await answer_latest_screen(
        bot,
        message,
        state,
        texts.parcel_text(
            weight=data.get("weight"),
            size_token=data.get("size_token", DEFAULT_SIZE_TOKEN),
        ),
        reply_markup=build_parcel_kb(
            size_token=data.get("size_token", DEFAULT_SIZE_TOKEN),
            weight_set=bool(data.get("weight")),
        ),
        parse_mode="HTML",
    )


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
    await state.set_state(CreateTtnState.entering_recipient_name)
    await callback.message.answer(
        texts.recipient_name_prompt(kind), reply_markup=build_cancel_kb(back="recipient_kind")
    )
    await callback.answer()


# ----------------------------------------------------------- дані отримувача (текст)


@router.message(CreateTtnState.entering_recipient_name, F.text, ~F.text.startswith("/"))
async def receive_recipient_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    kind = (await state.get_data()).get("recipient_kind", "person")
    if not _recipient_name_ok(kind, name):
        await message.answer(texts.recipient_name_invalid(kind))
        return
    await state.update_data(recipient_name=name)
    if kind == "organization":
        await state.set_state(CreateTtnState.entering_recipient_edrpou)
        await message.answer(
            texts.edrpou_prompt(), reply_markup=build_cancel_kb(back="recipient_name")
        )
    else:
        await state.set_state(CreateTtnState.entering_recipient_phone)
        await message.answer(
            texts.phone_prompt(), reply_markup=build_cancel_kb(back="recipient_details")
        )


@router.message(CreateTtnState.entering_recipient_edrpou, F.text, ~F.text.startswith("/"))
async def receive_recipient_edrpou(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not _valid_edrpou(raw):
        await message.answer(texts.edrpou_invalid())
        return
    await state.update_data(recipient_edrpou=raw)
    await state.set_state(CreateTtnState.entering_recipient_phone)
    await message.answer(
        texts.phone_prompt(), reply_markup=build_cancel_kb(back="recipient_details")
    )


@router.message(CreateTtnState.entering_recipient_phone, F.text, ~F.text.startswith("/"))
async def receive_recipient_phone(message: Message, state: FSMContext) -> None:
    phone = _normalize_phone(message.text or "")
    if phone is None:
        await message.answer(texts.phone_invalid())
        return
    await state.update_data(recipient_phone=phone)
    await state.set_state(CreateTtnState.entering_city_query)
    await message.answer(
        texts.city_prompt(), reply_markup=build_cancel_kb(back="recipient_phone"), parse_mode="HTML"
    )


# ------------------------------------------------------------------ місто (пошук НП)


@router.message(CreateTtnState.entering_city_query, F.text, ~F.text.startswith("/"))
async def receive_city_query(
    message: Message,
    bot: Bot,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    np_client: NovaPoshtaClient,
    np_cache: NPReferenceCache,
) -> None:
    client = _effective_client(effective_context)
    if client is None:
        await message.answer("Авторизуйтесь через /start.")
        return
    query = (message.text or "").strip()
    if not query:
        await message.answer("Введіть назву міста, наприклад Київ.")
        return
    await _typing(bot, message.chat.id)
    try:
        cities = await address.search_cities(
            db_session,
            client=client,
            query=query,
            np_client=np_client,
            cache=np_cache,
            sender_profile_id=_profile_uuid(await state.get_data()),
            account_id=_account_id(effective_context),
        )
    except ClientServiceError as exc:
        await message.answer(str(exc))
        return
    except NovaPoshtaError:
        await message.answer(texts.search_unavailable_text())
        return
    if not cities:
        await message.answer(texts.city_not_found(query))
        return
    serial = [{"ref": c.ref, "name": c.name, "area": c.area} for c in cities]
    await state.update_data(cities=serial)

    # Если введённое название точно совпало с единственным городом от НП, сразу
    # переходим к отделениям. Неоднозначные результаты по-прежнему показываем
    # кнопками, чтобы пользователь сам выбрал нужный населённый пункт.
    normalized_query = _normalized_city_name(query)
    exact = [city for city in serial if _normalized_city_name(city["name"]) == normalized_query]
    if len(exact) == 1:
        city = exact[0]
        await state.update_data(recipient_city_ref=city["ref"], recipient_city_name=city["name"])
        try:
            whs = await address.search_warehouses(
                db_session,
                client=client,
                city_ref=city["ref"],
                np_client=np_client,
                cache=np_cache,
                sender_profile_id=_profile_uuid(await state.get_data()),
                account_id=_account_id(effective_context),
            )
        except ClientServiceError as exc:
            await message.answer(str(exc))
            return
        except NovaPoshtaError:
            await message.answer(texts.search_unavailable_text())
            return
        if not whs:
            await message.answer(
                texts.warehouse_none_text(city["name"]),
                reply_markup=build_cancel_kb(back="recipient_phone"),
            )
            return
        warehouses = [
            {
                "ref": warehouse.ref,
                "number": warehouse.number,
                "description": warehouse.description,
            }
            for warehouse in whs
        ]
        await state.update_data(warehouses=warehouses, wh_offset=0)
        await state.set_state(CreateTtnState.entering_warehouse_query)
        await _show_warehouses(message, state, offset=0, edit=False)
        return

    await answer_latest_screen(
        bot,
        message,
        state,
        texts.city_results_text(query),
        reply_markup=build_city_results_kb(serial),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("cab:ttn:back:"))
async def cb_back(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    """Вернуть пользователя на предыдущий логический шаг мастера ТТН."""
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        step = callback.data.split(":")[3]
    except IndexError:
        await callback.answer(_STALE, show_alert=True)
        return
    client = _effective_client(effective_context)

    if step == "items":
        if client is None:
            await callback.answer("Авторизуйтесь через /start.", show_alert=True)
            return
        await state.set_state(CreateTtnState.picking_items)
        await _show_picker(
            callback.message,
            db_session,
            client,
            state,
            offset=0,
            edit=True,
            account_id=_account_id(effective_context),
            account=_account(effective_context),
        )
    elif step == "qty":
        if (await state.get_data()).get("pending") is None:
            await callback.answer(_STALE, show_alert=True)
            return
        await state.set_state(CreateTtnState.picking_items)
        await _show_stepper(callback.message, state, edit=True)
    elif step == "parcel":
        await state.set_state(CreateTtnState.picking_parcel)
        await _show_parcel(callback.message, state, edit=True)
    elif step == "recipient_kind":
        await state.set_state(CreateTtnState.picking_recipient_kind)
        await callback.message.edit_text(
            texts.recipient_kind_text(), reply_markup=build_recipient_kind_kb(), parse_mode="HTML"
        )
    elif step == "recipient_name":
        kind = (await state.get_data()).get("recipient_kind", "person")
        await state.set_state(CreateTtnState.entering_recipient_name)
        await callback.message.edit_text(
            texts.recipient_name_prompt(kind), reply_markup=build_cancel_kb(back="recipient_kind")
        )
    elif step == "recipient_details":
        kind = (await state.get_data()).get("recipient_kind")
        if kind == "organization":
            await state.set_state(CreateTtnState.entering_recipient_edrpou)
            await callback.message.edit_text(
                texts.edrpou_prompt(), reply_markup=build_cancel_kb(back="recipient_name")
            )
        else:
            await state.set_state(CreateTtnState.entering_recipient_name)
            await callback.message.edit_text(
                texts.recipient_name_prompt("person"),
                reply_markup=build_cancel_kb(back="recipient_kind"),
            )
    elif step == "recipient_phone":
        await state.update_data(
            recipient_city_ref=None,
            recipient_city_name=None,
            recipient_warehouse_ref=None,
            recipient_warehouse_name=None,
            warehouses=None,
        )
        await state.set_state(CreateTtnState.entering_recipient_phone)
        await callback.message.edit_text(
            texts.phone_prompt(), reply_markup=build_cancel_kb(back="recipient_details")
        )
    elif step == "city":
        await state.update_data(
            recipient_warehouse_ref=None,
            recipient_warehouse_name=None,
            warehouses=None,
        )
        await state.set_state(CreateTtnState.entering_city_query)
        await callback.message.edit_text(
            texts.city_prompt(),
            reply_markup=build_cancel_kb(back="recipient_phone"),
            parse_mode="HTML",
        )
    elif step == "warehouse":
        warehouses = (await state.get_data()).get("warehouses", [])
        if not warehouses:
            await callback.answer(_STALE, show_alert=True)
            return
        await state.set_state(CreateTtnState.entering_warehouse_query)
        await _show_warehouses(callback.message, state, offset=0, edit=True)
    else:
        await callback.answer(_STALE, show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("cab:ttn:city:"))
async def cb_city(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    np_client: NovaPoshtaClient,
    np_cache: NPReferenceCache,
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
    data = await state.get_data()
    cities = data.get("cities", [])
    if idx < 0 or idx >= len(cities):
        await callback.answer(_STALE, show_alert=True)
        return
    city = cities[idx]
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    await state.update_data(recipient_city_ref=city["ref"], recipient_city_name=city["name"])
    try:
        whs = await address.search_warehouses(
            db_session,
            client=client,
            city_ref=city["ref"],
            np_client=np_client,
            cache=np_cache,
            sender_profile_id=_profile_uuid(data),
            account_id=_account_id(effective_context),
        )
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    except NovaPoshtaError:
        await callback.answer(texts.search_unavailable_text(), show_alert=True)
        return
    if not whs:
        # Нет відділень — даём вернуться к выбору города (state остаётся «город»).
        await state.set_state(CreateTtnState.entering_city_query)
        await callback.message.edit_text(
            texts.warehouse_none_text(city["name"]),
            reply_markup=build_cancel_kb(back="recipient_phone"),
        )
        await callback.answer()
        return
    serial = [{"ref": w.ref, "number": w.number, "description": w.description} for w in whs]
    await state.update_data(warehouses=serial, wh_offset=0)
    await state.set_state(CreateTtnState.entering_warehouse_query)
    await _show_warehouses(callback.message, state, offset=0, edit=True)
    await callback.answer()


# --------------------------------------------------------------- відділення (пошук НП)


async def _show_warehouses(message: Message, state: FSMContext, *, offset: int, edit: bool) -> None:
    data = await state.get_data()
    whs = data.get("warehouses", [])
    city_name = data.get("recipient_city_name", "")
    text = texts.warehouse_results_text(city_name, total=len(whs))
    kb = build_warehouse_results_kb(whs, offset=offset)
    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    if edit:
        await remember_screen(state, message)


@router.callback_query(F.data.startswith("cab:ttn:whpage:"))
async def cb_wh_page(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        offset = max(0, int(callback.data.split(":")[3]))
    except (IndexError, ValueError):
        await callback.answer(_STALE, show_alert=True)
        return
    await state.update_data(wh_offset=offset)
    await _show_warehouses(callback.message, state, offset=offset, edit=True)
    await callback.answer()


@router.callback_query(F.data == "cab:ttn:whfind")
async def cb_wh_find(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    await state.set_state(CreateTtnState.entering_warehouse_query)
    await callback.message.answer(
        texts.warehouse_find_prompt(), reply_markup=build_cancel_kb(back="warehouse")
    )
    await callback.answer()


@router.message(CreateTtnState.entering_warehouse_query, F.text, ~F.text.startswith("/"))
async def receive_warehouse_query(
    message: Message,
    bot: Bot,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    np_client: NovaPoshtaClient,
    np_cache: NPReferenceCache,
) -> None:
    client = _effective_client(effective_context)
    if client is None:
        await message.answer("Авторизуйтесь через /start.")
        return
    data = await state.get_data()
    city_ref = data.get("recipient_city_ref")
    if not city_ref:
        await message.answer(_STALE)
        return
    query = (message.text or "").strip()
    await _typing(bot, message.chat.id)
    try:
        whs = await address.search_warehouses(
            db_session,
            client=client,
            city_ref=city_ref,
            np_client=np_client,
            cache=np_cache,
            query=query,
            sender_profile_id=_profile_uuid(data),
            account_id=_account_id(effective_context),
        )
    except ClientServiceError as exc:
        await message.answer(str(exc))
        return
    except NovaPoshtaError:
        await message.answer(texts.search_unavailable_text())
        return
    if not whs:
        await message.answer(f"За «{query}» відділень не знайдено. Спробуйте інакше.")
        return
    serial = [{"ref": w.ref, "number": w.number, "description": w.description} for w in whs]
    await state.update_data(warehouses=serial, wh_offset=0)
    city_name = data.get("recipient_city_name", "")
    await answer_latest_screen(
        bot,
        message,
        state,
        texts.warehouse_results_text(city_name, total=len(serial)),
        reply_markup=build_warehouse_results_kb(serial, offset=0),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("cab:ttn:wh:"))
async def cb_wh(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    np_client: NovaPoshtaClient,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        abs_idx = int(callback.data.split(":")[3])
    except (IndexError, ValueError):
        await callback.answer(_STALE, show_alert=True)
        return
    whs = (await state.get_data()).get("warehouses", [])
    if abs_idx < 0 or abs_idx >= len(whs):
        await callback.answer(_STALE, show_alert=True)
        return
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    wh = whs[abs_idx]
    await state.update_data(
        recipient_warehouse_ref=wh["ref"],
        recipient_warehouse_name=f"№{wh['number']}: {wh['description']}",
    )
    await state.set_state(CreateTtnState.summary)
    await _show_card(
        callback.message,
        state,
        session=db_session,
        client=client,
        np_client=np_client,
        edit=True,
        account_id=_account_id(effective_context),
    )
    await callback.answer()


# --------------------------------------------------------------------- картка-зведення


@router.callback_query(F.data == "cab:ttn:recompute")
async def cb_recompute(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    np_client: NovaPoshtaClient,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    await state.set_state(CreateTtnState.summary)
    await _show_card(
        callback.message,
        state,
        session=db_session,
        client=client,
        np_client=np_client,
        edit=True,
        force_price=True,
        account_id=_account_id(effective_context),
    )
    await callback.answer("Перераховано.")


# ------------------------------------------------------- картка: точкова правка ✏️

# Поля карточки с текстовым вводом: токен → (prompt, валидатор/апдейтер в receive_edit).
_TEXT_EDIT_TOKENS = {"name", "phone", "edrpou", "weight", "insured", "descr"}


def _recipient_name_ok(kind: str, value: str) -> bool:
    """Проверить имя получателя с учётом типа получателя."""
    return bool(value) and (kind != "person" or texts.recipient_person_name_valid(value))


@router.callback_query(CreateTtnState.summary, F.data == "cab:ttn:edit:sender")
async def cb_edit_sender(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    """Открыть выбор ФОП прямо из карточки-сводки."""
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    data = await state.get_data()
    if not data.get("sender_profile_id") or not data.get("cart"):
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        profiles = await sender_profile.list_profiles(
            db_session,
            actor=client,
            client_id=client.id,
            account_id=_account_id(effective_context),
        )
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    if not profiles:
        await callback.answer(_STALE, show_alert=True)
        return
    await state.set_state(CreateTtnState.picking_sender)
    await callback.message.edit_text(
        texts.pick_sender_text(),
        reply_markup=build_sender_edit_kb(profiles),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(
    CreateTtnState.picking_sender,
    F.data.startswith("cab:ttn:sender:"),
)
async def cb_edit_sender_pick(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    np_client: NovaPoshtaClient,
    state: FSMContext,
) -> None:
    """Применить ФОП, не очищая текущую карточку ТТН."""
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer(_STALE, show_alert=True)
        return
    data = await state.get_data()
    if not data.get("cart"):
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        profile_id = uuid.UUID(callback.data.split(":")[3])
    except (IndexError, ValueError):
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        resolved_id = await resolve_sender_id(
            db_session,
            client=client,
            profile_id=profile_id,
            account_id=_account_id(effective_context),
        )
        profile = await sender_profile.get_profile(
            db_session,
            actor=client,
            profile_id=resolved_id,
            account_id=_account_id(effective_context),
        )
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.update_data(sender_profile_id=str(resolved_id), sender_profile_name=profile.name)
    await state.set_state(CreateTtnState.summary)
    await _show_card(
        callback.message,
        state,
        session=db_session,
        client=client,
        np_client=np_client,
        edit=True,
        account_id=_account_id(effective_context),
    )
    await callback.answer(f"ФОП: {profile.name}")


async def _back_to_card(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    session: AsyncSession,
    np_client: NovaPoshtaClient,
    effective_context: EffectiveContext,
) -> bool:
    """Общий хвост для возврата на карточку из правки. False → не удалось (нет клиента)."""
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return False
    await state.set_state(CreateTtnState.summary)
    await _show_card(
        callback.message,
        state,
        session=session,
        client=client,
        np_client=np_client,
        edit=True,
        account_id=_account_id(effective_context),
    )
    return True


@router.callback_query(F.data == "cab:ttn:card")
async def cb_card(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    np_client: NovaPoshtaClient,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    if await _back_to_card(
        callback,
        state,
        session=db_session,
        np_client=np_client,
        effective_context=effective_context,
    ):
        await callback.answer()


def _edit_prompt(field: str, data: dict) -> str:
    if field == "name":
        return texts.recipient_name_prompt(data.get("recipient_kind", "person"))
    return {
        "phone": texts.phone_prompt(),
        "edrpou": texts.edrpou_prompt(),
        "weight": texts.weight_prompt_text(),
        "insured": texts.insured_prompt(),
        "descr": texts.description_prompt(),
        "cod_amount": texts.cod_amount_prompt(),
    }[field]


@router.callback_query(F.data.startswith("cab:ttn:edit:"))
async def cb_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    field = callback.data.split(":")[3]
    data = await state.get_data()
    if field in _TEXT_EDIT_TOKENS:
        if field == "edrpou" and data.get("recipient_kind") != "organization":
            await callback.answer(_STALE, show_alert=True)
            return
        await state.update_data(edit_field=field)
        await state.set_state(CreateTtnState.editing_field)
        await callback.message.answer(
            _edit_prompt(field, data), reply_markup=build_back_to_card_kb()
        )
        await callback.answer()
        return
    if field == "size":
        await callback.message.edit_text(
            texts.size_edit_text(), reply_markup=build_size_edit_kb(data.get("size_token", "s"))
        )
    elif field == "payer":
        await callback.message.edit_text(
            texts.payer_edit_text(),
            reply_markup=build_payer_edit_kb(data.get("payer_type", "Recipient")),
        )
    elif field == "pay":
        await callback.message.edit_text(
            texts.payment_edit_text(),
            reply_markup=build_payment_edit_kb(data.get("payment_method", "prepay")),
        )
    elif field == "city":
        # Перевыбор адреса: cb_wh в конце снова отрендерит карточку — возврат «даром».
        await state.set_state(CreateTtnState.entering_city_query)
        await callback.message.answer(
            texts.city_prompt(), reply_markup=build_back_to_card_kb(), parse_mode="HTML"
        )
    else:
        await callback.answer(_STALE, show_alert=True)
        return
    await callback.answer()


@router.message(CreateTtnState.editing_field, F.text, ~F.text.startswith("/"))
async def receive_edit(
    message: Message,
    bot: Bot,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    np_client: NovaPoshtaClient,
) -> None:
    field = (await state.get_data()).get("edit_field")
    raw = (message.text or "").strip()
    updates: dict = {}
    if field == "name":
        kind = (await state.get_data()).get("recipient_kind", "person")
        if not _recipient_name_ok(kind, raw):
            await message.answer(texts.recipient_name_invalid(kind))
            return
        updates["recipient_name"] = raw
    elif field == "phone":
        phone = _normalize_phone(raw)
        if phone is None:
            await message.answer(texts.phone_invalid())
            return
        updates["recipient_phone"] = phone
    elif field == "edrpou":
        if not _valid_edrpou(raw):
            await message.answer(texts.edrpou_invalid())
            return
        updates["recipient_edrpou"] = raw
    elif field == "weight":
        weight = _parse_weight(raw)
        if weight is None:
            await message.answer(texts.weight_invalid_text())
            return
        updates["weight"] = weight
    elif field == "insured":
        # Без `positive=True`: 0 остаётся вводимым — это осознанный отказ от
        # страховки, в отличие от молчаливого дефолта, который мы убрали.
        amount = _parse_money(raw)
        if amount is None:
            await message.answer(texts.insured_invalid())
            return
        updates["insured_amount"] = amount
        updates["insured_amount_source"] = "custom"
    elif field == "descr":
        if not raw:
            await message.answer(texts.description_invalid())
            return
        updates["description"] = raw[:100]
    elif field == "cod_amount":
        amount = _parse_money(raw, positive=True)
        if amount is None:
            await message.answer(texts.cod_invalid())
            return
        updates["payment_method"] = "cod"
        updates["cod_amount"] = amount
        updates["cod_amount_source"] = "custom"
    else:
        await state.set_state(CreateTtnState.summary)
        await message.answer(_STALE)
        return

    await state.update_data(**updates)
    await state.set_state(CreateTtnState.summary)
    client = _effective_client(effective_context)
    if client is None:
        await message.answer("Авторизуйтесь через /start.")
        return
    data = await _ensure_card_defaults(state)
    price = await _card_price(
        db_session, client, data, np_client, force=True, account_id=_account_id(effective_context)
    )
    await state.update_data(price_cache=price)
    await answer_latest_screen(
        bot,
        message,
        state,
        texts.card_text(data, price),
        reply_markup=build_card_kb(is_org=data.get("recipient_kind") == "organization"),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("cab:ttn:setsz:"))
async def cb_set_size(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    np_client: NovaPoshtaClient,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    token = callback.data.split(":")[3]
    if token not in SIZE_PRESETS:
        await callback.answer(_STALE, show_alert=True)
        return
    await state.update_data(size_token=token)
    if await _back_to_card(
        callback,
        state,
        session=db_session,
        np_client=np_client,
        effective_context=effective_context,
    ):
        await callback.answer(f"Габарити: {SIZE_PRESETS[token]}")


@router.callback_query(F.data.startswith("cab:ttn:setpr:"))
async def cb_set_payer(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    np_client: NovaPoshtaClient,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    payer = {"r": "Recipient", "s": "Sender"}.get(callback.data.split(":")[3])
    if payer is None:
        await callback.answer(_STALE, show_alert=True)
        return
    await state.update_data(payer_type=payer)
    if await _back_to_card(
        callback,
        state,
        session=db_session,
        np_client=np_client,
        effective_context=effective_context,
    ):
        await callback.answer()


@router.callback_query(F.data.startswith("cab:ttn:setpm:"))
async def cb_set_payment(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    np_client: NovaPoshtaClient,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    mode = callback.data.split(":")[3]
    if mode == "prepay":
        # Сброс COD: payment_method=prepay не должен тащить старую сумму.
        await state.update_data(payment_method="prepay", cod_amount=None, cod_amount_source=None)
        if await _back_to_card(
            callback,
            state,
            session=db_session,
            np_client=np_client,
            effective_context=effective_context,
        ):
            await callback.answer()
    elif mode == "cod":
        total = _cart_total((await state.get_data()).get("cart", {}))
        await callback.message.edit_text(
            texts.cod_amount_choice_text(), reply_markup=build_cod_amount_kb(total)
        )
        await callback.answer()
    else:
        await callback.answer(_STALE, show_alert=True)


@router.callback_query(F.data == "cab:ttn:cod:cart")
async def cb_set_cod_from_cart(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    np_client: NovaPoshtaClient,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    total = _cart_total((await state.get_data()).get("cart", {}))
    if total <= 0:
        await callback.answer("У кошику немає товарів із ціною.", show_alert=True)
        return
    await state.update_data(payment_method="cod", cod_amount=f"{total:f}", cod_amount_source="cart")
    if await _back_to_card(
        callback,
        state,
        session=db_session,
        np_client=np_client,
        effective_context=effective_context,
    ):
        await callback.answer("Накладений платіж: сума з кошика.")


@router.callback_query(F.data == "cab:ttn:cod:custom")
async def cb_set_cod_custom(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    await state.update_data(edit_field="cod_amount")
    await state.set_state(CreateTtnState.editing_field)
    await callback.message.answer(texts.cod_amount_prompt(), reply_markup=build_back_to_card_kb())
    await callback.answer()


# ----------------------------------------------------------------- відправлення ТТН


def _submit_error_text(exc: ClientServiceError, cart: dict) -> str:
    """Доменную ошибку create_shipment → понятный uk-текст для клиента."""
    if isinstance(exc, InsufficientStock):
        name = cart.get(exc.sku, {}).get("name", exc.sku)
        return f"❌ На залишку лише {exc.available} од. «{name}». Оновіть кошик і спробуйте ще раз."
    if isinstance(exc, SenderProfileNotValidated):
        return "❌ Ключ ФОП не підтверджено в НП. Зверніться до менеджера."
    if isinstance(exc, SenderProfileIncomplete):
        return (
            "❌ ФОП налаштований не до кінця (немає телефону/контакту відправника). "
            "Зверніться до менеджера."
        )
    if isinstance(exc, SenderDispatchNotConfigured):
        return "❌ Склад відправника не налаштований у системі. Зверніться до підтримки."
    if isinstance(exc, SenderProfileNotConfigured):
        return "❌ ФОП не налаштований. Зверніться до менеджера."
    if isinstance(exc, TtnCreationFailed):
        low = str(exc).lower()
        if "afterpayment" in low or "післяплат" in low or "контроль оплат" in low:
            return (
                "❌ Накладений платіж (контроль оплати) недоступний для цього ФОП. "
                "Зверніться до менеджера або оберіть передоплату."
            )
        return f"❌ Не вдалося створити ТТН: {exc}"
    return f"❌ Помилка: {exc}"


async def _show_success(message: Message, ttn_number: str | None) -> None:
    """Показать экран успеха — best-effort. ТТН уже создан в НП и пишется в БД
    (коммит у middleware ПОСЛЕ хендлера), поэтому сбой показа НЕ должен поднять
    исключение — иначе middleware откатит транзакцию и осиротит NP-ТТН."""
    text = texts.success_text(ttn_number)
    kb = build_success_kb()
    try:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramAPIError:
        try:
            await message.answer(text, reply_markup=kb, parse_mode="HTML")
        except TelegramAPIError:
            logger.warning("ttn_success_render_failed", ttn_number=ttn_number)


async def _do_create(
    session: AsyncSession,
    client,
    data: dict,
    np_client: NovaPoshtaClient,
    bot: Bot,
    *,
    account_id: uuid.UUID | None = None,
    account=None,
    actor_user_id: uuid.UUID | None = None,
):
    cart = data["cart"]
    cod = data.get("cod_amount")
    return await create_shipment(
        session,
        client=client,
        items=[(sku, entry["qty"]) for sku, entry in cart.items()],
        recipient_kind=data["recipient_kind"],
        recipient_name=data["recipient_name"],
        recipient_phone=data["recipient_phone"],
        recipient_city_ref=data["recipient_city_ref"],
        recipient_city_name=data["recipient_city_name"],
        recipient_warehouse_ref=data["recipient_warehouse_ref"],
        recipient_warehouse_name=data["recipient_warehouse_name"],
        weight=Decimal(data["weight"]),
        size_preset=SIZE_PRESETS.get(data.get("size_token") or DEFAULT_SIZE_TOKEN, "—"),
        size_dimensions=SIZE_DIMENSIONS.get(data.get("size_token") or DEFAULT_SIZE_TOKEN),
        description=data["description"],
        insured_amount=Decimal(data["insured_amount"]),
        np_client=np_client,
        payer_type=data.get("payer_type", "Recipient"),
        payment_method=data.get("payment_method", "prepay"),
        cod_amount=Decimal(cod) if cod else None,
        recipient_edrpou=data.get("recipient_edrpou"),
        sender_profile_id=_profile_uuid(data),
        notifier=BotNotifier(bot),
        account_id=account_id,
        account=account,
        actor_user_id=actor_user_id or client.id,
    )


@router.callback_query(F.data == "cab:ttn:send")
async def cb_submit(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    np_client: NovaPoshtaClient,
    bot: Bot,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    client = _effective_client(effective_context)
    if client is None:
        await callback.answer("Авторизуйтесь через /start.", show_alert=True)
        return
    data = await state.get_data()
    if not (data.get("recipient_warehouse_ref") and data.get("cart")):
        await callback.answer(_STALE, show_alert=True)
        return
    # Пересинк перед созданием: `_do_create` берёт позиции из живой корзины, а суммы
    # — из FSM. Корзину можно менять и после отрисовки карточки (кнопки «◀ Назад» и
    # устаревшие клавиатуры не привязаны к состоянию), и тогда без пересинка ТТН
    # ушла бы с суммой по прежнему составу заказа.
    data = await _ensure_card_defaults(state)
    if data.get("insured_amount") is None:
        await callback.answer(texts.insured_required_alert(), show_alert=True)
        return
    uid = client.telegram_id
    if uid in _SUBMITTING:  # single-flight: check+add без await между ними
        await callback.answer("ТТН вже відправляється, зачекайте…", show_alert=True)
        return
    _SUBMITTING.add(uid)
    await callback.answer("Створюємо ТТН…")
    try:
        try:
            card = await _do_create(
                db_session,
                client,
                data,
                np_client,
                bot,
                account_id=_account_id(effective_context),
                account=_account(effective_context),
                actor_user_id=client.id,
            )
        except ClientServiceError as exc:
            # NP-first: при ошибке в БД ничего нет — повтор безопасен, карточка остаётся.
            await callback.message.answer(_submit_error_text(exc, data.get("cart", {})))
            return
        await state.clear()
        await _show_success(callback.message, card.ttn_number)
    finally:
        _SUBMITTING.discard(uid)


@router.callback_query(F.data == "cab:ttn:again")
async def cb_again(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    await callback.answer()
    await start_create_ttn(callback.message, state, effective_context, db_session, edit=True)


@router.message(F.text == "🚚 Створити ТТН")
async def open_create_ttn(
    message: Message,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    """Reply-кнопка меню клиента → вход в поток создания ТТН."""
    await start_create_ttn(message, state, effective_context, db_session)


@router.callback_query(F.data == "home:ttn")
async def open_create_ttn_home(
    callback: CallbackQuery,
    state: FSMContext,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    await start_create_ttn(callback.message, state, effective_context, db_session, edit=True)
    await callback.answer()


# --------------------------------------------------------------------- скасування


@router.callback_query(F.data == "cab:ttn:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer(_STALE, show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text("Створення ТТН скасовано.")
    await callback.answer()
