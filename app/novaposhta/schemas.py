"""Структуры запросов/ответов НП.

В PR1 (транспортное ядро) — только конверт ответа НП. Доменные схемы
(черновик ТТН, город, відділення, цена) добавятся вместе с `methods.py`/
`mapping.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class NPEnvelope:
    """Разобранный конверт ответа НП.

    НП всегда отвечает HTTP 200 с телом
    `{success, data, errors, errorCodes, warnings, ...}`. Бизнес-сбой выражен
    `success=false`, а не HTTP-кодом.
    """

    success: bool
    data: list[dict[str, Any]]
    errors: list[str]
    error_codes: list[str]
    warnings: list[str]

    @classmethod
    def from_payload(cls, payload: Any) -> NPEnvelope:
        """Построить конверт из распарсенного JSON-тела (терпимо к форме)."""
        if not isinstance(payload, dict):
            return cls(
                success=False,
                data=[],
                errors=["неожиданный формат відповіді НП"],
                error_codes=[],
                warnings=[],
            )
        raw_data = payload.get("data")
        data = (
            [row for row in raw_data if isinstance(row, dict)] if isinstance(raw_data, list) else []
        )
        return cls(
            success=bool(payload.get("success")),
            data=data,
            errors=_as_str_list(payload.get("errors")),
            error_codes=_as_str_list(payload.get("errorCodes")),
            warnings=_as_str_list(payload.get("warnings")),
        )


def _as_str_list(value: Any) -> list[str]:
    """Привести произвольное поле НП (`errors`/`errorCodes`/...) к списку строк.

    Терпимо к форме: список → строки поэлементно; dict (НП иногда отдаёт
    объект-карту) → его значения; пусто/None → []; иначе одна строка.
    """
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, dict):
        return [str(item) for item in value.values()]
    if value in (None, ""):
        return []
    return [str(value)]
