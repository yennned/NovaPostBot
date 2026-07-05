"""Тексты кабинета клиента (Фаза 3)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.db.models.enums import ShipmentStatus
from app.services.client_settings import ClientSettingsView
from app.services.inventory import InventoryItem, InventoryPage
from app.services.sender_profile import SenderProfileView
from app.services.shipments import ShipmentCard, ShipmentPage
from app.services.stats import ClientStatsSnapshot
from app.utils.timefmt import fmt_dt

_STATUS_LABELS = {
    ShipmentStatus.created: "Створено",
    ShipmentStatus.confirmed: "Підтверджено",
    ShipmentStatus.dispatched: "Відправлено",
    ShipmentStatus.in_transit: "У дорозі",
    ShipmentStatus.arrived: "Прибуло",
    ShipmentStatus.delivered: "Вручено",
    ShipmentStatus.returning: "Повертається",
    ShipmentStatus.returned: "Повернено",
    ShipmentStatus.lost: "Втрачено",
    ShipmentStatus.damaged: "Пошкоджено",
    ShipmentStatus.cancelled: "Скасовано",
}

_BUCKET_LABELS = {
    "created": "Створені",
    "confirmed": "Підтверджені",
    "returns": "Повернення",
}


def _money(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}"


def _fmt_dt(value: datetime) -> str:
    return fmt_dt(value, "%d.%m.%Y %H:%M")


def products_text(page: InventoryPage) -> str:
    parts = [f"📦 <b>Товари</b> · {page.total} позицій"]
    if page.categories:
        parts.append("Категорії: " + ", ".join(page.categories[:5]))
    if not page.items:
        parts.append("Позицій поки немає.")
    else:
        parts.append(
            "Оберіть товар кнопкою нижче. У списку показано лише доступний залишок і ціну."
        )
    return "\n".join(parts)


def _inventory_line(item: InventoryItem) -> str:
    category = f" · {item.category}" if item.category else ""
    return (
        f"• <b>{item.sku}</b> — {item.name}{category}\n"
        f"  Доступно: <b>{item.available}</b> · Ціна: {_money(item.price)}"
    )


def shipments_text(page: ShipmentPage, bucket: str) -> str:
    title = _BUCKET_LABELS.get(bucket, "Відправлення")
    parts = [f"📬 <b>{title}</b> · {page.total}"]
    if not page.items:
        parts.append("Відправлень у цій групі поки немає.")
    else:
        for item in page.items:
            label = item.ttn_number or "без ТТН"
            parts.append(
                f"• <b>{label}</b> — {item.recipient_name} · "
                f"{_STATUS_LABELS[item.status]} · {item.items_count} шт"
            )
    return "\n".join(parts)


def shipment_card_text(card: ShipmentCard) -> str:
    lines = [
        "📬 <b>Картка відправлення</b>",
        f"ТТН: <b>{card.ttn_number or 'ще не присвоєно'}</b>",
        f"Статус: <b>{_STATUS_LABELS[card.status]}</b>",
        f"Одержувач: {card.recipient_name}",
        f"Телефон: {card.recipient_phone or '—'}",
        f"Місто: {card.recipient_city or '—'}",
        f"Відділення: {card.recipient_warehouse or '—'}",
        f"Оплата: {card.payment_method or '—'} / {card.payer_type or '—'}",
        f"COD: {_money(card.cod_amount)} · Оціночна: {_money(card.insured_amount)}",
        f"Створено: {_fmt_dt(card.created_at)}",
        f"Оновлено: {_fmt_dt(card.status_changed_at)}",
        f"Відправлено: {_fmt_dt(card.dispatched_at) if card.dispatched_at else '—'}",
        f"SLA дедлайн: {_fmt_dt(card.sla_deadline) if card.sla_deadline else '—'}",
        "SLA: "
        + (
            "вчасно"
            if card.sla_met is True
            else "прострочено"
            if card.sla_met is False
            else "ще не зафіксовано"
        ),
        f"Комісія: {_money(card.fee_amount)}{' · безкоштовно' if card.fee_free else ''}",
        "",
        "<b>Позиції</b>",
    ]
    for item in card.items:
        category = f" · {item.category}" if item.category else ""
        lines.append(
            f"• <b>{item.sku}</b> — {item.name}{category} · "
            f"{item.quantity} шт · {_money(item.unit_price)}"
        )
    return "\n".join(lines)


def stats_text(snapshot: ClientStatsSnapshot) -> str:
    lines = [
        "📊 <b>Статистика</b>",
        f"Період: {_fmt_dt(snapshot.start)} — {_fmt_dt(snapshot.end)}",
        f"Відправлено: <b>{snapshot.shipped_qty}</b>",
        f"Повернення/відмови: <b>{snapshot.returns_qty}</b>",
        f"Втрати/пошкодження: <b>{snapshot.losses_qty}</b>",
        f"Чисті продажі: <b>{snapshot.net_sales_qty}</b>",
        f"Залишок на складі: <b>{snapshot.total_available}</b>",
    ]
    if snapshot.top_skus:
        lines.append("")
        lines.append("<b>Топ SKU</b>")
        for item in snapshot.top_skus:
            lines.append(f"• {item.sku} — {item.quantity}")
    return "\n".join(lines)


def settings_text(view: ClientSettingsView) -> str:
    notifications = "\n".join(
        f"• {item.label}: {'увімкнено' if item.enabled else 'вимкнено'}"
        for item in view.notifications
    )
    return (
        "⚙️ <b>Налаштування</b>\n"
        f"ПІБ: {view.full_name or '—'}\n"
        f"Телефон: {view.phone or '—'}\n"
        f"ФОП: {view.sender_profiles_count}"
        + (f" · основний: {view.default_sender_name}" if view.default_sender_name else "")
        + "\n\n<b>Сповіщення</b>\n"
        + notifications
    )


def product_search_prompt() -> str:
    return "Введіть артикул, назву або категорію для пошуку товарів."


def shipment_search_prompt() -> str:
    return "Введіть № ТТН або ім'я одержувача для пошуку відправлень."


def profile_edit_prompt(field: str) -> str:
    prompts = {
        "full_name": "Введіть новий ПІБ.",
        "phone": "Введіть новий номер телефону.",
        "name": "Введіть нову назву ФОП.",
        "sender_full_name": "Введіть ПІБ контактної особи.",
        "sender_phone": "Введіть номер телефону контактної особи.",
        "edrpou": "Введіть ЄДРПОУ або '-' щоб очистити поле.",
    }
    return prompts.get(field, "Введіть нове значення.")


def sender_profiles_text(profiles: list[SenderProfileView]) -> str:
    lines = ["🏢 <b>Мої ФОП</b>"]
    if not profiles:
        lines.append("Профілів поки немає. Натисніть «➕ Додати ФОП», щоб створити.")
    else:
        for profile in profiles:
            suffix = " · основний" if profile.is_default else ""
            lines.append(f"• <b>{profile.name}</b>{suffix}")
        lines.append("Можна додати ще один через «➕ Додати ФОП».")
    return "\n".join(lines)


def new_profile_name_prompt() -> str:
    return (
        "➕ <b>Новий ФОП</b>\n"
        "Введіть назву ФОП (як вам зручно розрізняти профілі, напр. «ФОП Іваненко»)."
    )


def new_profile_key_prompt() -> str:
    return (
        "Введіть <b>ключ API Нової Пошти</b> цього ФОП.\n"
        "Ключ перевіримо в НП і одразу приберемо повідомлення з чату."
    )


def new_profile_sender_name_prompt() -> str:
    return "Введіть <b>ПІБ контактної особи</b> відправника (як у кабінеті НП)."


def new_profile_phone_prompt() -> str:
    return "Введіть <b>телефон відправника</b> (0XXXXXXXXX або +380XXXXXXXXX)."


def new_profile_created_text(profile: SenderProfileView) -> str:
    suffix = " (основний)" if profile.is_default else ""
    return f"✅ ФОП «{profile.name}»{suffix} додано і перевірено в НП."


def new_profile_key_invalid_text() -> str:
    return (
        "❌ Ключ НП недійсний або у відправника не налаштований контрагент. "
        "Введіть правильний ключ ще раз."
    )


def new_profile_invalid_phone_text() -> str:
    return "❌ Невірний номер. Введіть у форматі 0XXXXXXXXX або +380XXXXXXXXX."


def new_profile_np_unavailable_text() -> str:
    return (
        "⚠️ НП тимчасово недоступна — не вдалося перевірити ключ. "
        "Надішліть номер телефону ще раз за хвилину."
    )


def sender_profile_text(profile: SenderProfileView) -> str:
    return "\n".join(
        [
            "🏢 <b>Профіль ФОП</b>",
            f"Назва: {profile.name}",
            f"Тип: {profile.org_type.value}",
            f"ЄДРПОУ: {profile.edrpou or '—'}",
            f"Контакт: {profile.sender_full_name or '—'}",
            f"Телефон: {profile.sender_phone or '—'}",
            f"Основний: {'так' if profile.is_default else 'ні'}",
            f"Ключ НП: {'є' if profile.has_api_key else 'немає'}",
        ]
    )
