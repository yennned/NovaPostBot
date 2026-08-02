"""Нормализация статусов трекинга НП в доменные статусы отправлений."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

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


#: Поле ответа НП, несущее время сканирования, которым закрывается SLA.
#:
#: Берём ТОЛЬКО его. Соседние даты в том же ответе брать нельзя, и это не
#: осторожность, а разница в знаке ошибки: `DateCreated` — момент создания ТТН, то
#: есть СТАРТ отсчёта SLA. Подставив её как время отправки, мы бы получали
#: «отправлено в момент создания» и признавали успевшими все накладные подряд,
#: включая реально просроченные. `RecipientDateTime`/`ActualDeliveryDate` — про
#: вручение, они позже отправки и завышали бы промахи.
_SCAN_TIME_FIELD = "DateScan"

#: Формат снят живым пробником (`scripts/e2e/tracking_probe.py`), а не из документации.
#:
#: Боевой ответ отдаёт `DateScan` как `'20:05 01.08.2026'` — время впереди даты и
#: БЕЗ секунд. Причём соседние поля в том же ответе написаны иначе:
#: `DateCreated` = `'01-08-2026 20:05:33'`, `ScheduledDeliveryDate` =
#: `'04-08-2026 09:00:00'`. Единого формата дат у НП нет, и угадать его было
#: нельзя: первая версия этого кода знала только два «разумных» написания и не
#: разобрала бы ни одного реального ответа — вердикт SLA молча выродился бы в
#: «не знаем» на всех накладных сразу.
#:
#: Остальные написания оставлены запасом на случай смены формата: лишний
#: `strptime` дешевле, чем повторение той же ошибки.
_SCAN_TIME_FORMATS = (
    "%H:%M %d.%m.%Y",  # фактический формат DateScan (проверено боем 2026-08-02)
    "%d.%m.%Y %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)

#: Время в ответах НП — местное для Украины.
_NP_TZ = ZoneInfo("Europe/Kyiv")


def dispatch_scan_time(status: TrackingStatus) -> datetime | None:
    """Время сканирования из ответа НП (UTC) или `None`, если его там нет.

    `None` — законный и ожидаемый исход: он означает «НП не сказала, когда»,
    а не «прозевали срок». Ответственность за то, чтобы неизвестность не
    превратилась в промах SLA, лежит на `app/utils/sla.sla_verdict`.
    """
    raw_value = status.raw.get(_SCAN_TIME_FIELD)
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    text = raw_value.strip()
    for fmt in _SCAN_TIME_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=_NP_TZ).astimezone(UTC)
    return None


def map_tracking_status(status: TrackingStatus) -> ShipmentStatus | None:
    code = status.status_code.strip()
    if code in _STATUS_CODES:
        return _STATUS_CODES[code]

    haystack = f"{status.status} {status.raw}".lower()
    for needles, result in _STATUS_HINTS:
        if any(needle in haystack for needle in needles):
            return result
    return None
