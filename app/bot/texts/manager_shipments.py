"""Тексты manager shipment queue."""

from __future__ import annotations

from datetime import datetime

from app.bot.texts.client_cabinet import shipment_card_text
from app.services.manager_shipments import ManagerShipmentCard, ManagerShipmentPage
from app.utils.timefmt import fmt_dt


def queue_text(page: ManagerShipmentPage) -> str:
    title = {
        "created": "Створені",
        "confirmed": "Підтверджені",
        "returns": "Повернення",
    }.get(page.bucket, "Відправлення")
    lines = [f"📬 <b>{title}</b> · {page.total}"]
    if page.query:
        lines.append(f"Пошук: <code>{page.query}</code>")
    if not page.items:
        lines.append("Відправлень у цій групі поки немає.")
    else:
        for item in page.items:
            deadline = _fmt(item.sla_deadline) if item.sla_deadline else "—"
            lines.append(
                f"• <b>{item.ttn_number or 'без ТТН'}</b> — {item.client_name or '—'} / "
                f"{item.recipient_name}\n  SLA: {item.sla_state} · дедлайн {deadline}"
            )
    return "\n".join(lines)


def card_text(card: ManagerShipmentCard) -> str:
    lines = [
        f"👤 Клієнт: <b>{card.client_name or 'без імені'}</b>",
        f"🏢 ФОП: {card.sender_profile_name or '—'}",
        "",
        shipment_card_text(card.shipment),
    ]
    if card.can_confirm:
        lines.append("\nСтатус очікує підтвердження менеджером.")
    if card.can_receive_return:
        lines.append("\nПовернення можна оглянути та прийняти на склад через бот.")
    if card.can_mark_lost or card.can_mark_damaged:
        lines.append("\nДоступні ручні override-и для нестандартної ситуації.")
    return "\n".join(lines)


def return_inspection_text(card: ManagerShipmentCard, decisions: dict[str, bool]) -> str:
    lines = [
        "🔄 <b>Огляд повернення</b>",
        f"Клієнт: <b>{card.client_name or 'без імені'}</b>",
        f"ТТН: <code>{card.shipment.ttn_number or '—'}</code>",
        "",
    ]
    for item in card.shipment.items:
        accepted = decisions.get(item.sku, True)
        label = "На склад" if accepted else "Брак"
        lines.append(f"• <b>{item.sku}</b> — {item.name} ×{item.quantity}: <b>{label}</b>")
    lines.append("")
    lines.append("Натисніть на позицію, щоб перемкнути її між складом і браком.")
    return "\n".join(lines)


def search_prompt_text() -> str:
    return "Введіть № ТТН, ПІБ клієнта, телефон або одержувача для пошуку."


def action_done_text(action: str) -> str:
    labels = {
        "confirm": "✅ Відправлення підтверджено.",
        "cancel": "✅ Відправлення скасовано.",
        "return": "✅ Повернення прийнято на склад.",
        "lost": "✅ Відправлення позначено як втрачене.",
        "damaged": "✅ Відправлення позначено як пошкоджене.",
    }
    return labels.get(action, "✅ Готово.")


def _fmt(value: datetime) -> str:
    return fmt_dt(value)
