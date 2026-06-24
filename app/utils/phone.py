"""Нормализация украинских номеров телефона в формат НП (`380XXXXXXXXX`).

Единая точка валидации: используется и при создании ТТН (получатель), и при
само-редактировании телефона в кабинете клиента, чтобы форматы не разъезжались.
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
