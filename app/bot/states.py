"""FSM состояния bot-layer."""

from aiogram.fsm.state import State, StatesGroup


class StartStates(StatesGroup):
    waiting_for_contact = State()


class ClientManageState(StatesGroup):
    waiting_for_search = State()  # ждём строку поиска (в data: status-токен)
    waiting_for_edit = State()  # ждём новое значение (в data: client_id, field, token)


class ClientCabinetState(StatesGroup):
    waiting_for_product_search = State()
    waiting_for_shipment_search = State()
    waiting_for_stats_date = State()
    waiting_for_settings_profile = State()
    waiting_for_sender_profile_edit = State()


class ManagerShipmentState(StatesGroup):
    waiting_for_search = State()
    inspecting_return = State()


class SupportState(StatesGroup):
    """Поддержка Фазы 6. Длинные значения (thread_id) — в FSM-data."""

    client_chatting = State()  # клиент в чате обращения (data: support_thread_id)
    manager_replying = State()  # дежурный печатает ответ (data: support_thread_id)
    log_search = State()  # owner/dev: ввод строки поиска по логу


class StaffState(StatesGroup):
    """Управление персоналом (👔, owner-only)."""

    waiting_for_search = State()  # ввод строки поиска по менеджерам
    waiting_for_add = State()  # ввод телефона или Telegram-ID нового менеджера


class ReportsState(StatesGroup):
    """Отчёты «📊 Звіти» (менеджер): ручной ввод даты отчёта."""

    waiting_for_date = State()


class AnalyticsState(StatesGroup):
    """Аналитика «📈 Аналітика» (владелец): ручной ввод даты."""

    waiting_for_date = State()


class CreateTtnState(StatesGroup):
    """FSM создания ТТН (Express-картка, Фаза 4 PR 9). Длинные значения — в FSM-data.

    PR 9a покрывает кошик→параметри→тип отримувача; шаги отримувача/адреси/картки
    добавят PR 9b–9d.
    """

    picking_items = State()  # просмотр товаров/набор корзины (callbacks)
    entering_item_search = State()  # текстовый поиск товара в рамках создания ТТН
    entering_qty = State()  # текстовый ввод количества для выбранной позиции
    picking_parcel = State()  # экран «Параметри посилки» (вага+габарити)
    entering_weight = State()  # текстовый ввод веса
    picking_recipient_kind = State()  # розвилка особа/організація
    entering_recipient_name = State()  # ПІБ / назва організації
    entering_recipient_edrpou = State()  # ЄДРПОУ/ІПН (только organization)
    entering_recipient_phone = State()  # телефон отримувача
    entering_city_query = State()  # поиск города (текст → результаты)
    entering_warehouse_query = State()  # выбор/поиск відділення
    summary = State()  # карточка-зведення
    editing_field = State()  # точечная правка текстового поля карточки (edit_field в data)
