"""Тесты нормализации телефона (`app/utils/phone`) — единый формат НП `380XXXXXXXXX`."""

from __future__ import annotations

import pytest
from app.utils.phone import normalize_phone


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0671112233", "380671112233"),
        ("380671112233", "380671112233"),
        ("+380671112233", "380671112233"),
        ("(067) 111-22-33", "380671112233"),
        ("  +38 (067) 111 22 33 ", "380671112233"),
        ("Тест ФОП", None),  # ← баг, который чиним: текст вместо номера
        ("", None),
        ("-", None),
        ("123", None),
        ("06711122", None),  # слишком короткий
        ("0971112233444", None),  # слишком длинный
    ],
)
def test_normalize_phone(raw: str, expected: str | None) -> None:
    assert normalize_phone(raw) == expected
