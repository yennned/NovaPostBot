"""Пуш-уведомления и маршрутизация получателей Phase 5."""

from __future__ import annotations

import asyncio
import html
import uuid
from collections.abc import Iterable
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import permissions
from app.config import Settings, get_settings
from app.db.models.client_account import ClientAccount, ClientAccountMembership
from app.db.models.enums import MembershipStatus, ShipmentStatus, UserRole, UserStatus
from app.db.models.shipment import Shipment
from app.db.models.user import User
from app.db.repositories import NotificationSettingRepository, UserRepository
from app.services.client_settings import (
    DEFAULT_NOTIFICATION_SETTINGS,
    NOTIFY_ALL_ACCOUNT_SHIPMENTS,
    NOTIFY_LOW_STOCK,
    NOTIFY_SHIPMENT_STATUS,
)
from app.services.inventory import InventoryItem


class Notifier(Protocol):
    """Транспорт отправки. Бот-слой реализует поверх aiogram `Bot.send_message`."""

    async def send_message(self, telegram_id: int, text: str) -> None: ...


def _client_label(client: User) -> str:
    name = client.full_name or "без імені"
    phone = client.phone or "—"
    return f"{name} ({phone})"


def _money(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}"


def duty_shift_ended_text() -> str:
    """Пуш менеджеру при авто-снятии дежурства (закрытие отделения)."""
    return (
        "🔘 Зміну завершено — відділення зачинилося, ви більше не на звʼязку. "
        "Завтра відкрийте зміну кнопкою «🟢 Я на звʼязку»."
    )


def support_message_for_manager_text(client: User, text: str) -> str:
    """Релей сообщения клиента дежурному менеджеру (HTML — экранируем)."""
    return f"💬 Звернення від {html.escape(_client_label(client))}:\n{html.escape(text)}"


def support_message_for_client_text(text: str) -> str:
    """Релей ответа менеджера клиенту (HTML — экранируем)."""
    return f"💬 Менеджер:\n{html.escape(text)}"


def support_thread_closed_text() -> str:
    """Уведомление клиенту о закрытии обращения менеджером."""
    return (
        "✅ Менеджер закрив звернення. За потреби напишіть нове через «💬 Звернення до менеджера»."
    )


def support_thread_closed_by_client_text(client: User) -> str:
    """Уведомление дежурному менеджеру: клиент сам завершил обращение."""
    return f"✅ Клієнт {html.escape(_client_label(client))} завершив звернення."


def manager_added_text() -> str:
    """Уведомление новому менеджеру о выдаче доступа."""
    return (
        "👔 Вам надано доступ менеджера. Натисніть /start, щоб відкрити меню. "
        "Зміну відкривайте кнопкою «🟢 Я на звʼязку»."
    )


def new_client_text(client: User) -> str:
    return (
        "🆕 <b>Нова заявка на реєстрацію</b>\n"
        f"Клієнт: {_client_label(client)}\n"
        "Підтвердьте або заблокуйте у розділі «Клієнти»."
    )


def client_approved_text() -> str:
    return (
        "✅ <b>Вашу заявку підтверджено!</b>\n"
        "Тепер вам доступний особистий кабінет. Натисніть /start, щоб почати."
    )


def new_shipment_text(client: User, ttn_number: str | None) -> str:
    ttn = ttn_number or "—"
    return (
        "📦 <b>Нова ТТН від клієнта</b>\n"
        f"Клієнт: {_client_label(client)}\n"
        f"№ ТТН: <code>{ttn}</code>\n"
        "Дивіться у розділі «Відправлення» → «Створені»."
    )


def shipment_status_text(shipment: Shipment) -> str:
    ttn = shipment.ttn_number or "—"
    labels = {
        ShipmentStatus.created: "створено",
        ShipmentStatus.confirmed: "підтверджено менеджером",
        ShipmentStatus.dispatched: "відправлено",
        ShipmentStatus.in_transit: "у дорозі",
        ShipmentStatus.arrived: "прибуло у відділення",
        ShipmentStatus.delivered: "вручено",
        ShipmentStatus.returning: "посилка повертається",
        ShipmentStatus.returned: "повернення прийнято на склад",
        ShipmentStatus.lost: "посилку втрачено",
        ShipmentStatus.damaged: "посилку пошкоджено",
        ShipmentStatus.cancelled: "скасовано",
    }
    lines = [
        "📬 <b>Оновлення статусу відправлення</b>",
        f"№ ТТН: <code>{ttn}</code>",
        f"Статус: <b>{labels.get(shipment.status, shipment.status.value)}</b>",
    ]
    if shipment.status is ShipmentStatus.dispatched and shipment.sla_met is not None:
        lines.append("SLA: " + ("вчасно" if shipment.sla_met else "прострочено"))
    if shipment.status is ShipmentStatus.cancelled and shipment.cancellation_reason:
        lines.append(f"Причина: {html.escape(shipment.cancellation_reason)}")
    return "\n".join(lines)


def shipment_cancelled_text(client: User, shipment: Shipment) -> str:
    text = (
        "❌ <b>Клієнт скасував ТТН</b>\n"
        f"Клієнт: {_client_label(client)}\n"
        f"№ ТТН: <code>{shipment.ttn_number or '—'}</code>"
    )
    if shipment.cancellation_reason:
        text += f"\nПричина: {html.escape(shipment.cancellation_reason)}"
    return text


def account_low_stock_text(account: ClientAccount, items: list[InventoryItem]) -> str:
    """То же для персонала, но подписано аккаунтом, а не участником команды.

    Склад принадлежит аккаунту; подпись человеком означала бы, что менеджер видит
    имя того, до кого первым дошёл цикл, — и по одному и тому же складу в разные
    дни приходили бы письма от разных людей.
    """
    lines = [
        "📦 <b>Низький залишок</b>",
        f"Акаунт: {account.name}",
    ]
    for item in items[:10]:
        lines.append(
            f"• <b>{item.sku}</b> — {item.name}: доступно {item.available}, резерв {item.reserved}"
        )
    return "\n".join(lines)


def client_low_stock_text(items: list[InventoryItem]) -> str:
    lines = ["📦 <b>Увага: низький залишок</b>"]
    for item in items[:10]:
        line = (
            f"• <b>{item.sku}</b> — {item.name}: "
            f"доступно {item.available} · ціна {_money(item.price)}"
        )
        lines.append(line)
    return "\n".join(lines)


def nonstandard_shipment_text(shipment: Shipment, *, note: str | None = None) -> str:
    lines = [
        "⚠️ <b>Нестандартна ситуація по ТТН</b>",
        f"№ ТТН: <code>{shipment.ttn_number or '—'}</code>",
        f"Статус: <b>{shipment.status.value}</b>",
    ]
    if note:
        lines.append(note)
    lines.append("За потреби звʼяжіться з менеджером.")
    return "\n".join(lines)


async def _staff_recipient_ids(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
) -> set[int]:
    users = UserRepository(session)
    current_settings = settings or get_settings()
    recipient_ids: set[int] = set(current_settings.owner_telegram_ids)
    for owner in await users.list_by_role(UserRole.owner):
        if owner.status is UserStatus.active:
            recipient_ids.add(owner.telegram_id)
    recipient_ids |= await _manager_recipient_ids(session)
    return recipient_ids


async def _manager_recipient_ids(session: AsyncSession) -> set[int]:
    users = UserRepository(session)
    recipient_ids: set[int] = set()
    for manager in await users.list_by_role(UserRole.manager):
        # telegram_id может быть None у менеджера, заведённого по телефону и ещё не
        # вошедшего в бота (адопция при первом входе) — таким пуш не отправляем.
        if (
            manager.telegram_id is not None
            and manager.status is UserStatus.active
            and manager.on_duty
        ):
            recipient_ids.add(manager.telegram_id)
    return recipient_ids


async def _support_manager_recipient_ids(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
) -> set[int]:
    """Активные менеджеры с правом обрабатывать поддержку (`can_handle_support`)."""
    current_settings = settings or get_settings()
    recipient_ids: set[int] = set()
    for manager in await UserRepository(session).list_by_role(UserRole.manager):
        # Пропускаем предзаготовленного по телефону менеджера без telegram_id.
        if (
            manager.telegram_id is not None
            and manager.status is UserStatus.active
            and permissions.has_permission(
                manager, permissions.CAN_HANDLE_SUPPORT, current_settings
            )
        ):
            recipient_ids.add(manager.telegram_id)
    return recipient_ids


async def notify_support_queued_to_managers(
    session: AsyncSession,
    notifier: Notifier,
    *,
    client_label: str,
    settings: Settings | None = None,
) -> None:
    """Обращение в очереди в рабочее время без дежурного — сигнал менеджерам.

    Поддержка — функция менеджера (не владельца): пингуем всех активных менеджеров
    с правом `can_handle_support`, чтобы кто-то заступил «🟢 Я на зв'язку» и ответил.
    `client_label` передаётся строкой (а не ORM-объектом): хендлер формирует её до
    commit, чтобы пуш после commit не упёрся в expired-атрибуты.
    """
    text = (
        "⚠️ Звернення клієнта в черзі, але немає чергового менеджера.\n"
        f"Клієнт: {html.escape(client_label)}.\n"
        "Заступіть на зв'язок «🟢 Я на зв'язку» або відповідайте через «💬 Підтримка»."
    )
    await _send_many(
        notifier, await _support_manager_recipient_ids(session, settings=settings), text
    )


def _resolve_setting(user: User, key: str, overrides: dict[tuple[uuid.UUID, str], bool]) -> bool:
    """Значение настройки из уже прочитанной пачки. Приоритет тот же, что у
    `_notification_enabled`: строка в БД перебивает право пользователя, оно —
    дефолт. Держим их рядом, чтобы приоритет нельзя было разъехать незаметно."""
    default = bool(DEFAULT_NOTIFICATION_SETTINGS.get(key, True))
    override = overrides.get((user.id, key))
    if override is not None:
        return override
    return bool(user.permissions.get(key, default))


async def _notification_enabled(
    session: AsyncSession,
    *,
    user: User,
    key: str,
) -> bool:
    default = bool(DEFAULT_NOTIFICATION_SETTINGS.get(key, True))
    enabled = bool(user.permissions.get(key, default))
    row = await NotificationSettingRepository(session).get_by_user_and_key(user.id, key)
    if row is not None:
        enabled = row.enabled
    return enabled


async def _send_many(notifier: Notifier, recipient_ids: Iterable[int], text: str) -> None:
    # Отсекаем None (напр. менеджер по телефону без telegram_id): send_message(None)
    # упал бы и через gather оборвал бы рассылку остальным.
    unique_ids = [tid for tid in dict.fromkeys(recipient_ids) if tid is not None]
    await asyncio.gather(*(notifier.send_message(tid, text) for tid in unique_ids))


async def notify_new_client_registered(
    session: AsyncSession, notifier: Notifier, *, client: User
) -> None:
    await _send_many(notifier, await _staff_recipient_ids(session), new_client_text(client))


async def notify_shipment_created(
    session: AsyncSession, notifier: Notifier, *, client: User, ttn_number: str | None
) -> None:
    await _send_many(
        notifier, await _staff_recipient_ids(session), new_shipment_text(client, ttn_number)
    )


async def notify_client_approved(notifier: Notifier, *, client: User) -> None:
    await notifier.send_message(client.telegram_id, client_approved_text())


async def notify_shipment_status_changed(
    session: AsyncSession,
    notifier: Notifier,
    *,
    shipment: Shipment,
) -> None:
    """Уведомить активную команду аккаунта об изменении статуса ТТН.

    Получатели — участники аккаунта отправления. Отдельного `client` тут нет и
    быть не может: `shipments.account_id` NOT NULL, поэтому «клиент без аккаунта»
    (когда уведомляли одного `client.telegram_id`) — недостижимое состояние.
    """
    recipients: list[int] = []
    members = await session.scalars(
        select(User)
        .join(ClientAccountMembership, ClientAccountMembership.user_id == User.id)
        .where(
            ClientAccountMembership.account_id == shipment.account_id,
            ClientAccountMembership.status == MembershipStatus.active,
            User.status == UserStatus.active,
        )
    )
    # Настройки всех получателей — ОДНИМ запросом. Раньше цикл делал один-два
    # SELECT на каждого участника, и они шли подряд: задержка росла как N × RTT до
    # Neon, а не как max(RTT). Внутри прохода трекинга, который выдаёт сотню таких
    # вееров за раз, это и превращалось в минуты на ровном месте.
    member_list = list(members)
    overrides = await NotificationSettingRepository(session).map_for_users(
        [member.id for member in member_list],
        (NOTIFY_ALL_ACCOUNT_SHIPMENTS, NOTIFY_SHIPMENT_STATUS),
    )
    for member in member_list:
        own = shipment.created_by_user_id == member.id
        all_account = _resolve_setting(member, NOTIFY_ALL_ACCOUNT_SHIPMENTS, overrides)
        if (own or all_account) and _resolve_setting(member, NOTIFY_SHIPMENT_STATUS, overrides):
            recipients.append(member.telegram_id)
    await _send_many(notifier, recipients, shipment_status_text(shipment))


async def notify_account_low_stock(
    session: AsyncSession,
    notifier: Notifier,
    *,
    account: ClientAccount,
    items: list[InventoryItem],
) -> None:
    """Низкий остаток — всей активной команде аккаунта, персоналу один раз.

    Прежде джоба звала `notify_low_stock` в цикле по клиентам, а состояние алертов
    хранится **по аккаунту** (`upsert_state(account_id=…)`). Значит первый же
    участник, до которого доходил цикл, помечал SKU как «уже уведомили», и
    остальная команда не получала ничего. Кто именно окажется первым — зависело от
    порядка выборки пользователей, то есть было произвольным.

    Склад общий, поэтому и адресат — команда, а не один её участник. Настройки всех
    получателей читаются одним запросом: джоба ходит по всем аккаунтам сразу, и
    цикл из SELECT'ов на человека дал бы ту же N × RTT, что уже убрана в
    статус-пушах.
    """
    if not items:
        return
    members = list(
        await session.scalars(
            select(User)
            .join(ClientAccountMembership, ClientAccountMembership.user_id == User.id)
            .where(
                ClientAccountMembership.account_id == account.id,
                ClientAccountMembership.status == MembershipStatus.active,
                User.status == UserStatus.active,
            )
        )
    )
    overrides = await NotificationSettingRepository(session).map_for_users(
        [member.id for member in members], (NOTIFY_LOW_STOCK,)
    )
    recipients = [
        member.telegram_id
        for member in members
        if _resolve_setting(member, NOTIFY_LOW_STOCK, overrides)
    ]
    await _send_many(notifier, recipients, client_low_stock_text(items))
    await _send_many(
        notifier, await _staff_recipient_ids(session), account_low_stock_text(account, items)
    )


async def notify_stock_ingest_halted(
    session: AsyncSession,
    notifier: Notifier,
    *,
    reason: str,
    settings: Settings | None = None,
) -> None:
    """Ингест приёмки остановлен — молчать нельзя.

    Останавливаемся мы только на нарушенной целостности журнала, и это состояние
    само не рассосётся: пока человек не разберётся, приёмка в Postgres не едет, а
    остаток тихо расходится с листом. Владельцам и дежурным менеджерам.
    """
    current_settings = settings or get_settings()
    text = (
        "⚠️ <b>Інгест приймання зупинено</b>\n"
        f"{html.escape(reason)}\n\n"
        "Залишок у Postgres перестав оновлюватися. Потрібно звірити лист "
        "«Історія» книги «Склад» і перезапустити інгест."
    )
    await _send_many(notifier, await _staff_recipient_ids(session, settings=current_settings), text)


async def notify_staff(
    session: AsyncSession,
    notifier: Notifier,
    *,
    text: str,
    settings: Settings | None = None,
) -> None:
    """Готовый текст владельцам и дежурным менеджерам — для служебных сводок."""
    await _send_many(
        notifier, await _staff_recipient_ids(session, settings=settings or get_settings()), text
    )


async def notify_stock_manual_edits(
    session: AsyncSession,
    notifier: Notifier,
    *,
    account_label: str,
    applied: list[tuple[str, int, int, str]],
    rejected: list[tuple[str, int, int, str]],
    settings: Settings | None = None,
) -> None:
    """Ручные правки количества прямо в листе «Склад».

    Сообщаем и о принятых, и об отклонённых. Принятые — потому что изменение
    остатка мимо приёмки и отгрузки обязано быть видимым; отклонённые — потому что
    иначе человек будет считать, что поправил, а число вернётся обратно, и это
    выглядит как сбой бота.

    Автор берётся из журнала `_Правки` книги «Склад» и может быть пуст: Apps
    Script в книге не установлен либо Google не отдал адрес правившего. Пустого
    «хто» в тексте не показываем — строка «правив ―» не сообщает ничего.
    """
    if not applied and not rejected:
        return
    lines = [f"✏️ <b>Ручні правки залишку</b> · {html.escape(account_label)}"]
    for sku, was, now, author in applied:
        who = f" · {html.escape(author)}" if author else ""
        lines.append(f"• {html.escape(sku)}: {was} → {now}{who}")
    for sku, was, now, reason in rejected:
        lines.append(
            f"• ⛔ {html.escape(sku)}: {was} → {now} — не застосовано ({html.escape(reason)}), "
            "значення повернуто"
        )
    await _send_many(
        notifier,
        await _staff_recipient_ids(session, settings=settings or get_settings()),
        "\n".join(lines),
    )


async def notify_shipment_cancelled_by_client(
    session: AsyncSession,
    notifier: Notifier,
    *,
    client: User,
    shipment: Shipment,
) -> None:
    await _send_many(
        notifier, await _staff_recipient_ids(session), shipment_cancelled_text(client, shipment)
    )


async def notify_nonstandard_shipment(
    session: AsyncSession,
    notifier: Notifier,
    *,
    client: User,
    shipment: Shipment,
    note: str | None = None,
) -> None:
    text = nonstandard_shipment_text(shipment, note=note)
    recipients = [client.telegram_id, *(await _manager_recipient_ids(session))]
    await _send_many(notifier, recipients, text)
