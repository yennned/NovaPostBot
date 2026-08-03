"""Тесты потока создания ТТН — каркас + кошик (Фаза 4, PR 9a).

ФОП-гейт входа идёт на реальном Postgres (через `shipment.resolve_sender_id`
— то же предусловие, что и у `create_shipment`); набор корзины/степпер/параметри —
чистые (инвентарь замокан, БД не нужна).
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.bot.handlers import ttn as h
from app.bot.states import CreateTtnState
from app.bot.texts import ttn as ttn_texts
from app.bot.types import ClientAccountContext, EffectiveContext
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import (
    ClientAccountRepository,
    SenderProfileRepository,
    UserRepository,
)
from app.novaposhta.schemas import City, PriceQuote, Warehouse
from app.services.inventory import InventoryItem, InventoryPage
from sqlalchemy.ext.asyncio import AsyncSession


class FakeState:
    def __init__(self, **data) -> None:
        self.cleared = False
        self.state = None
        self._data = dict(data)

    async def clear(self) -> None:
        self.cleared = True
        self.state = None
        self._data = {}

    async def set_state(self, value) -> None:
        self.state = value

    async def update_data(self, **kw) -> None:
        self._data.update(kw)

    async def get_data(self) -> dict:
        return self._data


class FakeBot:
    """Минимальный бот для индикатора поиска и проверки старых экранов."""

    def __init__(self) -> None:
        self.actions: list[dict] = []
        self.edits: list[dict] = []

    async def send_chat_action(self, **kw) -> None:
        self.actions.append(kw)

    async def edit_message_text(self, text, reply_markup=None, **kw) -> None:
        self.edits.append({"text": text, "reply_markup": reply_markup})


class FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.chat = SimpleNamespace(id=1)
        self.answers: list[dict] = []
        self.edits: list[dict] = []

    async def answer(self, text, reply_markup=None, parse_mode=None) -> None:
        self.answers.append({"text": text, "reply_markup": reply_markup})

    async def edit_text(self, text, reply_markup=None, parse_mode=None) -> None:
        self.edits.append({"text": text, "reply_markup": reply_markup})


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = FakeMessage()
        self.acks: list[dict] = []

    async def answer(self, text=None, show_alert=False) -> None:
        self.acks.append({"text": text, "show_alert": show_alert})


def _item(
    sku: str,
    name: str,
    available: int,
    price: str | None = "100",
    category: str | None = None,
) -> InventoryItem:
    return InventoryItem(
        sku=sku,
        name=name,
        category=category,
        stock=available,
        reserved=0,
        available=available,
        price=Decimal(price) if price is not None else None,
    )


def _page(
    items: list[InventoryItem], *, offset: int = 0, total: int | None = None
) -> InventoryPage:
    return InventoryPage(
        items=items,
        total=total if total is not None else len(items),
        limit=h.TTN_PAGE_SIZE,
        offset=offset,
        categories=[],
    )


def _patch_inventory(monkeypatch, page: InventoryPage) -> None:
    async def fake_list_inventory(
        session,
        *,
        client,
        query=None,
        category=None,
        limit=8,
        offset=0,
        reader=None,
        **kwargs,
    ):
        return page

    async def fake_find_inventory_item(session, *, client, sku, **kwargs):
        return next((item for item in page.items if item.sku == sku), None)

    monkeypatch.setattr(h, "list_inventory", fake_list_inventory)
    monkeypatch.setattr(h, "find_inventory_item", fake_find_inventory_item)


def _patch_inventory_from_items(monkeypatch, items: list[InventoryItem]) -> None:
    """Фейк склада, ЧЕСТНО применяющий `query`/`category`/`offset`.

    `_patch_inventory` отдаёт одну и ту же страницу на любые аргументы, поэтому
    структурно не способен поймать рассинхрон «клавиатуру нарисовали с фильтром, а
    тап резолвили без него» — тесты `cb_pick` были зелёными на сломанном коде.
    Фильтрация повторяет `app.services.inventory.list_inventory`; сам сервис здесь
    не переиспользуем, потому что он тянет `reserved` из БД, а эти тесты чистые.
    """

    async def fake_list_inventory(
        session,
        *,
        client,
        query=None,
        category=None,
        limit=h.TTN_PAGE_SIZE,
        offset=0,
        reader=None,
        **kwargs,
    ):
        rows = list(items)
        categories = sorted({item.category for item in rows if item.category})
        if query:
            needle = query.strip().lower()
            rows = [
                item
                for item in rows
                if needle in item.sku.lower()
                or needle in item.name.lower()
                or needle in (item.category or "").lower()
            ]
        if category:
            rows = [item for item in rows if (item.category or "").lower() == category.lower()]
        return InventoryPage(
            items=rows[offset : offset + limit],
            total=len(rows),
            limit=limit,
            offset=offset,
            categories=categories,
        )

    async def fake_find_inventory_item(session, *, client, sku, **kwargs):
        return next((item for item in items if item.sku == sku), None)

    monkeypatch.setattr(h, "list_inventory", fake_list_inventory)
    monkeypatch.setattr(h, "find_inventory_item", fake_find_inventory_item)


def _ctx(client):
    # Настоящий EffectiveContext, а не SimpleNamespace: фейк не имел `account`/
    # `account_context`, и хендлеры держались только на `getattr(..., None)`.
    # Пользователь остаётся фейковым — здесь важна форма контекста, не User.
    return EffectiveContext(
        actor_user=client,
        effective_user=client,
        effective_role=UserRole.client,
        is_dev=False,
    )


def _ctx_with_account(client, account_id):
    """Контекст работника: `account_context` заполнен, как это делает мидлварь."""
    account = SimpleNamespace(id=account_id)
    return EffectiveContext(
        actor_user=client,
        effective_user=client,
        effective_role=UserRole.client,
        is_dev=False,
        account_context=ClientAccountContext(
            user=client, account=account, membership=SimpleNamespace(id="mid")
        ),
    )


_CLIENT = SimpleNamespace(id="cid", telegram_id=900)


# --------------------------------------------------------------- ФОП-гейт (Postgres)


async def _active_client(session: AsyncSession, telegram_id: int):
    return await UserRepository(session).create(
        telegram_id=telegram_id, full_name="Клієнт", role=UserRole.client, status=UserStatus.active
    )


async def test_entry_no_profile(db_session: AsyncSession):
    client = await _active_client(db_session, 901)
    msg = FakeMessage()
    state = FakeState()
    await h.start_create_ttn(msg, state, _ctx(client), db_session)
    assert "ФОП ще не налаштований" in msg.answers[-1]["text"]
    assert state.state is None  # в поток не вошли


async def test_entry_profile_not_validated(db_session: AsyncSession):
    client = await _active_client(db_session, 902)
    await SenderProfileRepository(db_session).create(
        client_id=client.id, name="ФОП", np_api_key="k", is_default=True
    )  # без np_sender_ref → не провалидирован
    msg = FakeMessage()
    state = FakeState()
    await h.start_create_ttn(msg, state, _ctx(client), db_session)
    assert "не підтверджено" in msg.answers[-1]["text"]
    assert state.state is None


async def test_entry_profile_incomplete(db_session: AsyncSession):
    client = await _active_client(db_session, 904)
    # ключ валиден (np_sender_ref есть), но нет телефона/контакта отправителя
    await SenderProfileRepository(db_session).create(
        client_id=client.id, name="ФОП", np_api_key="k", is_default=True, np_sender_ref="cp-1"
    )
    msg = FakeMessage()
    state = FakeState()
    await h.start_create_ttn(msg, state, _ctx(client), db_session)
    assert "не до кінця" in msg.answers[-1]["text"]
    assert state.state is None


async def test_entry_dispatch_not_configured(db_session: AsyncSession, monkeypatch):
    client = await _active_client(db_session, 905)
    # профиль полный, но склад-отправитель системы (NP_SENDER_*) не задан
    monkeypatch.setenv("NP_SENDER_CITY_REF", "")
    monkeypatch.setenv("NP_SENDER_WAREHOUSE_REF", "")
    await SenderProfileRepository(db_session).create(
        client_id=client.id,
        name="ФОП",
        np_api_key="k",
        is_default=True,
        np_sender_ref="cp-1",
        np_contact_ref="ct-1",
        sender_phone="+380501112233",
    )
    msg = FakeMessage()
    state = FakeState()
    await h.start_create_ttn(msg, state, _ctx(client), db_session)
    assert "Склад відправника не налаштований" in msg.answers[-1]["text"]
    assert state.state is None


async def test_entry_ok_shows_picker(db_session: AsyncSession, monkeypatch):
    client = await _active_client(db_session, 903)
    # склад-отправитель системы задан + профиль полный (ключ/контакт/телефон)
    monkeypatch.setenv("NP_SENDER_CITY_REF", "sender-city")
    monkeypatch.setenv("NP_SENDER_WAREHOUSE_REF", "sender-wh")
    await SenderProfileRepository(db_session).create(
        client_id=client.id,
        name="ФОП",
        np_api_key="k",
        is_default=True,
        np_sender_ref="cp-1",
        np_contact_ref="ct-1",
        sender_phone="+380501112233",
    )
    _patch_inventory(monkeypatch, _page([_item("SKU1", "Товар", 10)]))
    msg = FakeMessage()
    state = FakeState()
    await h.start_create_ttn(msg, state, _ctx(client), db_session)
    assert state.state == CreateTtnState.picking_items
    assert state._data["sender_profile_id"]
    assert state._data["cart"] == {}
    assert state._data["nonce"]
    assert "Створення ТТН" in msg.answers[-1]["text"]


def _capture_inventory(monkeypatch, page: InventoryPage) -> dict:
    """Как `_patch_inventory`, но запоминает kwargs — иначе проброс не проверить."""
    seen: dict = {}

    async def fake_list_inventory(session, *, client, **kwargs):
        seen.update(kwargs)
        seen["client"] = client
        return page

    monkeypatch.setattr(h, "list_inventory", fake_list_inventory)
    return seen


async def _account_ctx(session, client):
    """Контекст с РЕАЛЬНЫМ аккаунтом: `_require_account_actor` сверяет членство в БД."""
    membership = await ClientAccountRepository(session).get_membership(user_id=client.id)
    account = membership.account
    return SimpleNamespace(
        effective_user=client, actor_user=client, account=account, membership=membership
    ), account


async def test_entry_passes_account_to_inventory(db_session: AsyncSession, monkeypatch):
    # Регрессия: `_resolve_sender_and_begin` передавал в `_show_picker` только
    # `account_id`, но не `account`. `list_inventory` берёт ключ листа от
    # `account or client`, поэтому работник видел свой склад вместо складу
    # магазина. Пара (account_id, account) обязана ехать вместе.
    client = await _active_client(db_session, 930)
    monkeypatch.setenv("NP_SENDER_CITY_REF", "sender-city")
    monkeypatch.setenv("NP_SENDER_WAREHOUSE_REF", "sender-wh")
    await _dispatchable_profile(db_session, client, "ФОП", is_default=True)
    ctx, account = await _account_ctx(db_session, client)
    seen = _capture_inventory(monkeypatch, _page([_item("SKU1", "Товар", 10)]))

    await h.start_create_ttn(FakeMessage(), FakeState(), ctx, db_session)

    assert seen["account"] is account
    assert seen["account_id"] == account.id


async def test_cb_pick_sender_passes_account_to_inventory(db_session: AsyncSession, monkeypatch):
    client = await _active_client(db_session, 931)
    monkeypatch.setenv("NP_SENDER_CITY_REF", "sender-city")
    monkeypatch.setenv("NP_SENDER_WAREHOUSE_REF", "sender-wh")
    profile = await _dispatchable_profile(db_session, client, "ФОП A", is_default=True)
    ctx, account = await _account_ctx(db_session, client)
    seen = _capture_inventory(monkeypatch, _page([_item("SKU1", "Товар", 10)]))

    await h.cb_pick_sender(FakeCallback(f"ttn:sender:{profile.id}"), FakeState(), ctx, db_session)

    assert seen["account"] is account
    assert seen["account_id"] == account.id


async def _dispatchable_profile(session, client, name, *, is_default):
    return await SenderProfileRepository(session).create(
        client_id=client.id,
        name=name,
        np_api_key="k",
        is_default=is_default,
        np_sender_ref="cp-1",
        np_contact_ref="ct-1",
        sender_phone="+380501112233",
    )


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def _button_labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


async def test_entry_multi_profile_shows_sender_picker(db_session: AsyncSession, monkeypatch):
    client = await _active_client(db_session, 910)
    monkeypatch.setenv("NP_SENDER_CITY_REF", "sender-city")
    monkeypatch.setenv("NP_SENDER_WAREHOUSE_REF", "sender-wh")
    await _dispatchable_profile(db_session, client, "ФОП A", is_default=True)
    await _dispatchable_profile(db_session, client, "ФОП B", is_default=False)
    msg = FakeMessage()
    state = FakeState()

    await h.start_create_ttn(msg, state, _ctx(client), db_session)

    assert state.state == CreateTtnState.picking_sender
    callbacks = _callbacks(msg.answers[-1]["reply_markup"])
    assert sum(cb.startswith("ttn:sender:") for cb in callbacks) == 2


async def test_cb_pick_sender_begins_cart(db_session: AsyncSession, monkeypatch):
    client = await _active_client(db_session, 911)
    monkeypatch.setenv("NP_SENDER_CITY_REF", "sender-city")
    monkeypatch.setenv("NP_SENDER_WAREHOUSE_REF", "sender-wh")
    await _dispatchable_profile(db_session, client, "ФОП A", is_default=True)
    chosen = await _dispatchable_profile(db_session, client, "ФОП B", is_default=False)
    _patch_inventory(monkeypatch, _page([_item("SKU1", "Товар", 10)]))
    state = FakeState()
    await state.set_state(CreateTtnState.picking_sender)
    cb = FakeCallback(f"ttn:sender:{chosen.id}")

    await h.cb_pick_sender(cb, state, _ctx(client), db_session)

    assert state.state == CreateTtnState.picking_items
    assert state._data["sender_profile_id"] == str(chosen.id)  # ушёл выбранный, не дефолт
    assert cb.message.edits  # кошик отрисован


async def test_edit_sender_requires_existing_cart(monkeypatch):
    state = FakeState(sender_profile_id=str(uuid4()), cart={})
    await state.set_state(CreateTtnState.summary)
    cb = FakeCallback("cab:ttn:edit:sender")

    await h.cb_edit_sender(cb, _ctx(_CLIENT), object(), state)

    assert cb.acks[-1]["show_alert"] is True
    assert state.state == CreateTtnState.summary


async def test_edit_sender_pick_preserves_cart_and_updates_profile(monkeypatch):
    profile_id = uuid4()
    profile = SimpleNamespace(id=profile_id, name="ФОП B")
    state = FakeState(
        cart={"SKU1": {"name": "Товар", "qty": 1, "price": "10"}},
        sender_profile_id=str(uuid4()),
    )
    await state.set_state(CreateTtnState.picking_sender)
    shown = {}

    async def fake_resolve_sender_id(*args, **kwargs):
        return profile_id

    async def fake_get_profile(*args, **kwargs):
        return profile

    async def fake_show_card(*args, **kwargs):
        shown["called"] = True

    monkeypatch.setattr(h, "resolve_sender_id", fake_resolve_sender_id)
    monkeypatch.setattr(h.sender_profile, "get_profile", fake_get_profile)
    monkeypatch.setattr(h, "_show_card", fake_show_card)

    await h.cb_edit_sender_pick(
        FakeCallback(f"cab:ttn:sender:{profile_id}"),
        _ctx(_CLIENT),
        object(),
        object(),
        state,
    )

    assert state.state == CreateTtnState.summary
    assert state._data["sender_profile_id"] == str(profile_id)
    assert state._data["sender_profile_name"] == "ФОП B"
    assert state._data["cart"]["SKU1"]["qty"] == 1
    assert shown["called"] is True


# ----------------------------------------------------------------- кошик (чистые)


async def test_pick_opens_stepper(monkeypatch):
    # `picker_skus` — идентичность отрисованной страницы; её пишет рендер пикера.
    _patch_inventory(monkeypatch, _page([_item("SKU1", "Кава", 24)]))
    state = FakeState(cart_offset=0, cart={}, picker_skus=["SKU1"])
    cb = FakeCallback("cab:ttn:pick:0")
    await h.cb_pick(cb, _ctx(_CLIENT), None, state)
    assert state._data["pending"]["sku"] == "SKU1"
    assert state._data["pending"]["qty"] == 1
    assert cb.message.edits  # степпер отрисован


async def test_pick_zero_available_blocked(monkeypatch):
    _patch_inventory(monkeypatch, _page([_item("SKU0", "Немає", 0)]))
    state = FakeState(cart_offset=0, cart={}, picker_skus=["SKU0"])
    cb = FakeCallback("cab:ttn:pick:0")
    await h.cb_pick(cb, _ctx(_CLIENT), None, state)
    assert "pending" not in state._data
    assert cb.acks[-1]["show_alert"] is True


def _catalog() -> list[InventoryItem]:
    """Склад «как на видео»: нулевая «Дріп кава» впереди, Lavazza в наличии — дальше."""
    return [
        _item("DRIP-BRA", "Дріп кава Бразилія 7 шт", 0, category="Дріп кава"),
        _item("DRIP-ETH", "Дріп кава Ефіопія 7 шт", 0, category="Дріп кава"),
        _item("LAV-BEAN", "Кава в зернах Lavazza", 7, category="Кава Lavazza"),
        _item("LAV-GRND", "Кава мелена Lavazza", 9, category="Кава Lavazza"),
    ]


async def test_pick_resolves_item_from_rendered_page(monkeypatch):
    """Регрессия: после смены категории тап брал товар из НЕфильтрованного списка.

    На видео владельца это выглядит так: активна «Кава Lavazza», все видимые позиции
    в наличии (7/9 шт), а тап отвечает «Дріп кава ... немає на залишку» — про товар,
    которого на экране нет. Индекс кнопки резолвился против другого снапшота.
    """
    _patch_inventory_from_items(monkeypatch, _catalog())
    state = FakeState(cart={}, ttn_category="Кава Lavazza")
    await h._show_picker(FakeMessage(), None, _CLIENT, state, offset=0, edit=False)

    cb = FakeCallback("cab:ttn:pick:0")
    await h.cb_pick(cb, _ctx(_CLIENT), None, state)

    assert state._data.get("pending", {}).get("sku") == "LAV-BEAN"
    assert [ack for ack in cb.acks if ack["show_alert"]] == []


async def test_pick_category_then_pick(monkeypatch):
    """Сквозной путь как у пользователя: чип категории → тап по товару."""
    _patch_inventory_from_items(monkeypatch, _catalog())
    state = FakeState(cart={})
    await h._show_picker(FakeMessage(), None, _CLIENT, state, offset=0, edit=False)
    # «Кава Lavazza» — индекс 1 в отсортированном списке категорий страницы.
    idx = state._data["ttn_categories"].index("Кава Lavazza")

    await h.cb_pick_category(FakeCallback(f"cab:ttn:pcat:{idx}"), _ctx(_CLIENT), None, state)
    cb = FakeCallback("cab:ttn:pick:1")
    await h.cb_pick(cb, _ctx(_CLIENT), None, state)

    assert state._data["ttn_category"] == "Кава Lavazza"
    assert state._data["pending"]["sku"] == "LAV-GRND"


async def test_show_picker_stores_page_skus(monkeypatch):
    _patch_inventory_from_items(monkeypatch, _catalog())
    state = FakeState(cart={}, ttn_category="Кава Lavazza")

    await h._show_picker(FakeMessage(), None, _CLIENT, state, offset=0, edit=False)

    assert state._data["picker_skus"] == ["LAV-BEAN", "LAV-GRND"]


async def test_receive_item_search_stores_page_skus(monkeypatch):
    """Второй рендер пикера обязан вести ту же идентичность, что и `_show_picker`."""
    _patch_inventory_from_items(monkeypatch, _catalog())
    state = FakeState(cart={})

    await h.receive_item_search(FakeMessage(text="мелена"), FakeBot(), state, _ctx(_CLIENT), None)

    assert state._data["picker_skus"] == ["LAV-GRND"]


async def test_pick_item_gone_from_stock(monkeypatch):
    """Позиция исчезла со склада между рендером и тапом → явный алерт, не молчание."""
    _patch_inventory_from_items(monkeypatch, _catalog())
    state = FakeState(cart={}, picker_skus=["GONE-SKU"], cart_offset=0)
    cb = FakeCallback("cab:ttn:pick:0")

    await h.cb_pick(cb, _ctx(_CLIENT), None, state)

    assert "pending" not in state._data
    assert cb.acks[-1]["show_alert"] is True
    assert cb.message.edits  # пикер перерисован, кнопки снова совпадают со складом


async def test_pick_index_out_of_range(monkeypatch):
    _patch_inventory_from_items(monkeypatch, _catalog())
    state = FakeState(cart={}, picker_skus=["LAV-BEAN"])
    cb = FakeCallback("cab:ttn:pick:5")

    await h.cb_pick(cb, _ctx(_CLIENT), None, state)

    assert "pending" not in state._data
    assert cb.acks[-1]["show_alert"] is True


async def test_cart_edit_resolves_sku_beyond_first_page(monkeypatch):
    """Регрессия: `cb_cart_edit` искал остаток через `query=sku` с лимитом страницы.

    Подстрочный матч по общему префиксу выдаёт больше позиций, чем помещается на
    странице, и нужная в неё не попадала → `available` тихо падал на количество из
    корзины, и степпер показывал устаревший максимум.
    """
    # «SKU-1» — подстрока каждого из SKU-10…SKU-19, поэтому подстрочный поиск
    # выдаёт 11 позиций, а нужная в первую страницу (6) не попадает.
    items = [_item(f"SKU-1{i}", f"Кава 1{i}", 1) for i in range(10)]
    items.append(_item("SKU-1", "Кава одна", 12))
    _patch_inventory_from_items(monkeypatch, items)
    state = FakeState(cart={"SKU-1": {"qty": 2, "name": "Кава одна", "price": "100"}})
    cb = FakeCallback("cab:ttn:cedit:0")

    await h.cb_cart_edit(cb, _ctx(_CLIENT), None, state)

    assert state._data["pending"]["available"] == 12


async def test_cart_edit_replaces_quantity(monkeypatch):
    """«✏️» правит позицию, а не добирает.

    Регрессия: `cb_qty_ok` безусловно складывал `prev + pending`, и подтверждение
    правки без единого изменения удваивало позицию (2 шт → 4). Вместе с ней
    завышались оголошена вартість и накладений платіж — обе из `_cart_total`.
    """
    _patch_inventory_from_items(monkeypatch, [_item("SKU1", "Кава", 10)])
    state = FakeState(cart_offset=0, cart={"SKU1": {"qty": 2, "name": "Кава", "price": "100"}})

    await h.cb_cart_edit(FakeCallback("cab:ttn:cedit:0"), _ctx(_CLIENT), None, state)
    assert state._data["pending"]["qty"] == 2  # степпер открыт на текущем количестве
    ok = FakeCallback("cab:ttn:qok")
    await h.cb_qty_ok(ok, _ctx(_CLIENT), None, state)

    assert state._data["cart"]["SKU1"]["qty"] == 2
    assert "Оновлено" in ok.acks[-1]["text"]


async def test_cart_edit_lowers_quantity(monkeypatch):
    """Правка вниз обязана уменьшать: раньше «−1» от 5 давала 5+4=9."""
    _patch_inventory_from_items(monkeypatch, [_item("SKU1", "Кава", 10)])
    state = FakeState(cart_offset=0, cart={"SKU1": {"qty": 5, "name": "Кава", "price": "100"}})

    await h.cb_cart_edit(FakeCallback("cab:ttn:cedit:0"), _ctx(_CLIENT), None, state)
    await h.cb_qty_delta(FakeCallback("cab:ttn:qd:-1"), state)
    await h.cb_qty_ok(FakeCallback("cab:ttn:qok"), _ctx(_CLIENT), None, state)

    assert state._data["cart"]["SKU1"]["qty"] == 4


async def test_cart_edit_clamps_to_dropped_stock(monkeypatch):
    """Остаток упал ниже лежащего в кошику — степпер показывает реальный максимум."""
    _patch_inventory_from_items(monkeypatch, [_item("SKU1", "Кава", 3)])
    state = FakeState(cart={"SKU1": {"qty": 8, "name": "Кава", "price": "100"}})

    await h.cb_cart_edit(FakeCallback("cab:ttn:cedit:0"), _ctx(_CLIENT), None, state)

    assert state._data["pending"]["available"] == 3
    assert state._data["pending"]["qty"] == 3


@pytest.mark.parametrize(
    "stock",
    [
        pytest.param([_item("SKU1", "Кава", 0)], id="остаток обнулился"),
        pytest.param([], id="позиция исчезла со склада"),
    ],
)
async def test_cart_edit_unavailable_alerts(monkeypatch, stock):
    """Править нечего: степпер с максимумом 0 нерабочий, а исчезнувшую позицию
    правка «подтверждала» бы, отложив ошибку до создания отправления."""
    _patch_inventory_from_items(monkeypatch, stock)
    state = FakeState(cart={"SKU1": {"qty": 2, "name": "Кава", "price": "100"}})
    cb = FakeCallback("cab:ttn:cedit:0")

    await h.cb_cart_edit(cb, _ctx(_CLIENT), None, state)

    assert "pending" not in state._data
    assert cb.acks[-1]["show_alert"] is True
    assert cb.message.edits  # кошик перерисован, позиция на месте — её можно убрать


async def test_cart_edit_returns_to_cart_after_save(monkeypatch):
    """Правку затевают из кошика — туда и возвращаемся, результат виден сразу."""
    _patch_inventory_from_items(monkeypatch, [_item("SKU1", "Кава", 10)])
    state = FakeState(cart_offset=0, cart={"SKU1": {"qty": 2, "name": "Кава", "price": "100"}})

    await h.cb_cart_edit(FakeCallback("cab:ttn:cedit:0"), _ctx(_CLIENT), None, state)
    ok = FakeCallback("cab:ttn:qok")
    await h.cb_qty_ok(ok, _ctx(_CLIENT), None, state)

    assert "Кошик" in ok.message.edits[-1]["text"]


def test_stepper_kb_edit_labels():
    """В режиме правки кнопка — «Зберегти», а «Назад» ведёт в кошик, не в пикер."""
    from app.bot.keyboards.ttn import build_stepper_kb

    def _back(kb) -> str:
        return next(
            b.callback_data for row in kb.inline_keyboard for b in row if b.text == "◀ Назад"
        )

    def _labels(kb) -> list[str]:
        return [b.text for row in kb.inline_keyboard for b in row]

    add = build_stepper_kb(qty=2, available=5)
    edit = build_stepper_kb(qty=2, available=5, edit=True)
    assert "✓ Додати" in _labels(add)
    assert "✓ Зберегти" in _labels(edit)
    assert _back(add) == "cab:ttn:page:0"
    assert _back(edit) == "cab:ttn:cart"


async def test_qty_delta_clamps_to_available():
    state = FakeState(pending={"sku": "S", "name": "X", "available": 3, "price": "100", "qty": 1})
    cb = FakeCallback("cab:ttn:qd:10")
    await h.cb_qty_delta(cb, state)
    assert state._data["pending"]["qty"] == 3  # +10, но остаток 3


async def test_qty_delta_floor_one():
    state = FakeState(pending={"sku": "S", "name": "X", "available": 5, "price": "100", "qty": 1})
    cb = FakeCallback("cab:ttn:qd:-1")
    await h.cb_qty_delta(cb, state)
    assert state._data["pending"]["qty"] == 1  # не опускается ниже 1


async def test_qty_max():
    state = FakeState(pending={"sku": "S", "name": "X", "available": 7, "price": "100", "qty": 2})
    cb = FakeCallback("cab:ttn:qmax")
    await h.cb_qty_max(cb, state)
    assert state._data["pending"]["qty"] == 7


async def test_qty_ok_adds_to_cart(monkeypatch):
    _patch_inventory(monkeypatch, _page([_item("SKU1", "Кава", 10)]))
    state = FakeState(
        cart_offset=0,
        cart={},
        pending={"sku": "SKU1", "name": "Кава", "available": 10, "price": "100", "qty": 4},
    )
    cb = FakeCallback("cab:ttn:qok")
    await h.cb_qty_ok(cb, _ctx(_CLIENT), None, state)
    assert state._data["cart"]["SKU1"]["qty"] == 4
    assert state._data["pending"] is None


async def test_qty_ok_aggregates_capped(monkeypatch):
    _patch_inventory(monkeypatch, _page([_item("SKU1", "Кава", 10)]))
    state = FakeState(
        cart_offset=0,
        cart={"SKU1": {"qty": 8, "name": "Кава", "price": "100"}},
        pending={"sku": "SKU1", "name": "Кава", "available": 10, "price": "100", "qty": 6},
    )
    cb = FakeCallback("cab:ttn:qok")
    await h.cb_qty_ok(cb, _ctx(_CLIENT), None, state)
    assert state._data["cart"]["SKU1"]["qty"] == 10  # 8+6=14 → capped на остаток 10


async def test_receive_qty_validates_range():
    state = FakeState(pending={"sku": "S", "name": "X", "available": 5, "price": "100", "qty": 1})
    await state.set_state(CreateTtnState.entering_qty)
    msg = FakeMessage(text="99")
    await h.receive_qty(msg, object(), state)
    assert "1–5" in msg.answers[-1]["text"]  # отклонено
    assert state.state == CreateTtnState.entering_qty


async def test_receive_qty_accepts():
    state = FakeState(pending={"sku": "S", "name": "X", "available": 5, "price": "100", "qty": 1})
    await state.set_state(CreateTtnState.entering_qty)
    msg = FakeMessage(text="3")
    await h.receive_qty(msg, object(), state)
    assert state._data["pending"]["qty"] == 3
    assert state.state == CreateTtnState.picking_items


async def test_cart_remove(monkeypatch):
    state = FakeState(
        cart={
            "A": {"qty": 1, "name": "A", "price": "10"},
            "B": {"qty": 2, "name": "B", "price": "20"},
        }
    )
    cb = FakeCallback("cab:ttn:crm:0")
    await h.cb_cart_remove(cb, state)
    assert list(state._data["cart"].keys()) == ["B"]


async def test_search_clear_empties_cart(db_session: AsyncSession, monkeypatch):
    """«🧹 Скинути» очищает и фильтры, и корзину, перерисовывая пикер."""
    client = await _active_client(db_session, 960)
    _patch_inventory(monkeypatch, _page([_item("A", "Кава", 10)]))
    state = FakeState(
        cart={"A": {"qty": 3, "name": "Кава", "price": "100"}},
        ttn_query="кава",
        ttn_category="напої",
        pending={"sku": "B"},
    )
    cb = FakeCallback("cab:ttn:searchclear")
    await h.cb_search_clear(cb, _ctx(client), db_session, state)
    assert state._data["cart"] == {}
    assert state._data["ttn_query"] is None
    assert state._data["ttn_category"] is None
    assert state._data["pending"] is None
    assert state.state == CreateTtnState.picking_items
    assert cb.message.edits  # пикер перерисован


async def test_item_search_keeps_reset_button(monkeypatch):
    """После текстового поиска «🧹 Скинути» остаётся на экране (has_reset передан).

    Регрессия: второй вызов build_cart_picker_kb в receive_item_search шёл без
    has_reset → кнопка сброса пропадала после поиска.
    """
    _patch_inventory(monkeypatch, _page([_item("A", "Кава", 10)]))
    state = FakeState(
        cart={"A": {"qty": 1, "name": "Кава", "price": "100"}},
        _screen_chat_id=1,
        _screen_message_id=10,
    )
    await state.set_state(CreateTtnState.entering_item_search)
    bot = FakeBot()
    msg = FakeMessage(text="кава")
    await h.receive_item_search(msg, bot, state, _ctx(_CLIENT), None)
    kb = msg.answers[-1]["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "🧹 Скинути" in labels
    assert bot.edits == []  # результат поиска всегда отправлен последним сообщением


def test_cart_picker_reset_button_guarded():
    """«🧹 Скинути» скрыта без корзины/фильтра и показана, когда есть что сбросить."""
    from app.bot.keyboards.ttn import build_cart_picker_kb

    page = _page([_item("A", "Кава", 10)])
    hidden_kb = build_cart_picker_kb(page, cart_count=0)
    hidden = [b.text for row in hidden_kb.inline_keyboard for b in row]
    assert "🧹 Скинути" not in hidden
    shown = [
        b.text
        for row in build_cart_picker_kb(page, cart_count=1, has_reset=True).inline_keyboard
        for b in row
    ]
    assert "🧹 Скинути" in shown


# --------------------------------------------------------- параметри посилки


async def test_next_requires_nonempty_cart():
    state = FakeState(cart={})
    cb = FakeCallback("cab:ttn:next")
    await h.cb_next_to_parcel(cb, state)
    assert cb.acks[-1]["show_alert"] is True
    assert state.state is None


async def test_next_to_parcel():
    state = FakeState(cart={"A": {"qty": 1, "name": "A", "price": "10"}}, size_token="s")
    cb = FakeCallback("cab:ttn:next")
    await h.cb_next_to_parcel(cb, state)
    assert state.state == CreateTtnState.picking_parcel
    assert cb.message.edits


async def test_size_select():
    state = FakeState(size_token="s", weight="1.0")
    cb = FakeCallback("cab:ttn:sz:l")
    await h.cb_size(cb, state)
    assert state._data["size_token"] == "l"


async def test_receive_weight_invalid():
    state = FakeState(size_token="s")
    await state.set_state(CreateTtnState.entering_weight)
    msg = FakeMessage(text="abc")
    await h.receive_weight(msg, object(), state)
    assert "Невірна вага" in msg.answers[-1]["text"]
    assert "weight" not in state._data
    assert state.state == CreateTtnState.entering_weight


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "0", "-1", "1001"])
async def test_receive_weight_rejects_non_finite_and_out_of_range(raw):
    """`Decimal("nan")` проходит обе границы (сравнения с NaN всегда False).

    Без явной проверки на конечность такой вес доехал бы до маппера НП и уронил
    бы `quantize` на `InvalidOperation` уже при создании ТТН.
    """
    state = FakeState(size_token="s")
    await state.set_state(CreateTtnState.entering_weight)
    msg = FakeMessage(text=raw)
    await h.receive_weight(msg, object(), state)
    assert "Невірна вага" in msg.answers[-1]["text"]
    assert "weight" not in state._data


async def test_receive_weight_accepts_comma():
    state = FakeState(size_token="s")
    await state.set_state(CreateTtnState.entering_weight)
    msg = FakeMessage(text="2,5")
    await h.receive_weight(msg, object(), state)
    assert state._data["weight"] == "2.5"
    assert state.state == CreateTtnState.picking_parcel


async def test_to_recipient_requires_weight():
    state = FakeState(size_token="s")  # без weight
    cb = FakeCallback("cab:ttn:torcpt")
    await h.cb_to_recipient(cb, state)
    assert cb.acks[-1]["show_alert"] is True
    assert state.state is None


async def test_to_recipient_ok_and_kind_stored():
    state = FakeState(size_token="s", weight="1.0")
    cb = FakeCallback("cab:ttn:torcpt")
    await h.cb_to_recipient(cb, state)
    assert state.state == CreateTtnState.picking_recipient_kind

    cb2 = FakeCallback("cab:ttn:rk:o")
    await h.cb_recipient_kind(cb2, state)
    assert state._data["recipient_kind"] == "organization"


async def test_cancel_clears_state():
    state = FakeState(cart={"A": {"qty": 1, "name": "A", "price": "10"}})
    cb = FakeCallback("cab:ttn:cancel")
    await h.cb_cancel(cb, state)
    assert state.cleared is True
    assert "скасовано" in cb.message.edits[-1]["text"]


# ----------------------------------------------------------- HTML-экранирование (review fix)


def test_stepper_text_escapes_html():
    item = _item("SKU", "Кава & <тег>", 5)
    out = ttn_texts.stepper_text(item, 2)
    assert "&amp;" in out
    assert "&lt;тег&gt;" in out


def test_cart_review_text_escapes_html():
    out = ttn_texts.cart_review_text([("A&B<x>", 1, Decimal("10"))])
    assert "&amp;" in out
    assert "&lt;x&gt;" in out


# ----------------------------------------------------------- параметри посилки (коробки)


async def test_size_select_sets_default_weight():
    # Выбор коробки подставляет вес (верхняя граница тира) → активирует «Далі».
    state = FakeState(size_token="s")  # без weight
    cb = FakeCallback("cab:ttn:sz:l")
    await h.cb_size(cb, state)
    assert state._data["size_token"] == "l"
    assert state._data["weight"] == "30"  # Велика (до 30 кг)


async def test_show_parcel_defaults_weight_so_dali_available():
    # Вход на экран без веса → дефолтная коробка+вес (frictionless «Далі» с порога).
    state = FakeState()
    msg = FakeMessage()
    await h._show_parcel(msg, state, edit=False)
    assert state._data["size_token"] == "s"
    assert state._data["weight"] == "2"  # Мала (до 2 кг)


# ----------------------------------------------------------- COD-гард (анти cod_amount=None)


async def test_set_payment_cod_without_cart_price_offers_custom_amount():
    # Без цен в корзине можно указать свою сумму, но нельзя ошибочно выбрать сумму корзины.
    state = FakeState(cart={"A": {"qty": 1, "name": "A", "price": None}}, payment_method="prepay")
    cb = FakeCallback("cab:ttn:setpm:cod")
    await h.cb_set_payment(cb, None, None, None, state)
    assert state._data.get("payment_method") == "prepay"
    assert "cod_amount" not in state._data
    labels = _button_labels(cb.message.edits[-1]["reply_markup"])
    assert "✏️ Ввести власну суму" in labels
    assert not any("Сума з кошика" in label for label in labels)


# ----------------------------------------------------------- фильтр-категория в пикере


async def test_pick_category_sets_filter(monkeypatch):
    _patch_inventory(monkeypatch, _page([_item("SKU1", "Кава", 5)]))
    state = FakeState(ttn_categories=["Кава", "Чай"], cart={})
    cb = FakeCallback("cab:ttn:pcat:1")
    await h.cb_pick_category(cb, _ctx(_CLIENT), None, state)
    assert state._data["ttn_category"] == "Чай"


async def test_pick_category_all_clears_filter(monkeypatch):
    _patch_inventory(monkeypatch, _page([_item("SKU1", "Кава", 5)]))
    state = FakeState(ttn_categories=["Кава"], ttn_category="Кава", cart={})
    cb = FakeCallback("cab:ttn:pcat:all")
    await h.cb_pick_category(cb, _ctx(_CLIENT), None, state)
    assert state._data["ttn_category"] is None


# ===================== PR 9b: отримувач + адреса =====================


def test_normalize_phone():
    assert h._normalize_phone("0671234567") == "380671234567"
    assert h._normalize_phone("380671234567") == "380671234567"
    assert h._normalize_phone("+38 (067) 123-45-67") == "380671234567"
    assert h._normalize_phone("12345") is None
    assert h._normalize_phone("0971234567890") is None


def test_valid_edrpou():
    assert h._valid_edrpou("12345678") is True  # 8
    assert h._valid_edrpou("1234567890") is True  # 10 (ІПН ФОП)
    assert h._valid_edrpou("1234567") is False  # 7
    assert h._valid_edrpou("123456789") is False  # 9
    assert h._valid_edrpou("abcdefgh") is False


def test_recipient_person_name_rejects_digits_and_accepts_words():
    assert ttn_texts.recipient_person_name_valid("Петренко Іван") is True
    assert ttn_texts.recipient_person_name_valid("Петренко Іванович") is True
    assert ttn_texts.recipient_person_name_valid("5514") is False
    assert ttn_texts.recipient_person_name_valid("Петренко 5514") is False


async def test_recipient_kind_forwards_to_name():
    state = FakeState(weight="1.0")
    cb = FakeCallback("cab:ttn:rk:o")
    await h.cb_recipient_kind(cb, state)
    assert state._data["recipient_kind"] == "organization"
    assert state.state == CreateTtnState.entering_recipient_name
    assert "організації" in cb.message.answers[-1]["text"]


async def test_receive_name_org_then_edrpou():
    state = FakeState(recipient_kind="organization")
    await state.set_state(CreateTtnState.entering_recipient_name)
    msg = FakeMessage(text="ТОВ Ромашка")
    await h.receive_recipient_name(msg, state)
    assert state._data["recipient_name"] == "ТОВ Ромашка"
    assert state.state == CreateTtnState.entering_recipient_edrpou


async def test_receive_name_person_skips_edrpou():
    state = FakeState(recipient_kind="person")
    await state.set_state(CreateTtnState.entering_recipient_name)
    msg = FakeMessage(text="Іваненко Іван")
    await h.receive_recipient_name(msg, state)
    assert state.state == CreateTtnState.entering_recipient_phone  # без ЄДРПОУ


async def test_receive_name_empty_rejected():
    state = FakeState(recipient_kind="person")
    await state.set_state(CreateTtnState.entering_recipient_name)
    msg = FakeMessage(text="   ")
    await h.receive_recipient_name(msg, state)
    assert "recipient_name" not in state._data
    assert state.state == CreateTtnState.entering_recipient_name


async def test_receive_name_with_digits_rejected():
    state = FakeState(recipient_kind="person")
    await state.set_state(CreateTtnState.entering_recipient_name)
    await h.receive_recipient_name(FakeMessage(text="5514"), state)
    assert "recipient_name" not in state._data
    assert state.state == CreateTtnState.entering_recipient_name


async def test_receive_edrpou_invalid_then_valid():
    state = FakeState()
    await state.set_state(CreateTtnState.entering_recipient_edrpou)
    bad = FakeMessage(text="123")
    await h.receive_recipient_edrpou(bad, state)
    assert "recipient_edrpou" not in state._data
    good = FakeMessage(text="12345678")
    await h.receive_recipient_edrpou(good, state)
    assert state._data["recipient_edrpou"] == "12345678"
    assert state.state == CreateTtnState.entering_recipient_phone


async def test_receive_phone_normalizes_and_advances():
    state = FakeState()
    await state.set_state(CreateTtnState.entering_recipient_phone)
    msg = FakeMessage(text="067 123 45 67")
    await h.receive_recipient_phone(msg, state)
    assert state._data["recipient_phone"] == "380671234567"
    assert state.state == CreateTtnState.entering_city_query


def _patch_cities(monkeypatch, cities, *, seen=None):
    async def fake(
        session, *, client, query, np_client, cache, sender_profile_id=None, account_id=None
    ):
        if seen is not None:
            seen["account_id"] = account_id
        return cities

    monkeypatch.setattr(h.address, "search_cities", fake)


def _patch_warehouses(monkeypatch, whs, *, seen=None):
    async def fake(
        session,
        *,
        client,
        city_ref,
        np_client,
        cache,
        query=None,
        sender_profile_id=None,
        account_id=None,
    ):
        if seen is not None:
            seen["account_id"] = account_id
        return whs

    monkeypatch.setattr(h.address, "search_warehouses", fake)


async def test_city_query_shows_results(monkeypatch):
    _patch_cities(monkeypatch, [City(ref="c1", name="Київ", area="Київська")])
    state = FakeState()
    await state.set_state(CreateTtnState.entering_city_query)
    msg = FakeMessage(text="Ки")
    bot = FakeBot()
    await h.receive_city_query(msg, bot, state, _ctx(_CLIENT), None, object(), object())
    assert state._data["cities"][0]["ref"] == "c1"
    assert msg.answers[-1]["reply_markup"] is not None
    assert bot.actions and bot.actions[-1]["action"] == "typing"  # индикатор загрузки


async def test_city_query_passes_account_id_to_address(monkeypatch):
    """Проводка: хендлер отдаёт `account_id` в поиск городов.

    Сервисный фикс без этого бесполезен — `address` ушёл бы в legacy-ветку по
    `client_id`, и работник снова получил бы «ФОП не знайдено». Тест ловит именно
    обрыв провода между хендлером и сервисом.
    """
    seen: dict = {}
    _patch_cities(monkeypatch, [City(ref="c1", name="Київ", area="Київська")], seen=seen)
    state = FakeState()
    await state.set_state(CreateTtnState.entering_city_query)

    await h.receive_city_query(
        FakeMessage(text="Ки"),
        FakeBot(),
        state,
        _ctx_with_account(_CLIENT, "acc-1"),
        None,
        object(),
        object(),
    )

    assert seen["account_id"] == "acc-1"


async def test_warehouse_pick_passes_account_id_to_address(monkeypatch):
    seen: dict = {}
    _patch_cities(monkeypatch, [City(ref="c1", name="Київ", area="Київська")])
    _patch_warehouses(
        monkeypatch, [Warehouse(ref="w1", number="5", description="Хрещатик")], seen=seen
    )
    state = FakeState()
    await state.set_state(CreateTtnState.entering_city_query)

    await h.receive_city_query(
        FakeMessage(text="Київ"),  # точное совпадение → сразу грузит відділення
        FakeBot(),
        state,
        _ctx_with_account(_CLIENT, "acc-2"),
        None,
        object(),
        object(),
    )

    assert seen["account_id"] == "acc-2"


async def test_city_query_exact_match_opens_warehouses(monkeypatch):
    """Точное название города не требует дополнительного выбора кнопкой."""
    _patch_cities(monkeypatch, [City(ref="c1", name="Київ", area="Київська")])
    _patch_warehouses(monkeypatch, [Warehouse(ref="w1", number="5", description="Хрещатик")])
    state = FakeState()
    await state.set_state(CreateTtnState.entering_city_query)
    msg = FakeMessage(text="Київ")

    await h.receive_city_query(msg, FakeBot(), state, _ctx(_CLIENT), None, object(), object())

    assert state._data["recipient_city_ref"] == "c1"
    assert state._data["warehouses"][0]["ref"] == "w1"
    assert state.state == CreateTtnState.entering_warehouse_query
    assert msg.answers[-1]["reply_markup"] is not None


async def test_city_query_empty_stays_on_city_step():
    state = FakeState()
    await state.set_state(CreateTtnState.entering_city_query)
    msg = FakeMessage(text="   ")

    await h.receive_city_query(msg, FakeBot(), state, _ctx(_CLIENT), None, object(), object())

    assert state.state == CreateTtnState.entering_city_query
    assert "Введіть назву міста" in msg.answers[-1]["text"]


async def test_city_query_not_found(monkeypatch):
    _patch_cities(monkeypatch, [])
    state = FakeState()
    await state.set_state(CreateTtnState.entering_city_query)
    msg = FakeMessage(text="Хххх")
    await h.receive_city_query(msg, FakeBot(), state, _ctx(_CLIENT), None, object(), object())
    assert "cities" not in state._data
    assert "Нічого не знайшли" in msg.answers[-1]["text"]


async def test_back_from_city_returns_to_recipient_phone():
    state = FakeState(
        recipient_city_ref="c1",
        recipient_city_name="Київ",
        recipient_warehouse_ref="w1",
        recipient_warehouse_name="№5: Хрещатик",
        warehouses=[{"ref": "w1", "number": "5", "description": "Хрещатик"}],
    )
    cb = FakeCallback("cab:ttn:back:recipient_phone")

    await h.cb_back(cb, _ctx(_CLIENT), None, state)

    assert state.state == CreateTtnState.entering_recipient_phone
    assert state._data["recipient_city_ref"] is None
    assert state._data["recipient_warehouse_ref"] is None
    assert "Введіть телефон" in cb.message.edits[-1]["text"]


async def test_city_pick_loads_warehouses(monkeypatch):
    _patch_warehouses(monkeypatch, [Warehouse(ref="w1", number="5", description="вул. Хрещатик")])
    state = FakeState(cities=[{"ref": "c1", "name": "Київ", "area": "Київська"}])
    cb = FakeCallback("cab:ttn:city:0")
    await h.cb_city(cb, _ctx(_CLIENT), None, object(), object(), state)
    assert state._data["recipient_city_ref"] == "c1"
    assert state._data["warehouses"][0]["ref"] == "w1"
    assert state.state == CreateTtnState.entering_warehouse_query


async def test_city_pick_no_warehouses_returns_to_city(monkeypatch):
    _patch_warehouses(monkeypatch, [])
    state = FakeState(cities=[{"ref": "c1", "name": "Село", "area": None}])
    cb = FakeCallback("cab:ttn:city:0")
    await h.cb_city(cb, _ctx(_CLIENT), None, object(), object(), state)
    assert state.state == CreateTtnState.entering_city_query
    assert "не знайдено" in cb.message.edits[-1]["text"]


async def test_back_from_warehouse_search_returns_to_warehouse_list():
    state = FakeState(
        recipient_city_name="Київ",
        warehouses=[{"ref": "w1", "number": "5", "description": "Хрещатик"}],
    )
    cb = FakeCallback("cab:ttn:back:warehouse")

    await h.cb_back(cb, _ctx(_CLIENT), None, state)

    assert state.state == CreateTtnState.entering_warehouse_query
    assert "Відділення у місті Київ" in cb.message.edits[-1]["text"]


def _patch_pricing(monkeypatch, *, quote=None, raise_exc=None, counter=None, seen=None):
    async def fake(
        session,
        *,
        client,
        sender_profile_id,
        city_recipient_ref,
        weight,
        cost,
        np_client,
        cod_amount=None,
        dimensions_cm=None,
        account_id=None,
        settings=None,
    ):
        if counter is not None:
            counter["n"] = counter.get("n", 0) + 1
        if seen is not None:
            seen["account_id"] = account_id
            seen["dimensions_cm"] = dimensions_cm
            seen["weight"] = weight
        if raise_exc is not None:
            raise raise_exc
        return quote

    monkeypatch.setattr(h.pricing, "quote_ttn", fake)


def _quote():
    return PriceQuote(
        cost=Decimal("70"), cost_redelivery=Decimal("20"), estimated_delivery_date="2026-06-25"
    )


def _card_state(**over):
    base = {
        "sender_profile_id": str(uuid4()),
        "cart": {"SKU1": {"qty": 2, "name": "Кава", "price": "150"}},
        "recipient_kind": "person",
        "recipient_name": "Іваненко Іван",
        "recipient_phone": "380671234567",
        "recipient_city_ref": "c1",
        "recipient_city_name": "Київ",
        "recipient_warehouse_ref": "w1",
        "recipient_warehouse_name": "№5: Хрещатик",
        "warehouses": [{"ref": "w1", "number": "5", "description": "Хрещатик"}],
        "weight": "2.5",
        "size_token": "s",
    }
    base.update(over)
    return FakeState(**base)


async def test_warehouse_pick_renders_card(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(
        warehouses=[
            {"ref": "w1", "number": "5", "description": "Хрещатик"},
            {"ref": "w2", "number": "7", "description": "Сагайдачного"},
        ]
    )
    cb = FakeCallback("cab:ttn:wh:1")
    await h.cb_wh(cb, _ctx(_CLIENT), None, object(), state)
    assert state._data["recipient_warehouse_ref"] == "w2"
    assert state.state == CreateTtnState.summary
    card = cb.message.edits[-1]["text"]
    assert "Перевірте ТТН" in card
    assert "70" in card  # цена показана


async def test_card_computes_defaults(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state()
    cb = FakeCallback("cab:ttn:wh:0")
    await h.cb_wh(cb, _ctx(_CLIENT), None, object(), state)
    # Оголошена вартість = сумма корзины (2 × 150), а не молчаливый ноль.
    assert state._data["insured_amount"] == "300"
    assert state._data["insured_amount_source"] == "cart"
    assert state._data["description"] == "Кава"
    assert state._data["payment_method"] == "prepay"
    assert state._data["payer_type"] == "Recipient"


async def test_card_insured_shown_with_source(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state()
    cb = FakeCallback("cab:ttn:wh:0")
    await h.cb_wh(cb, _ctx(_CLIENT), None, object(), state)
    card = cb.message.edits[-1]["text"]
    assert "Оголошена вартість: 300 ₴ (сума з кошика)" in card


async def test_card_insured_unset_when_cart_has_no_prices(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(cart={"SKU1": {"qty": 2, "name": "Кава", "price": None}})
    cb = FakeCallback("cab:ttn:wh:0")
    await h.cb_wh(cb, _ctx(_CLIENT), None, object(), state)
    assert state._data.get("insured_amount") is None
    assert state._data.get("insured_amount_source") is None
    card = cb.message.edits[-1]["text"]
    assert "не вказана" in card
    # Цена доставки при этом обязана считаться: иначе клиент вместо понятного
    # «вкажіть вартість» увидит «розрахунок недоступний».
    assert "Розрахунок недоступний" not in card


async def test_card_insured_partial_prices_warns(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(
        cart={
            "SKU1": {"qty": 2, "name": "Кава", "price": "150"},
            "SKU2": {"qty": 1, "name": "Чай", "price": None},
        }
    )
    cb = FakeCallback("cab:ttn:wh:0")
    await h.cb_wh(cb, _ctx(_CLIENT), None, object(), state)
    assert state._data["insured_amount"] == "300"  # только позиции с ценой
    assert "Частина товарів без ціни" in cb.message.edits[-1]["text"]


async def test_card_insured_zero_warns_uninsured(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(insured_amount="0.00", insured_amount_source="custom")
    cb = FakeCallback("cab:ttn:wh:0")
    await h.cb_wh(cb, _ctx(_CLIENT), None, object(), state)
    assert state._data["insured_amount"] == "0.00"  # свою сумму корзина не трогает
    assert "відшкодує 0 ₴" in cb.message.edits[-1]["text"]


async def test_card_insured_follows_cart(monkeypatch):
    """Товар, добавленный после первого показа карточки, обязан поднять сумму."""
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state()
    await h.cb_wh(FakeCallback("cab:ttn:wh:0"), _ctx(_CLIENT), None, object(), state)
    assert state._data["insured_amount"] == "300"

    cart = dict(state._data["cart"])
    cart["SKU2"] = {"qty": 1, "name": "Чай", "price": "80"}
    await state.update_data(cart=cart)
    await h.cb_card(FakeCallback("cab:ttn:card"), _ctx(_CLIENT), None, object(), state)
    assert state._data["insured_amount"] == "380"


async def test_cart_edit_does_not_inflate_insured(monkeypatch):
    """Правка позиции без изменений не двигает оголошену вартість.

    Ради этого инварианта всё и затевалось: `cb_qty_ok` удваивал количество, а
    `_ensure_card_defaults` честно пересчитывал сумму из раздутого кошика.
    """
    _patch_pricing(monkeypatch, quote=_quote())
    _patch_inventory_from_items(monkeypatch, [_item("SKU1", "Кава", 10, price="150")])
    state = _card_state()
    await h.cb_wh(FakeCallback("cab:ttn:wh:0"), _ctx(_CLIENT), None, object(), state)
    assert state._data["insured_amount"] == "300"  # 2 × 150

    await h.cb_cart_edit(FakeCallback("cab:ttn:cedit:0"), _ctx(_CLIENT), None, state)
    await h.cb_qty_ok(FakeCallback("cab:ttn:qok"), _ctx(_CLIENT), None, state)
    await h.cb_card(FakeCallback("cab:ttn:card"), _ctx(_CLIENT), None, object(), state)

    assert state._data["insured_amount"] == "300"


async def test_card_insured_custom_survives_cart_change(monkeypatch):
    """Заниженная вручную сумма — осознанный выбор клиента, корзина её не перетирает."""
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(insured_amount="50", insured_amount_source="custom")
    await h.cb_wh(FakeCallback("cab:ttn:wh:0"), _ctx(_CLIENT), None, object(), state)

    cart = dict(state._data["cart"])
    cart["SKU2"] = {"qty": 1, "name": "Чай", "price": "80"}
    await state.update_data(cart=cart)
    await h.cb_card(FakeCallback("cab:ttn:card"), _ctx(_CLIENT), None, object(), state)
    assert state._data["insured_amount"] == "50"
    assert state._data["insured_amount_source"] == "custom"


async def test_card_price_graceful_on_np_error(monkeypatch):
    from app.novaposhta.exceptions import NovaPoshtaValidationError

    _patch_pricing(monkeypatch, raise_exc=NovaPoshtaValidationError("no Cost"))
    state = _card_state()
    cb = FakeCallback("cab:ttn:wh:0")
    await h.cb_wh(cb, _ctx(_CLIENT), None, object(), state)
    assert state._data["price_cache"]["unavailable"] is True
    assert "Розрахунок недоступний" in cb.message.edits[-1]["text"]


async def test_card_price_cached_between_renders(monkeypatch):
    counter: dict = {}
    _patch_pricing(monkeypatch, quote=_quote(), counter=counter)
    state = _card_state()
    cb = FakeCallback("cab:ttn:wh:0")
    await h.cb_wh(cb, _ctx(_CLIENT), None, object(), state)
    await h.cb_wh(cb, _ctx(_CLIENT), None, object(), state)  # те же поля → кэш
    assert counter["n"] == 1


async def test_card_price_cache_is_invalidated_when_sender_changes(monkeypatch):
    counter: dict = {}
    _patch_pricing(monkeypatch, quote=_quote(), counter=counter)
    state = _card_state(insured_amount="0")
    data = state._data

    first = await h._card_price(None, _CLIENT, data, object(), force=False)
    data["price_cache"] = first
    data["sender_profile_id"] = str(uuid4())
    second = await h._card_price(None, _CLIENT, data, object(), force=False)

    assert counter["n"] == 2
    assert second["key"] != first["key"]


async def test_card_price_cache_is_invalidated_when_size_changes(monkeypatch):
    counter: dict = {}
    _patch_pricing(monkeypatch, quote=_quote(), counter=counter)
    state = _card_state(insured_amount="0", size_token="s")
    data = state._data

    first = await h._card_price(None, _CLIENT, data, object(), force=False)
    data["price_cache"] = first
    data["size_token"] = "l"
    second = await h._card_price(None, _CLIENT, data, object(), force=False)

    assert counter["n"] == 2
    assert second["key"] != first["key"]


async def test_card_price_sends_preset_dimensions_to_np(monkeypatch):
    """Габариты пресета обязаны доехать до оценки цены.

    НП тарифицирует по максимуму из фактического и объёмного веса; без габаритов
    оценка «Велика» + 2 кг показала бы цену за 2 кг вместо 12.
    """
    seen: dict = {}
    _patch_pricing(monkeypatch, quote=_quote(), seen=seen)
    state = _card_state(insured_amount="0", size_token="l", weight="2")

    await h._card_price(None, _CLIENT, state._data, object(), force=False)

    assert seen["dimensions_cm"] == ("40", "40", "30")


async def test_card_price_shows_billable_weight_only_when_volumetric_wins(monkeypatch):
    seen: dict = {}
    _patch_pricing(
        monkeypatch,
        quote=PriceQuote(cost=Decimal("70"), billable_weight=Decimal("12")),
        seen=seen,
    )
    state = _card_state(insured_amount="0", size_token="l", weight="2")
    bulky = await h._card_price(None, _CLIENT, state._data, object(), force=False)
    assert bulky["billable_weight"] == "12"

    _patch_pricing(monkeypatch, quote=PriceQuote(cost=Decimal("70"), billable_weight=Decimal("30")))
    heavy = await h._card_price(
        None,
        _CLIENT,
        _card_state(insured_amount="0", size_token="l", weight="30")._data,
        object(),
        force=False,
    )
    assert "billable_weight" not in heavy


async def test_recompute_forces_price(monkeypatch):
    counter: dict = {}
    _patch_pricing(monkeypatch, quote=_quote(), counter=counter)
    state = _card_state(price_cache={"key": "stale", "cost": "1"})
    cb = FakeCallback("cab:ttn:recompute")
    await h.cb_recompute(cb, _ctx(_CLIENT), None, object(), state)
    assert counter["n"] == 1  # форс пересчёта, несмотря на наличие кэша
    assert state._data["price_cache"]["cost"] == "70"


async def test_recompute_stale_state_graceful(monkeypatch):
    # Устаревшая кнопка recompute на сброшенном FSM: не падаем KeyError, НП не дёргаем.
    counter: dict = {}
    _patch_pricing(monkeypatch, quote=_quote(), counter=counter)
    state = FakeState(cart={})  # нет recipient_city_ref / weight
    cb = FakeCallback("cab:ttn:recompute")
    await h.cb_recompute(cb, _ctx(_CLIENT), None, object(), state)
    assert state._data["price_cache"]["unavailable"] is True
    assert counter.get("n", 0) == 0


# ===================== PR 9c-2: точкова правка карточки + COD =====================


async def test_edit_text_field_prompts():
    state = _card_state()
    cb = FakeCallback("cab:ttn:edit:phone")
    await h.cb_edit(cb, state)
    assert state.state == CreateTtnState.editing_field
    assert state._data["edit_field"] == "phone"
    assert cb.message.answers  # prompt отправлен


async def test_edit_edrpou_blocked_for_person():
    state = _card_state(recipient_kind="person")
    cb = FakeCallback("cab:ttn:edit:edrpou")
    await h.cb_edit(cb, state)
    assert state._data.get("edit_field") is None
    assert cb.acks[-1]["show_alert"] is True


async def test_edit_size_shows_picker():
    state = _card_state()
    cb = FakeCallback("cab:ttn:edit:size")
    await h.cb_edit(cb, state)
    assert cb.message.edits  # картка → пикер габаритов


async def test_edit_city_reenters_search():
    state = _card_state()
    cb = FakeCallback("cab:ttn:edit:city")
    await h.cb_edit(cb, state)
    assert state.state == CreateTtnState.entering_city_query


async def test_receive_edit_name_updates_and_renders(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(edit_field="name")
    msg = FakeMessage(text="Петренко Петро")
    await h.receive_edit(msg, object(), state, _ctx(_CLIENT), None, object())
    assert state._data["recipient_name"] == "Петренко Петро"
    assert state.state == CreateTtnState.summary
    assert msg.answers  # карточка перерисована


async def test_receive_edit_phone_invalid_stays(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(edit_field="phone")
    msg = FakeMessage(text="not-a-phone")
    await h.receive_edit(msg, object(), state, _ctx(_CLIENT), None, object())
    assert state._data["recipient_phone"] == "380671234567"  # не изменился


async def test_receive_edit_weight_updates(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(edit_field="weight")
    msg = FakeMessage(text="3,2")
    await h.receive_edit(msg, object(), state, _ctx(_CLIENT), None, object())
    assert state._data["weight"] == "3.2"


async def test_receive_edit_insured_sets_value(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(edit_field="insured")
    msg = FakeMessage(text="500")
    await h.receive_edit(msg, object(), state, _ctx(_CLIENT), None, object())
    assert state._data["insured_amount"] == "500"
    # Источник переключился на custom, иначе следующий рендер вернул бы сумму
    # корзины и правка была бы бессмысленной.
    assert state._data["insured_amount_source"] == "custom"


async def test_receive_edit_insured_lower_than_cart_survives_render(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(edit_field="insured")
    await h.receive_edit(FakeMessage(text="120"), object(), state, _ctx(_CLIENT), None, object())
    await h.cb_card(FakeCallback("cab:ttn:card"), _ctx(_CLIENT), None, object(), state)
    assert state._data["insured_amount"] == "120"


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "1e999", "-1", "abc"])
def test_parse_money_rejects_broken_input(raw):
    """`nan` роняло хендлер (сигнальное сравнение), `inf`/`1e999` уходили в НП."""
    assert h._parse_money(raw) is None


@pytest.mark.parametrize(("raw", "expected"), [("0", "0"), ("1200", "1200"), ("1,5", "1.5")])
def test_parse_money_accepts(raw, expected):
    assert h._parse_money(raw) == expected


async def test_receive_edit_insured_invalid_rejected(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(edit_field="insured", insured_amount="300")
    msg = FakeMessage(text="abc")
    await h.receive_edit(msg, object(), state, _ctx(_CLIENT), None, object())
    assert state._data["insured_amount"] == "300"


async def test_set_size_updates_and_returns(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(size_token="s")
    cb = FakeCallback("cab:ttn:setsz:l")
    await h.cb_set_size(cb, _ctx(_CLIENT), None, object(), state)
    assert state._data["size_token"] == "l"
    assert state.state == CreateTtnState.summary


async def test_set_payer(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state()
    cb = FakeCallback("cab:ttn:setpr:s")
    await h.cb_set_payer(cb, _ctx(_CLIENT), None, object(), state)
    assert state._data["payer_type"] == "Sender"


async def test_set_payment_prepay_clears_cod(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(payment_method="cod", cod_amount="300", cod_amount_source="cart")
    cb = FakeCallback("cab:ttn:setpm:prepay")
    await h.cb_set_payment(cb, _ctx(_CLIENT), None, object(), state)
    assert state._data["payment_method"] == "prepay"
    assert state._data["cod_amount"] is None
    assert state._data["cod_amount_source"] is None


async def test_set_payment_cod_opens_amount_choice():
    state = _card_state()
    cb = FakeCallback("cab:ttn:setpm:cod")

    await h.cb_set_payment(cb, _ctx(_CLIENT), None, object(), state)

    labels = _button_labels(cb.message.edits[-1]["reply_markup"])
    assert "🧺 Сума з кошика: 300 ₴" in labels
    assert "✏️ Ввести власну суму" in labels
    assert state._data.get("payment_method", "prepay") == "prepay"


async def test_set_payment_cod_uses_cart_total(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state()
    cb = FakeCallback("cab:ttn:cod:cart")
    await h.cb_set_cod_from_cart(cb, _ctx(_CLIENT), None, object(), state)
    assert state._data["payment_method"] == "cod"
    assert state._data["cod_amount"] == "300"
    assert state._data["cod_amount_source"] == "cart"
    assert state.state == CreateTtnState.summary


async def test_set_payment_cod_not_linked_to_insured(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    # Своя страховая сумма: COD её не трогает и не подменяет корзиной.
    state = _card_state(insured_amount="450", insured_amount_source="custom")
    cb = FakeCallback("cab:ttn:cod:cart")
    await h.cb_set_cod_from_cart(cb, _ctx(_CLIENT), None, object(), state)
    assert state._data["cod_amount"] == "300"
    assert state._data["payment_method"] == "cod"
    assert state._data["insured_amount"] == "450"


async def test_cod_from_cart_follows_cart(monkeypatch):
    """Регресс: выведенный из корзины COD обязан идти за корзиной.

    До правки сумма считалась один раз и застывала — с получателя взяли бы деньги
    по прежнему составу заказа.
    """
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(payment_method="cod", cod_amount="300", cod_amount_source="cart")
    cart = dict(state._data["cart"])
    cart["SKU2"] = {"qty": 1, "name": "Чай", "price": "80"}
    await state.update_data(cart=cart)
    await h.cb_card(FakeCallback("cab:ttn:card"), _ctx(_CLIENT), None, object(), state)
    assert state._data["cod_amount"] == "380"


async def test_cod_custom_survives_cart_change(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(payment_method="cod", cod_amount="999", cod_amount_source="custom")
    cart = dict(state._data["cart"])
    cart["SKU2"] = {"qty": 1, "name": "Чай", "price": "80"}
    await state.update_data(cart=cart)
    await h.cb_card(FakeCallback("cab:ttn:card"), _ctx(_CLIENT), None, object(), state)
    assert state._data["cod_amount"] == "999"


async def test_set_payment_cod_custom_amount(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state()
    cb = FakeCallback("cab:ttn:cod:custom")

    await h.cb_set_cod_custom(cb, state)

    assert state.state == CreateTtnState.editing_field
    assert state._data["edit_field"] == "cod_amount"
    msg = FakeMessage(text="450,50")
    await h.receive_edit(msg, object(), state, _ctx(_CLIENT), None, object())
    assert state._data["payment_method"] == "cod"
    assert state._data["cod_amount"] == "450.50"
    assert state._data["cod_amount_source"] == "custom"
    assert state.state == CreateTtnState.summary


async def test_set_payment_cod_custom_amount_must_be_positive(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(edit_field="cod_amount")
    await state.set_state(CreateTtnState.editing_field)
    msg = FakeMessage(text="0")

    await h.receive_edit(msg, object(), state, _ctx(_CLIENT), None, object())

    assert state.state == CreateTtnState.editing_field
    assert "більшою за 0" in msg.answers[-1]["text"]


async def test_back_to_card(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state()
    cb = FakeCallback("cab:ttn:card")
    await h.cb_card(cb, _ctx(_CLIENT), None, object(), state)
    assert state.state == CreateTtnState.summary
    assert cb.message.edits


# ===================== PR 9d: відправлення + single-flight + wiring =====================


def _ready_state(**over):
    """Состояние карточки ПОСЛЕ рендера (дефолты проставлены) — для тестов отправки."""
    base = {
        "description": "Кава",
        "insured_amount": "300",
        "payment_method": "prepay",
        "payer_type": "Recipient",
    }
    base.update(over)
    return _card_state(**base)


def _patch_create(monkeypatch, *, ttn="59000123", raise_exc=None, calls=None, seen=None):
    async def fake(session, **kw):
        if calls is not None:
            calls["n"] = calls.get("n", 0) + 1
        if seen is not None:
            seen.update(kw)
        if raise_exc is not None:
            raise raise_exc
        return SimpleNamespace(ttn_number=ttn)

    monkeypatch.setattr(h, "create_shipment", fake)


async def test_submit_success(monkeypatch):
    h._SUBMITTING.discard(_CLIENT.telegram_id)
    _patch_create(monkeypatch, ttn="59000999")
    state = _ready_state()
    cb = FakeCallback("cab:ttn:send")
    await h.cb_submit(cb, _ctx(_CLIENT), None, object(), object(), state)
    assert state.cleared is True
    assert "59000999" in cb.message.edits[-1]["text"]
    assert _CLIENT.telegram_id not in h._SUBMITTING  # флаг снят


async def test_submit_blocked_without_insured(monkeypatch):
    """Корзина без цен → сумму вывести неоткуда, ТТН не уходит с нулевой страховкой."""
    h._SUBMITTING.discard(_CLIENT.telegram_id)
    calls: dict = {}
    _patch_create(monkeypatch, calls=calls)
    _patch_pricing(monkeypatch, quote=_quote())
    state = _ready_state(
        cart={"SKU1": {"qty": 2, "name": "Кава", "price": None}},
        insured_amount=None,
        insured_amount_source=None,
    )
    cb = FakeCallback("cab:ttn:send")
    await h.cb_submit(cb, _ctx(_CLIENT), None, object(), object(), state)
    assert calls.get("n", 0) == 0
    assert cb.acks[-1]["show_alert"] is True
    assert "Вкажіть оголошену вартість" in cb.acks[-1]["text"]
    assert state.cleared is False


async def test_submit_resyncs_insured_to_cart(monkeypatch):
    """Корзину можно менять после отрисовки карточки — ТТН обязана уйти по факту."""
    h._SUBMITTING.discard(_CLIENT.telegram_id)
    seen: dict = {}
    _patch_create(monkeypatch, seen=seen)
    _patch_pricing(monkeypatch, quote=_quote())
    state = _ready_state()
    state._data["cart"]["SKU2"] = {"qty": 1, "name": "Чай", "price": "80"}
    cb = FakeCallback("cab:ttn:send")
    await h.cb_submit(cb, _ctx(_CLIENT), None, object(), object(), state)
    assert seen["insured_amount"] == Decimal("380")


async def test_submit_single_flight(monkeypatch):
    calls: dict = {}
    _patch_create(monkeypatch, calls=calls)
    state = _card_state(recipient_warehouse_ref="w1")
    h._SUBMITTING.add(_CLIENT.telegram_id)  # уже отправляется
    try:
        cb = FakeCallback("cab:ttn:send")
        await h.cb_submit(cb, _ctx(_CLIENT), None, object(), object(), state)
        assert cb.acks[-1]["show_alert"] is True
        assert calls.get("n", 0) == 0  # create_shipment не вызывали
    finally:
        h._SUBMITTING.discard(_CLIENT.telegram_id)


async def test_submit_insufficient_stock_uk(monkeypatch):
    from app.services.exceptions import InsufficientStock

    h._SUBMITTING.discard(_CLIENT.telegram_id)
    _patch_create(monkeypatch, raise_exc=InsufficientStock("SKU1", 5, 2))
    state = _ready_state()
    cb = FakeCallback("cab:ttn:send")
    await h.cb_submit(cb, _ctx(_CLIENT), None, object(), object(), state)
    assert state.cleared is False  # карточка осталась — можно повторить
    assert "лише 2" in cb.message.answers[-1]["text"]  # имя из корзины + остаток
    assert _CLIENT.telegram_id not in h._SUBMITTING


async def test_submit_missing_fields_stale(monkeypatch):
    h._SUBMITTING.discard(_CLIENT.telegram_id)
    _patch_create(monkeypatch)
    state = FakeState(cart={})  # нет warehouse/cart
    cb = FakeCallback("cab:ttn:send")
    await h.cb_submit(cb, _ctx(_CLIENT), None, object(), object(), state)
    assert cb.acks[-1]["show_alert"] is True


async def test_ttn_button_forwards_to_entry(monkeypatch):
    spy: dict = {}

    async def fake_start(message, state, ctx, session):
        spy["called"] = True

    monkeypatch.setattr(h, "start_create_ttn", fake_start)
    await h.open_create_ttn(FakeMessage(), FakeState(), _ctx(_CLIENT), None)
    assert spy.get("called") is True


async def test_submit_success_render_failure_does_not_raise(monkeypatch):
    # Если показ успеха падает (Telegram), исключение НЕ должно всплыть — иначе
    # middleware откатит транзакцию и осиротит уже созданный NP-ТТН.
    from aiogram.exceptions import TelegramAPIError

    h._SUBMITTING.discard(_CLIENT.telegram_id)
    _patch_create(monkeypatch, ttn="59000777")

    class RaisingMessage(FakeMessage):
        async def edit_text(self, *a, **kw):
            raise TelegramAPIError(method=None, message="boom")

        async def answer(self, *a, **kw):
            raise TelegramAPIError(method=None, message="boom")

    state = _ready_state()
    cb = FakeCallback("cab:ttn:send")
    cb.message = RaisingMessage()
    await h.cb_submit(cb, _ctx(_CLIENT), None, object(), object(), state)  # не должно бросить
    assert state.cleared is True
    assert _CLIENT.telegram_id not in h._SUBMITTING


async def test_again_forwards_to_entry(monkeypatch):
    spy: dict = {}

    async def fake_start(message, state, ctx, session, *, edit=False):
        spy["called"] = True
        spy["edit"] = edit

    monkeypatch.setattr(h, "start_create_ttn", fake_start)
    cb = FakeCallback("cab:ttn:again")
    await h.cb_again(cb, _ctx(_CLIENT), None, FakeState())
    assert spy.get("called") is True
    assert spy.get("edit") is True
    assert cb.acks  # callback подтверждён


async def test_warehouse_page():
    whs = [{"ref": f"w{i}", "number": str(i), "description": f"від {i}"} for i in range(20)]
    state = FakeState(recipient_city_name="Київ", warehouses=whs)
    cb = FakeCallback("cab:ttn:whpage:8")
    await h.cb_wh_page(cb, state)
    assert state._data["wh_offset"] == 8
    assert cb.message.edits


# ----------------------------------------------- негативный индекс (review fix, defense-in-depth)


async def test_negative_index_rejected_city():
    state = FakeState(cities=[{"ref": "c1", "name": "Київ", "area": None}])
    cb = FakeCallback("cab:ttn:city:-1")
    await h.cb_city(cb, _ctx(_CLIENT), None, object(), object(), state)
    assert "recipient_city_ref" not in state._data  # -1 не выбрал «последний» город
    assert cb.acks[-1]["show_alert"] is True


async def test_negative_index_rejected_warehouse():
    state = FakeState(warehouses=[{"ref": "w1", "number": "5", "description": "X"}])
    cb = FakeCallback("cab:ttn:wh:-1")
    await h.cb_wh(cb, _ctx(_CLIENT), None, object(), state)
    assert "recipient_warehouse_ref" not in state._data
    assert cb.acks[-1]["show_alert"] is True


async def test_card_description_is_np_safe(monkeypatch):
    """Автоопис из названий товаров уже очищен от того, что НП не принимает.

    Клиент подтверждает карточку, и то же описание печатается на ярлыке. Если
    чистить только на границе с НП, на карточке будет одно, а в документе другое.
    Живой прогон 2026-08-03: SKU «Кава Ferarra 100% Arabica» убил ТТН на сабмите.
    """
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(
        cart={"SKU1": {"qty": 2, "name": "Кава Ferarra 100% Arabica – 250 г", "price": "150"}}
    )
    await h.cb_wh(FakeCallback("cab:ttn:wh:0"), _ctx(_CLIENT), None, object(), state)

    assert state._data["description"] == "Кава Ferarra 100 відс. Arabica - 250 г"


async def test_manual_description_is_cleaned_and_client_told(monkeypatch):
    """Введённое руками чистим, но не молча: человек видит, что уйдёт в НП."""
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(edit_field="descr")
    state.state = CreateTtnState.editing_field
    message = FakeMessage("Кава 100% арабіка")
    await h.receive_edit(message, None, state, _ctx(_CLIENT), None, object())

    assert state._data["description"] == "Кава 100 відс. арабіка"
    assert any("не приймає деякі символи" in a["text"] for a in message.answers)


async def test_clean_description_does_not_nag(monkeypatch):
    """Описание без посторонних символов не должно вызывать лишнее сообщение."""
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(edit_field="descr")
    state.state = CreateTtnState.editing_field
    message = FakeMessage("Кава арабіка 250 г")
    await h.receive_edit(message, None, state, _ctx(_CLIENT), None, object())

    assert state._data["description"] == "Кава арабіка 250 г"
    assert not any("не приймає деякі символи" in a["text"] for a in message.answers)
