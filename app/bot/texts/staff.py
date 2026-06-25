"""uk-тексты управления персоналом (👔, Фаза 6). parse_mode=HTML."""

from __future__ import annotations

import html

from app.db.models.enums import UserStatus
from app.services.staff import StaffCard, StaffPage

_STATUS_LABELS = {
    UserStatus.active: "активний",
    UserStatus.blocked: "заблокований",
    UserStatus.pending: "очікує",
    UserStatus.archived: "архів",
}


def _esc(value: str) -> str:
    return html.escape(value)


def list_text(page: StaffPage) -> str:
    lines = [f"👔 <b>Персонал</b> · {page.total}"]
    if page.query:
        lines.append(f"Пошук: <code>{_esc(page.query)}</code>")
    if page.total == 0:
        lines.append("Менеджерів немає. Додайте кнопкою «➕ Додати».")
    else:
        lines.append("Оберіть менеджера зі списку (🟢 — на звʼязку, 🚫 — заблокований).")
    return "\n".join(lines)


def card_text(card: StaffCard) -> str:
    name = _esc(card.full_name or "—")
    phone = _esc(card.phone or "—")
    duty = "на звʼязку" if card.on_duty else "не на звʼязку"
    lines = [
        f"👔 <b>{name}</b>",
        f"Телефон: {phone}",
        f"Telegram-ID: <code>{card.telegram_id}</code>",
        f"Статус: {_STATUS_LABELS.get(card.status, card.status.value)} · {duty}",
        "",
        "<b>Права:</b>",
    ]
    for flag in card.permissions:
        mark = "✅" if flag.enabled else "⬜"
        lines.append(f"{mark} {_esc(flag.label)} — {_esc(flag.description)}")
    lines.append("")
    lines.append("Натисніть право, щоб увімкнути/вимкнути.")
    return "\n".join(lines)


def delete_confirm_text(card: StaffCard) -> str:
    return (
        f"Видалити менеджера <b>{_esc(card.full_name or str(card.telegram_id))}</b>?\n"
        "Ми заблокуємо доступ, знімемо роль менеджера, а відкриті звернення повернуться в чергу."
    )


def add_prompt_text() -> str:
    return (
        "Введіть <b>Telegram-ID</b> або <b>номер телефону</b> нового менеджера.\n"
        "За телефоном можна додати лише того, хто вже користувався ботом."
    )


def added_text(card: StaffCard) -> str:
    name = _esc(card.full_name or str(card.telegram_id))
    return f"✅ {name} тепер менеджер. Усі права увімкнені за замовчуванням."


def search_prompt_text() -> str:
    return "Введіть імʼя або телефон менеджера для пошуку."


def not_owner_text() -> str:
    return "Розділ доступний лише власнику."


def invalid_add_input_text() -> str:
    return "Не схоже на Telegram-ID чи телефон. Спробуйте ще раз або поверніться в меню."
