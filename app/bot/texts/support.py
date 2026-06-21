"""uk-тексты поддержки (Фаза 6). HTML parse_mode → пользовательский текст экранируем."""

from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.models.support import SupportThread
from app.services.support import DutyContact

_SENDER_LABELS = {"client": "Клієнт", "manager": "Менеджер", "owner": "Менеджер", "dev": "Менеджер"}
_MAX_MESSAGES = 15


def _esc(value: str) -> str:
    return html.escape(value)


def _local(value: datetime) -> datetime:
    return value.astimezone(ZoneInfo(get_settings().timezone))


def _window_str(contact: DutyContact) -> str | None:
    if contact.window is None:
        return None
    start, end = contact.window
    return f"{start:%H:%M}–{end:%H:%M}"


def _client_label(thread: SupportThread) -> str:
    client = thread.client
    name = (client.full_name if client else None) or "Клієнт"
    phone = (client.phone if client else None) or "—"
    return f"{_esc(name)} ({_esc(phone)})"


def duty_card_text(contact: DutyContact) -> str:
    window = _window_str(contact)
    if contact.office_open and contact.manager is not None:
        name = _esc(contact.manager.full_name or "Менеджер")
        phone = _esc(contact.manager.phone or "—")
        lines = [
            "💬 <b>Звернення до менеджера</b>",
            f"Черговий: {name}",
            f"Телефон: {phone}",
        ]
        if window:
            lines.append(f"Відділення працює сьогодні: {window}")
        lines.append("Натисніть «Почати чат», щоб написати.")
        return "\n".join(lines)

    if not contact.office_open:
        lines = ["Відділення зараз не працює."]
        lines.append(f"Графік сьогодні: {window}." if window else "Сьогодні вихідний.")
        lines.append("Залиште звернення — відповімо в робочий час.")
        return "\n".join(lines)

    return "Зараз немає чергового менеджера.\nЗалиште звернення — ним займуться найближчим часом."


def client_resume_text() -> str:
    return "У вас вже є відкрите звернення. Напишіть повідомлення — воно піде менеджеру."


def chat_exited_text() -> str:
    return "Чат завершено. Звернення лишається відкритим, поки менеджер його не закриє."


def queued_ack_text() -> str:
    return "Повідомлення збережено. Менеджер відповість, щойно буде на звʼязку."


def conversation_text(thread: SupportThread) -> str:
    status = {"open": "відкрите", "waiting": "у черзі", "closed": "закрите"}.get(
        thread.status.value, thread.status.value
    )
    lines = [f"💬 <b>Звернення</b> · {_client_label(thread)} · {status}"]
    messages = thread.messages[-_MAX_MESSAGES:]
    if not messages:
        lines.append("Повідомлень ще немає.")
    for msg in messages:
        who = _SENDER_LABELS.get(msg.sender_role, msg.sender_role)
        lines.append(f"<b>{_local(msg.created_at):%d.%m %H:%M} · {who}:</b> {_esc(msg.text)}")
    return "\n".join(lines)


def inbox_text(total: int, *, scope: str, query: str | None = None) -> str:
    lines = [f"💬 <b>Підтримка</b> · {scope} · {total}"]
    if query:
        lines.append(f"Пошук: <code>{_esc(query)}</code>")
    if total == 0:
        lines.append("Звернень немає.")
    else:
        lines.append("Оберіть звернення зі списку.")
    return "\n".join(lines)


def reply_prompt_text() -> str:
    return "Введіть відповідь клієнту одним повідомленням."


def reply_sent_text() -> str:
    return "✅ Відповідь надіслано."


def reply_exited_text() -> str:
    return "Режим відповіді завершено."


def thread_closed_text() -> str:
    return "✅ Звернення закрито."


def search_prompt_text() -> str:
    return "Введіть імʼя, телефон клієнта або дату (ДД.ММ.РРРР) для пошуку."


def thread_unavailable_text() -> str:
    return "Звернення недоступне або вже закрите."
