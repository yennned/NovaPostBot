"""Тесты потока создания ТТН — каркас + кошик (Фаза 4, PR 9a).

ФОП-гейт входа идёт на реальном Postgres (через `sender_profile.list_profiles`);
набор корзины/степпер/параметри — чистые (инвентарь замокан, БД не нужна).
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.bot.handlers import ttn as h
from app.bot.states import CreateTtnState
from app.bot.texts import ttn as ttn_texts
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import SenderProfileRepository, UserRepository
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


class FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
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


def _item(sku: str, name: str, available: int, price: str | None = "100") -> InventoryItem:
    return InventoryItem(
        sku=sku,
        name=name,
        category=None,
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
        session, *, client, query=None, category=None, limit=8, offset=0, reader=None
    ):
        return page

    monkeypatch.setattr(h, "list_inventory", fake_list_inventory)


def _ctx(client):
    return SimpleNamespace(effective_user=client, actor_user=client)


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


async def test_entry_ok_shows_picker(db_session: AsyncSession, monkeypatch):
    client = await _active_client(db_session, 903)
    await SenderProfileRepository(db_session).create(
        client_id=client.id, name="ФОП", np_api_key="k", is_default=True, np_sender_ref="cp-1"
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


# ----------------------------------------------------------------- кошик (чистые)


async def test_pick_opens_stepper(monkeypatch):
    _patch_inventory(monkeypatch, _page([_item("SKU1", "Кава", 24)]))
    state = FakeState(cart_offset=0, cart={})
    cb = FakeCallback("cab:ttn:pick:0")
    await h.cb_pick(cb, _ctx(_CLIENT), None, state)
    assert state._data["pending"]["sku"] == "SKU1"
    assert state._data["pending"]["qty"] == 1
    assert cb.message.edits  # степпер отрисован


async def test_pick_zero_available_blocked(monkeypatch):
    _patch_inventory(monkeypatch, _page([_item("SKU0", "Немає", 0)]))
    state = FakeState(cart_offset=0, cart={})
    cb = FakeCallback("cab:ttn:pick:0")
    await h.cb_pick(cb, _ctx(_CLIENT), None, state)
    assert "pending" not in state._data
    assert cb.acks[-1]["show_alert"] is True


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
    await h.receive_qty(msg, state)
    assert "1–5" in msg.answers[-1]["text"]  # отклонено
    assert state.state == CreateTtnState.entering_qty


async def test_receive_qty_accepts():
    state = FakeState(pending={"sku": "S", "name": "X", "available": 5, "price": "100", "qty": 1})
    await state.set_state(CreateTtnState.entering_qty)
    msg = FakeMessage(text="3")
    await h.receive_qty(msg, state)
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
    await h.receive_weight(msg, state)
    assert "Невірна вага" in msg.answers[-1]["text"]
    assert "weight" not in state._data
    assert state.state == CreateTtnState.entering_weight


async def test_receive_weight_accepts_comma():
    state = FakeState(size_token="s")
    await state.set_state(CreateTtnState.entering_weight)
    msg = FakeMessage(text="2,5")
    await h.receive_weight(msg, state)
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


def _patch_cities(monkeypatch, cities):
    async def fake(session, *, client, query, np_client, cache, sender_profile_id=None):
        return cities

    monkeypatch.setattr(h.address, "search_cities", fake)


def _patch_warehouses(monkeypatch, whs):
    async def fake(
        session, *, client, city_ref, np_client, cache, query=None, sender_profile_id=None
    ):
        return whs

    monkeypatch.setattr(h.address, "search_warehouses", fake)


async def test_city_query_shows_results(monkeypatch):
    _patch_cities(monkeypatch, [City(ref="c1", name="Київ", area="Київська")])
    state = FakeState()
    await state.set_state(CreateTtnState.entering_city_query)
    msg = FakeMessage(text="Київ")
    await h.receive_city_query(msg, state, _ctx(_CLIENT), None, object(), object())
    assert state._data["cities"][0]["ref"] == "c1"
    assert msg.answers[-1]["reply_markup"] is not None


async def test_city_query_not_found(monkeypatch):
    _patch_cities(monkeypatch, [])
    state = FakeState()
    await state.set_state(CreateTtnState.entering_city_query)
    msg = FakeMessage(text="Хххх")
    await h.receive_city_query(msg, state, _ctx(_CLIENT), None, object(), object())
    assert "cities" not in state._data
    assert "Нічого не знайшли" in msg.answers[-1]["text"]


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


def _patch_pricing(monkeypatch, *, quote=None, raise_exc=None, counter=None):
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
        settings=None,
    ):
        if counter is not None:
            counter["n"] = counter.get("n", 0) + 1
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
    assert state._data["insured_amount"] == "300"  # 150 × 2
    assert state._data["description"] == "Кава"
    assert state._data["payment_method"] == "prepay"
    assert state._data["payer_type"] == "Recipient"


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
    await h.receive_edit(msg, state, _ctx(_CLIENT), None, object())
    assert state._data["recipient_name"] == "Петренко Петро"
    assert state.state == CreateTtnState.summary
    assert msg.answers  # карточка перерисована


async def test_receive_edit_phone_invalid_stays(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(edit_field="phone")
    msg = FakeMessage(text="not-a-phone")
    await h.receive_edit(msg, state, _ctx(_CLIENT), None, object())
    assert state._data["recipient_phone"] == "380671234567"  # не изменился


async def test_receive_edit_weight_updates(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(edit_field="weight")
    msg = FakeMessage(text="3,2")
    await h.receive_edit(msg, state, _ctx(_CLIENT), None, object())
    assert state._data["weight"] == "3.2"


async def test_receive_edit_cod_sets_payment(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(edit_field="cod")
    msg = FakeMessage(text="500")
    await h.receive_edit(msg, state, _ctx(_CLIENT), None, object())
    assert state._data["cod_amount"] == "500"
    assert state._data["payment_method"] == "cod"


async def test_receive_edit_cod_zero_rejected(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(edit_field="cod")
    msg = FakeMessage(text="0")
    await h.receive_edit(msg, state, _ctx(_CLIENT), None, object())
    assert "cod_amount" not in state._data


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
    state = _card_state(payment_method="cod", cod_amount="300")
    cb = FakeCallback("cab:ttn:setpm:prepay")
    await h.cb_set_payment(cb, _ctx(_CLIENT), None, object(), state)
    assert state._data["payment_method"] == "prepay"
    assert state._data["cod_amount"] is None


async def test_set_payment_cod_prompts_amount():
    state = _card_state()
    cb = FakeCallback("cab:ttn:setpm:cod")
    await h.cb_set_payment(cb, _ctx(_CLIENT), None, object(), state)
    assert state.state == CreateTtnState.editing_field
    assert state._data["edit_field"] == "cod"
    # payment_method ещё НЕ cod — выставится после ввода суммы
    assert state._data.get("payment_method") != "cod"


async def test_cod_equal_uses_insured(monkeypatch):
    _patch_pricing(monkeypatch, quote=_quote())
    state = _card_state(insured_amount="450")
    cb = FakeCallback("cab:ttn:codeq")
    await h.cb_cod_equal(cb, _ctx(_CLIENT), None, object(), state)
    assert state._data["cod_amount"] == "450"
    assert state._data["payment_method"] == "cod"


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


def _patch_create(monkeypatch, *, ttn="59000123", raise_exc=None, calls=None):
    async def fake(session, **kw):
        if calls is not None:
            calls["n"] = calls.get("n", 0) + 1
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

    async def fake_start(message, state, ctx, session):
        spy["called"] = True

    monkeypatch.setattr(h, "start_create_ttn", fake_start)
    cb = FakeCallback("cab:ttn:again")
    await h.cb_again(cb, _ctx(_CLIENT), None, FakeState())
    assert spy.get("called") is True
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
