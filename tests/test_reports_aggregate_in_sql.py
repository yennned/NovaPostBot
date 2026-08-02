"""Отчёты считает Postgres, а не Python.

Проверяется не «числа сошлись» — они сходились и у прежней реализации, — а сам
механизм: сколько строк отчёт втянул в память процесса. Прежняя форма грузила
весь период через `joinedload(client, items)` и складывала `Counter`-ом; на
15 000 ТТН/мес это 15 000 отправлений × ~3 позиции через декартово произведение
на каждый тап кнопки периода. Тест на числах такую регрессию не заметит вовсе,
поэтому считаем загруженные объекты.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.db.models.enums import ShipmentStatus, UserRole, UserStatus
from app.db.models.shipment import Shipment
from app.db.repositories import ShipmentItemDraft, ShipmentRepository, UserRepository
from app.services import reports
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

_SHIPMENTS = 30
_KYIV = ZoneInfo("Europe/Kyiv")


def _inside_todays_kyiv_day() -> datetime:
    """Момент внутри СЕГОДНЯШНЕГО киевского дня, а не «сейчас минус минуты».

    Границы периода отчёт считает в киевском дне (`_bounds`), а сид ставил
    `now(UTC) - i минут`. С 21:00 до 24:00 UTC киевские сутки уже сменились, и
    часть ТТН уезжала во «вчера»: тест падал три часа в сутки, причём на
    произвольном PR — он ловил не свою регрессию, а часовой пояс. Полдень по
    Киеву гарантированно внутри дня при любом времени прогона.
    """
    today_kyiv = datetime.now(_KYIV).date()
    return datetime.combine(today_kyiv, time(12, 0), tzinfo=_KYIV).astimezone(UTC)


@contextmanager
def count_loaded_shipments(session: AsyncSession):
    """Считает `Shipment`, превращённые в ORM-объекты за время блока.

    Именно событие загрузки, а не размер identity map: карта держит **слабые**
    ссылки, и строки, из которых отчёт собрал свои DTO, успевают исчезнуть до
    проверки — счётчик по карте показывал бы ноль независимо от того, грузил
    отчёт период целиком или не грузил вовсе.
    """
    loaded: list[Shipment] = []

    def _track(_session, instance) -> None:
        if isinstance(instance, Shipment):
            loaded.append(instance)

    sync_session = session.sync_session
    event.listen(sync_session, "loaded_as_persistent", _track)
    try:
        yield loaded
    finally:
        event.remove(sync_session, "loaded_as_persistent", _track)


async def _seed(session: AsyncSession, *, telegram_id: int, late: int = 0) -> None:
    owner = await UserRepository(session).create(
        telegram_id=telegram_id, role=UserRole.owner, status=UserStatus.active
    )
    client = await UserRepository(session).create(
        telegram_id=telegram_id + 1,
        full_name="Клієнт",
        role=UserRole.client,
        status=UserStatus.active,
    )
    repo = ShipmentRepository(session)
    now = _inside_todays_kyiv_day()
    for i in range(_SHIPMENTS):
        shipment = await repo.create(
            client_id=client.id,
            recipient_name=f"Отримувач {i}",
            ttn_number=f"5900{i:04d}",
            status=ShipmentStatus.dispatched,
            status_changed_at=now - timedelta(minutes=i),
            items=[ShipmentItemDraft(sku="A1", name="Товар", quantity=2)],
        )
        shipment.dispatched_at = now - timedelta(minutes=i)
        shipment.sla_met = i >= late
    await session.flush()
    session.expunge_all()
    return owner


async def test_period_report_loads_no_shipment_rows(db_session: AsyncSession):
    """Сводка по периоду не тянет в память ни одной строки отправлений.

    Ей нужны три числа и разбивка по клиентам — всё это отдаёт `GROUP BY`.
    Загруженный `Shipment` здесь означает возврат к выгрузке периода целиком.
    """
    owner = await _seed(db_session, telegram_id=3000)

    with count_loaded_shipments(db_session) as loaded:
        report = await reports.period_report(db_session, actor=owner, period="today")

    assert report.shipped == _SHIPMENTS * 2, "агрегат обязан совпадать с прежним счётом"
    assert len(loaded) == 0, (
        f"отчёт втянул {len(loaded)} строк отправлений: на 15k ТТН/мес это десятки "
        "тысяч объектов ORM ради трёх чисел на экране"
    )


async def test_financial_report_loads_only_late_rows(db_session: AsyncSession):
    """Финотчёт грузит построчно ровно то, что построчно и показывает.

    Суммы и счётчики — агрегаты; список опоздавших действительно нужен строками,
    но их двое из тридцати, и растёт он по числу промахов SLA, а не по обороту.
    """
    owner = await _seed(db_session, telegram_id=3100, late=2)

    with count_loaded_shipments(db_session) as loaded:
        fin = await reports.financial_report(db_session, actor=owner, period="today")

    assert fin.dispatched_count == _SHIPMENTS
    assert len(fin.late) == 2
    assert len(loaded) == 2, (
        f"загружено {len(loaded)} строк вместо двух опоздавших — "
        "финотчёт снова выгружает весь период"
    )


async def test_period_is_bounded_by_dispatch_time_not_status_change(db_session: AsyncSession):
    """Период отчёта режется по `dispatched_at`, а не по `status_changed_at`.

    Возврат меняет статус сегодня, но уехала посылка в прошлом месяце — в
    «відправлено» за сегодня она попадать не должна. Прежний legacy-фолбэк
    `OR (dispatched_at IS NULL AND status IN (…) AND status_changed_at …)` ровно
    это и делал для строк без времени отправки.
    """
    owner = await UserRepository(db_session).create(
        telegram_id=3200, role=UserRole.owner, status=UserStatus.active
    )
    client = await UserRepository(db_session).create(
        telegram_id=3201, full_name="Клієнт", role=UserRole.client, status=UserStatus.active
    )
    now = datetime.now(UTC)
    shipment = await ShipmentRepository(db_session).create(
        client_id=client.id,
        recipient_name="Отримувач",
        status=ShipmentStatus.returned,
        status_changed_at=now,
        items=[ShipmentItemDraft(sku="A1", name="Товар", quantity=7)],
    )
    shipment.dispatched_at = now - timedelta(days=40)
    await db_session.flush()

    report = await reports.period_report(db_session, actor=owner, period="today")

    assert report.shipped == 0, "уехавшее сорок дней назад не «відправлено сьогодні»"
    assert report.returns == 7, "а вот возврат случился сегодня и в период попадает"
