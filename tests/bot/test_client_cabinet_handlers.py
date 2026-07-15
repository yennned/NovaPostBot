"""Тесты хендлеров кабинета клиента Фазы 3."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import httpx
from app.bot.handlers.client_cabinet import (
    cb_calendar_day,
    cb_cancel_shipment,
    cb_settings_toggle,
    cb_shipment_card,
    cb_stats,
    open_products,
    open_settings,
    open_shipments,
    receive_new_profile_key,
    receive_new_profile_name,
    receive_new_profile_phone,
    receive_new_profile_sender_name,
)
from app.bot.states import SenderProfileCreateState
from app.bot.types import ClientAccountContext, EffectiveContext
from app.config import Settings
from app.db.models.enums import ShipmentStatus, UserRole, UserStatus
from app.db.repositories import ClientAccountRepository, SenderProfileRepository, UserRepository
from app.novaposhta.client import NovaPoshtaClient
from app.services.client_settings import ClientSettingsView, NotificationSettingView
from app.services.inventory import InventoryItem, InventoryPage
from app.services.shipments import (
    ShipmentCard,
    ShipmentItemView,
    ShipmentListItemView,
    ShipmentPage,
)
from app.services.stats import ClientStatsSnapshot, TopSkuStat
from sqlalchemy.ext.asyncio import AsyncSession


async def _ctx(session, client) -> EffectiveContext:
    """Настоящий EffectiveContext — как его собирает мидлварь.

    Включая `account_context`: у клиента аккаунт есть всегда, и склад/ТТН
    account-scoped. Контекст без аккаунта — сломанное состояние, которое сервисы
    теперь отвергают (`shipments.require_client_account`), поэтому и в тестах его
    строить нельзя: тест на несуществующем состоянии ничего не доказывает.
    """
    account_scope = await ClientAccountRepository(session).get_context_for_user(client.id)
    context = EffectiveContext(
        actor_user=client,
        effective_user=client,
        effective_role=UserRole.client,
        is_dev=False,
    )
    if account_scope is not None:
        account, membership = account_scope
        context.account_context = ClientAccountContext(
            user=client, account=account, membership=membership
        )
    return context


class FakeState:
    def __init__(self) -> None:
        self.cleared = False
        self.state = None
        self._data = {}

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
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[dict] = []
        self.edits: list[dict] = []
        self.deleted = False

    async def answer(self, text, reply_markup=None, parse_mode=None, **kwargs) -> None:
        self.answers.append({"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode})

    async def edit_text(self, text, reply_markup=None, parse_mode=None, **kwargs) -> None:
        self.edits.append({"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode})

    async def delete(self) -> None:
        self.deleted = True


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = FakeMessage()
        self.acks: list[dict] = []

    async def answer(self, text=None, show_alert=False) -> None:
        self.acks.append({"text": text, "show_alert": show_alert})


async def _active_client(session: AsyncSession, telegram_id: int = 700):
    return await UserRepository(session).create(
        telegram_id=telegram_id,
        full_name="Клієнт Фази 3",
        role=UserRole.client,
        status=UserStatus.active,
    )


def _settings_view(*, shipment_status: bool = True) -> ClientSettingsView:
    return ClientSettingsView(
        full_name="Клієнт Фази 3",
        phone="+380001",
        notifications=[
            NotificationSettingView(
                key="notify_registration_approved",
                label="Підтвердження реєстрації",
                enabled=True,
            ),
            NotificationSettingView(
                key="notify_shipment_status",
                label="Статуси відправлень",
                enabled=shipment_status,
            ),
            NotificationSettingView(
                key="notify_low_stock",
                label="Залишки та low-stock",
                enabled=True,
            ),
        ],
        sender_profiles_count=1,
        default_sender_name="ФОП-1",
    )


async def test_open_products_renders_inventory(db_session: AsyncSession, monkeypatch):
    client = await _active_client(db_session)
    msg = FakeMessage()

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
        return InventoryPage(
            items=[
                InventoryItem(
                    sku="SKU-1",
                    name="Кава",
                    category="Кава",
                    stock=10,
                    reserved=2,
                    available=8,
                    price=Decimal("100.00"),
                )
            ],
            total=1,
            limit=limit,
            offset=offset,
            categories=["Кава"],
        )

    monkeypatch.setattr("app.bot.handlers.client_cabinet.list_inventory", fake_list_inventory)
    await open_products(
        msg,
        FakeState(),
        await _ctx(db_session, client),
        db_session,
    )

    assert msg.answers
    assert "Товари" in msg.answers[0]["text"]


async def test_open_shipments_renders_list(db_session: AsyncSession, monkeypatch):
    client = await _active_client(db_session, telegram_id=701)
    msg = FakeMessage()

    async def fake_list_shipments(
        session, *, client, bucket="created", query=None, limit=8, offset=0, **kwargs
    ):
        return ShipmentPage(
            items=[
                ShipmentListItemView(
                    id=uuid4(),
                    ttn_number="TTN-500",
                    recipient_name="Іван",
                    status=ShipmentStatus.created,
                    created_at=datetime.now(UTC),
                    items_count=2,
                )
            ],
            total=1,
            limit=limit,
            offset=offset,
        )

    monkeypatch.setattr("app.bot.handlers.client_cabinet.list_shipments", fake_list_shipments)
    await open_shipments(
        msg,
        FakeState(),
        await _ctx(db_session, client),
        db_session,
    )

    assert msg.answers
    assert "Відправлення" in msg.answers[0]["text"] or "Створені" in msg.answers[0]["text"]


async def test_cb_shipment_card_renders_card(db_session: AsyncSession, monkeypatch):
    client = await _active_client(db_session, telegram_id=702)
    shipment_id = uuid4()
    cb = FakeCallback(data=f"cab:shipment:created:0:{shipment_id}")

    async def fake_get_shipment_card(session, *, client, shipment_id, **kwargs):
        return ShipmentCard(
            id=shipment_id,
            ttn_number="TTN-777",
            recipient_name="Іван",
            recipient_phone="+380001",
            recipient_city="Київ",
            recipient_warehouse="Відділення 1",
            status=ShipmentStatus.created,
            created_at=datetime.now(UTC),
            status_changed_at=datetime.now(UTC),
            dispatched_at=None,
            sla_deadline=None,
            sla_met=None,
            payment_method="cod",
            payer_type="recipient",
            cod_amount=Decimal("500.00"),
            insured_amount=Decimal("700.00"),
            fee_amount=Decimal("21.00"),
            fee_free=False,
            items=[
                ShipmentItemView(
                    sku="SKU-1",
                    name="Кава",
                    category="Кава",
                    quantity=2,
                    unit_price=Decimal("100.00"),
                )
            ],
            can_cancel=True,
        )

    monkeypatch.setattr("app.bot.handlers.client_cabinet.get_shipment_card", fake_get_shipment_card)
    await cb_shipment_card(
        cb,
        await _ctx(db_session, client),
        db_session,
        FakeState(),
    )

    assert cb.message.edits
    assert "Картка відправлення" in cb.message.edits[0]["text"]


async def test_cb_cancel_shipment_updates_card(db_session: AsyncSession, monkeypatch):
    client = await _active_client(db_session, telegram_id=704)
    shipment_id = uuid4()
    cb = FakeCallback(data=f"cab:cancel:created:0:{shipment_id}")

    async def fake_cancel_shipment(session, *, client, shipment_id, np_client, **kwargs):
        # Хендлер не использует возврат — после отмены ререндерит список группы.
        return None

    monkeypatch.setattr("app.bot.handlers.client_cabinet.cancel_shipment", fake_cancel_shipment)
    await cb_cancel_shipment(
        cb,
        await _ctx(db_session, client),
        db_session,
        object(),  # np_client (фейк — реальная отмена замокана)
        FakeState(),
    )

    # После #49 отмена возвращает в список группы (single-window), а не в карточку.
    assert cb.message.edits
    assert "Створені" in cb.message.edits[0]["text"]
    assert cb.acks[-1]["text"] == "ТТН видалено."


async def test_open_settings_renders_view(db_session: AsyncSession, monkeypatch):
    client = await _active_client(db_session, telegram_id=705)
    msg = FakeMessage()

    async def fake_get_client_settings(session, *, client, **kwargs):
        return _settings_view()

    monkeypatch.setattr(
        "app.bot.handlers.client_cabinet.client_settings.get_client_settings",
        fake_get_client_settings,
    )
    await open_settings(
        msg,
        FakeState(),
        await _ctx(db_session, client),
        db_session,
    )

    assert msg.answers
    assert "Налаштування" in msg.answers[0]["text"]


async def test_cb_settings_toggle_updates_view(db_session: AsyncSession, monkeypatch):
    client = await _active_client(db_session, telegram_id=706)
    cb = FakeCallback(data="cab:set:toggle:shp")

    async def fake_toggle_notification(session, *, client, key, **kwargs):
        assert key == "notify_shipment_status"
        return _settings_view(shipment_status=False)

    monkeypatch.setattr(
        "app.bot.handlers.client_cabinet.client_settings.toggle_notification",
        fake_toggle_notification,
    )
    await cb_settings_toggle(
        cb,
        await _ctx(db_session, client),
        db_session,
        FakeState(),
    )

    assert cb.message.edits
    assert "вимкнено" in cb.message.edits[0]["text"]
    assert cb.acks[-1]["text"] == "Налаштування оновлено."


async def test_cb_stats_renders_period(db_session: AsyncSession, monkeypatch):
    client = await _active_client(db_session, telegram_id=703)
    cb = FakeCallback(data="cab:stats:week")

    async def fake_get_client_stats(session, *, client, period="today", **kwargs):
        now = datetime.now(UTC)
        return ClientStatsSnapshot(
            period=period,
            start=now,
            end=now,
            shipped_qty=5,
            returns_qty=1,
            losses_qty=0,
            net_sales_qty=4,
            total_available=9,
            top_skus=[TopSkuStat(sku="SKU-1", quantity=5)],
        )

    monkeypatch.setattr("app.bot.handlers.client_cabinet.get_client_stats", fake_get_client_stats)
    await cb_stats(cb, await _ctx(db_session, client), db_session, FakeState())

    assert cb.message.edits
    assert "Статистика" in cb.message.edits[0]["text"]


def _np_client(routes: dict) -> NovaPoshtaClient:
    settings = Settings(_env_file=None)
    settings.np_retry_backoff = 0.0

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        result = routes[(body["modelName"], body["calledMethod"])]
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(
            200, json={"success": True, "data": result, "errors": [], "errorCodes": []}
        )

    return NovaPoshtaClient(settings=settings, transport=httpx.MockTransport(handler))


_VALID_KEY_ROUTES = {
    ("Counterparty", "getCounterparties"): [{"Ref": "sender-cp"}],
    ("Counterparty", "getCounterpartyContactPersons"): [{"Ref": "sender-contact"}],
}


async def test_add_fop_wizard_creates_validated_profile(db_session: AsyncSession):
    client = await _active_client(db_session, telegram_id=710)
    ctx = await _ctx(db_session, client)
    state = FakeState()

    await receive_new_profile_name(FakeMessage("ФОП Тест"), state)
    assert state.state == SenderProfileCreateState.entering_api_key

    key_msg = FakeMessage("np-secret-key")
    await receive_new_profile_key(key_msg, state)
    assert key_msg.deleted  # секрет удалён из истории чата

    await receive_new_profile_sender_name(FakeMessage("Іван Відправник"), state)
    await receive_new_profile_phone(
        FakeMessage("0501112233"), state, ctx, db_session, _np_client(_VALID_KEY_ROUTES)
    )

    profiles = await SenderProfileRepository(db_session).list_for_client(client.id)
    assert len(profiles) == 1
    assert profiles[0].name == "ФОП Тест"
    assert profiles[0].is_default is True  # первый ФОП — основной
    assert profiles[0].np_sender_ref == "sender-cp"  # ключ провалидирован в НП
    assert profiles[0].sender_phone == "380501112233"  # телефон нормализован


async def test_add_fop_wizard_rejects_invalid_key(db_session: AsyncSession):
    client = await _active_client(db_session, telegram_id=711)
    ctx = await _ctx(db_session, client)
    state = FakeState()
    await receive_new_profile_name(FakeMessage("ФОП Х"), state)
    await receive_new_profile_key(FakeMessage("bad-key"), state)
    await receive_new_profile_sender_name(FakeMessage("Іван"), state)

    bad_np = _np_client(
        {
            ("Counterparty", "getCounterparties"): httpx.Response(
                200,
                json={"success": False, "data": [], "errors": [], "errorCodes": ["20000200068"]},
            )
        }
    )
    msg = FakeMessage("0501112233")
    await receive_new_profile_phone(msg, state, ctx, db_session, bad_np)

    # Профиль не создан, вернулись на шаг ключа для повторного ввода.
    assert await SenderProfileRepository(db_session).list_for_client(client.id) == []
    assert state.state == SenderProfileCreateState.entering_api_key


async def test_add_fop_wizard_np_unavailable_keeps_phone_step(db_session: AsyncSession):
    """НП недоступна (5xx) при проверке ключа → черновик цел, шаг телефона держим."""
    client = await _active_client(db_session, telegram_id=712)
    ctx = await _ctx(db_session, client)
    state = FakeState()
    await receive_new_profile_name(FakeMessage("ФОП Down"), state)
    await receive_new_profile_key(FakeMessage("np-secret-key"), state)
    await receive_new_profile_sender_name(FakeMessage("Іван"), state)

    down_np = _np_client(
        {("Counterparty", "getCounterparties"): httpx.Response(500, text="upstream error")}
    )
    msg = FakeMessage("0501112233")
    await receive_new_profile_phone(msg, state, ctx, db_session, down_np)

    # Профиль не создан, остаёмся на шаге телефона (не падаем без ответа).
    assert await SenderProfileRepository(db_session).list_for_client(client.id) == []
    assert state.state == SenderProfileCreateState.entering_sender_phone
    assert msg.answers and "недоступна" in str(msg.answers[-1]["text"]).lower()


async def test_calendar_range_flow_passes_date_from_to(db_session: AsyncSession, monkeypatch):
    from datetime import date

    client = await _active_client(db_session, telegram_id=730)
    ctx = await _ctx(db_session, client)
    state = FakeState()
    captured: dict = {}

    async def fake_get_client_stats(session, *, client, date_from=None, date_to=None, **kw):
        captured["from"] = date_from
        captured["to"] = date_to
        now = datetime.now(UTC)
        return ClientStatsSnapshot(
            period="range",
            start=now,
            end=now,
            shipped_qty=0,
            returns_qty=0,
            losses_qty=0,
            net_sales_qty=0,
            total_available=0,
            top_skus=[],
        )

    monkeypatch.setattr("app.bot.handlers.client_cabinet.get_client_stats", fake_get_client_stats)

    # Первый клик — начало диапазона: только запоминаем, не считаем.
    await cb_calendar_day(FakeCallback("cal:day:2026-07-01"), ctx, db_session, state)
    assert state._data["stats_cal_from"] == "2026-07-01"
    assert captured == {}

    # Второй клик — конец диапазона: применяем.
    await cb_calendar_day(FakeCallback("cal:day:2026-07-05"), ctx, db_session, state)
    assert captured["from"] == date(2026, 7, 1)
    assert captured["to"] == date(2026, 7, 5)
    assert state._data.get("stats_cal_from") is None  # состояние сброшено
