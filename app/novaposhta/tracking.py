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

#: Код 2 у НП — «Видалено», а не «створено». Раньше он вёл в `confirmed`, и
#: накладная, удалённая в кабинете НП (клиентом или нашей же отменой, чья
#: транзакция не доехала), навсегда оставалась «підтверджена»: висела в очереди
#: менеджера и держала резерв склада под посылку, которой уже нет.
_DELETED_STATUS_CODE = "2"

_STATUS_CODES: dict[str, ShipmentStatus] = {
    "1": ShipmentStatus.confirmed,
    _DELETED_STATUS_CODE: ShipmentStatus.cancelled,
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


def is_deleted_in_np(status: TrackingStatus) -> bool:
    """Документ удалён в НП (`StatusCode=2`, «Видалено»).

    Нужно отмене: НП на удаление уже удалённого документа отвечает не «не
    знайдено», а `Error getting payment info …; No document changed DeletionMark`.
    Классифицировать это по тексту ошибки нельзя — под ту же формулировку попадёт
    и «удалить нельзя», а тогда мы пометили бы отменённой живую накладную, сняли
    резерв, и посылка всё равно уехала бы. Поэтому спрашиваем НП про статус.
    """
    return status.status_code.strip() == _DELETED_STATUS_CODE


def map_tracking_status(status: TrackingStatus) -> ShipmentStatus | None:
    code = status.status_code.strip()
    if code in _STATUS_CODES:
        return _STATUS_CODES[code]

    haystack = f"{status.status} {status.raw}".lower()
    for needles, result in _STATUS_HINTS:
        if any(needle in haystack for needle in needles):
            return result
    return None
