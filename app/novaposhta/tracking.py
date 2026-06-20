"""Нормализация статусов трекинга НП в доменные статусы отправлений."""

from __future__ import annotations

from app.db.models.enums import ShipmentStatus
from app.novaposhta.schemas import TrackingStatus

_STATUS_HINTS: list[tuple[tuple[str, ...], ShipmentStatus]] = [
    (("вруч", "отриман"), ShipmentStatus.delivered),
    (("повернен",), ShipmentStatus.returned),
    (("відмов", "поверта", "зворот"), ShipmentStatus.returning),
    (("пошкод",), ShipmentStatus.damaged),
    (("втрачен", "загуб"), ShipmentStatus.lost),
    (("прибул", "на відділенні", "у відділенні"), ShipmentStatus.arrived),
    (("дороз", "транзит", "переміщ"), ShipmentStatus.in_transit),
    (("відправ", "передано до перевезення"), ShipmentStatus.dispatched),
    (("створен", "зареєстрован"), ShipmentStatus.confirmed),
]

_STATUS_CODES: dict[str, ShipmentStatus] = {
    "1": ShipmentStatus.confirmed,
    "2": ShipmentStatus.confirmed,
    "3": ShipmentStatus.dispatched,
    "4": ShipmentStatus.in_transit,
    "5": ShipmentStatus.arrived,
    "7": ShipmentStatus.arrived,
    "8": ShipmentStatus.delivered,
    "9": ShipmentStatus.returning,
    "10": ShipmentStatus.returned,
    "11": ShipmentStatus.returned,
    "12": ShipmentStatus.lost,
    "13": ShipmentStatus.damaged,
}


def map_tracking_status(status: TrackingStatus) -> ShipmentStatus | None:
    code = status.status_code.strip()
    if code in _STATUS_CODES:
        return _STATUS_CODES[code]

    haystack = f"{status.status} {status.raw}".lower()
    for needles, result in _STATUS_HINTS:
        if any(needle in haystack for needle in needles):
            return result
    return None
