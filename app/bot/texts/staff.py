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
        f"Telegram-ID: <code>{card.telegram_id or '—'}</code>",
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
    """Подтверждение удаления менеджера.

    Текст обещал «заблокуємо доступ, знімемо роль» — это описывало старый демоушен.
    Теперь удаление физическое и безвозвратное, и предупредить об этом обязаны до
    нажатия, а не после.
    """
    return (
        f"🗑 Видалити менеджера <b>{_esc(_card_label(card))}</b>?\n\n"
        "Дію <b>не можна скасувати</b>. Обліковий запис буде видалено назавжди, "
        "номер і Telegram звільняться.\n"
        "Відкриті звернення повернуться в чергу, історія (ТТН, склад, листування) "
        "збережеться без імені автора.\n"
        "Повторний найм створить <b>нового</b> користувача — старі права не повернуться.\n\n"
        "Щоб лише тимчасово закрити доступ, скасуйте і натисніть «🚫 Заблокувати»."
    )


def add_prompt_text() -> str:
    return (
        "Введіть <b>номер телефону</b> (0…, 380…, +380…) або <b>Telegram-ID</b> "
        "нового менеджера.\n"
        "За телефоном можна додати навіть того, хто ще не користувався ботом — "
        "він стане менеджером одразу після першого входу за цим номером."
    )


def _card_label(card: StaffCard) -> str:
    """Человекочитаемая метка менеджера: ПІБ → телефон → Telegram-ID."""
    return card.full_name or card.phone or str(card.telegram_id or "—")


def added_text(card: StaffCard) -> str:
    name = _esc(_card_label(card))
    if card.telegram_id is None:
        return (
            f"✅ {name} доданий менеджером. Він активується автоматично після "
            "першого входу в бота за цим номером. Усі права увімкнені."
        )
    return f"✅ {name} тепер менеджер. Усі права увімкнені за замовчуванням."


def search_prompt_text() -> str:
    return "Введіть імʼя або телефон менеджера для пошуку."


def not_owner_text() -> str:
    return "Розділ доступний лише власнику."


def invalid_add_input_text() -> str:
    return "Не схоже на телефон чи Telegram-ID. Спробуйте ще раз або поверніться в меню."
