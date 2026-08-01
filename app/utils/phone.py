"""Нормализация украинских номеров телефона в формат НП (`380XXXXXXXXX`).

Единая точка валидации для всех телефонов, чтобы форматы не разъезжались:
телефон получателя ТТН, `sender_phone` профиля ФОП (уходит в НП как
`SendersPhone`), само-редактирование телефона в кабинете, правка клиента
менеджером, наём персонала и приглашение работника — плюс приведение контакта
из `request_contact` при авторизации.

Хранить телефон нормализованным критично в двух местах: `users.phone` — UNIQUE
и сверяется точным равенством при адопции по номеру, а `sender_phone` уходит в
НП, которая на мусор отвечает своим текстом вместо понятной ошибки бота.

Инвариант нормализованного `users.phone` держится только для украинских мобильных:
`register_contact` не блокирует вход при ненормализуемом контакте (он приходит от
Telegram и настоящий), а сохраняет его как есть и пишет warning. Для `sender_phone`
исключений нет — сервис ФОП отбивает всё, что не проходит `normalize_phone`.
"""

from __future__ import annotations

import re

_NON_DIGITS = re.compile(r"\D")


def normalize_phone(raw: str) -> str | None:
    """`0XXXXXXXXX` / `380XXXXXXXXX` / `+380XXXXXXXXX` → `380XXXXXXXXX`.

    Возвращает `None`, если строка не похожа на украинский мобильный номер.
    """
    digits = _NON_DIGITS.sub("", raw or "")
    if len(digits) == 10 and digits.startswith("0"):
        digits = "38" + digits
    if len(digits) == 12 and digits.startswith("380"):
        return digits
    return None
