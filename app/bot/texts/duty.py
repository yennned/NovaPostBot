"""uk-тексты дежурства менеджера (Фаза 6)."""

from __future__ import annotations

from app.services.duty import DutyResult
from app.services.exceptions import OfficeClosed
from app.utils.timefmt import to_local


def on_duty_text(result: DutyResult) -> str:
    return (
        "🟢 Зміну відкрито — ви на звʼязку.\n"
        f"Працюєте до {to_local(result.window_end):%H:%M}. "
        "Звернення клієнтів надходитимуть вам.\n"
        "Зміна закриється автоматично після закриття відділення — вимикати не треба."
    )


def office_closed_text(exc: OfficeClosed) -> str:
    if exc.next_open is not None:
        return (
            "Відділення зараз зачинене, зміну можна відкрити лише в робочі години.\n"
            f"Найближче відкриття — {to_local(exc.next_open):%d.%m о %H:%M}."
        )
    return "Відділення зараз зачинене — зміну можна відкрити лише в робочі години."


def not_staff_text() -> str:
    return "Ця дія доступна лише персоналу."


def duty_unavailable_text() -> str:
    return "Чергування недоступне для вашого акаунта."
