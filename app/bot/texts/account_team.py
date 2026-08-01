"""Тексты экрана «👥 Команда» (работники клиентского акаунта).

Единственный модуль фичи, который формирует UI-текст: сервисы этого не делают
(см. докстринг `app/services/exceptions.py`). Здесь же — экранирование: `full_name`
приходит из Telegram, то есть управляется пользователем, и в HTML сырым не идёт.
"""

from __future__ import annotations

import html

from app.db.models.enums import MembershipStatus

# Метки статуса членства. Раньше их знала только клавиатура, а карточка печатала
# `status.value` — список говорил «активний», карточка про того же работника —
# «active». Один источник.
_STATUS_LABELS = {
    MembershipStatus.invited: "очікує",
    MembershipStatus.active: "активний",
    MembershipStatus.blocked: "заблокований",
}


def _esc(value: str) -> str:
    return html.escape(value)


def status_label(status: MembershipStatus) -> str:
    return _STATUS_LABELS.get(status, status.value)


def member_label(item) -> str:
    """Имя работника для кнопки/заголовка: ПІБ → телефон → id. Без экранирования."""
    return item.full_name or item.phone or str(item.user_id)


def team_list_text(total: int) -> str:
    return f"👥 <b>Команда</b> · {total}\nОберіть працівника або запросіть нового."


def invite_result_text(item) -> str:
    """Итог приглашения. Развилка по состоянию, а не один текст на все случаи.

    Номер, уже присылавший контакт боту, вступает сразу `active` — требовать с
    него ещё один контакт было бы неправдой (см. `account_team._joining_status`).
    """
    if item.status is MembershipStatus.active:
        return f"✅ {item.phone} додано до команди."
    return f"✅ Запрошення створено для {item.phone}. Працівник має надіслати власний контакт боту."


def member_delete_confirm_text(item) -> str:
    """Подтверждение удаления работника: сказать, что необратимо, ДО нажатия."""
    return (
        f"🗑 Видалити працівника <b>{_esc(member_label(item))}</b>?\n\n"
        "Дію <b>не можна скасувати</b>. Обліковий запис буде видалено назавжди, "
        "номер звільниться.\n"
        "ТТН і склад вашого акаунта залишаться — зникне лише ім'я автора.\n"
        "Щоб повернути людину, доведеться запросити її знову як нового працівника.\n\n"
        "Якщо треба лише тимчасово закрити доступ — скасуйте і натисніть "
        "«⛔ Заблокувати»."
    )


def member_deleted_text(item) -> str:
    return f"🗑 {_esc(member_label(item))} — видалено з команди."


def member_card_text(item, *, with_phone: bool = True) -> str:
    lines = [f"👤 <b>{_esc(member_label(item))}</b>"]
    if with_phone:
        lines.append(f"Телефон: {_esc(item.phone or '—')}")
    lines.append(f"Стан: {status_label(item.status)}")
    return "\n".join(lines)
