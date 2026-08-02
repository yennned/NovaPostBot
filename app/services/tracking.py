"""Трекинг НП, SLA-флаги и списание складских остатков."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.enums import ShipmentStatus, StockMovementType
from app.db.models.shipment import Shipment
from app.db.repositories import AuditRepository, ShipmentRepository, StockMovementRepository
from app.logging_config import get_logger
from app.novaposhta import methods
from app.novaposhta.client import NovaPoshtaClient
from app.novaposhta.schemas import TrackingStatus
from app.novaposhta.tracking import dispatch_scan_time, map_tracking_status
from app.services import notifications
from app.services.client_sheet_sync import best_effort_sync
from app.services.inventory_backend import build_inventory_backend
from app.services.notifications import Notifier
from app.services.stock_write import apply_physical_movement, items_from_shipment
from app.sheets import StockSource
from app.utils.sla import sla_verdict

_log = get_logger("tracking")

NONSTANDARD_STATUSES = {
    ShipmentStatus.returning,
    ShipmentStatus.returned,
    ShipmentStatus.lost,
    ShipmentStatus.damaged,
}

#: Статусы, означающие «посылка уже уехала». Отправка — это переход в ЛЮБОЙ из
#: них, а не только в `dispatched`: при перерыве в опросе НП отдаёт сразу более
#: поздний статус, и промежуточный мы не видим никогда.
POST_DISPATCH_STATUSES = {
    ShipmentStatus.dispatched,
    ShipmentStatus.in_transit,
    ShipmentStatus.arrived,
    ShipmentStatus.delivered,
}

# Потолок конкурентных НП-чтений статусов за один поллинг (по одному httpx-клиенту).
_POLL_FETCH_CONCURRENCY = 8


@dataclass(frozen=True, slots=True)
class TrackingPollResult:
    checked: int
    updated: int
    notified: int


async def _poll_batch(
    session: AsyncSession,
    *,
    shipments: list[Shipment],
    np_client: NovaPoshtaClient,
    notifier: Notifier | None,
    mutator: StockSource | None,
) -> TrackingPollResult:
    """Спросить НП про переданные ТТН и применить ответы.

    Общее ядро горячего трекинга и позднего прохода возвратов: они отличаются
    только выборкой, а работа с НП и запись статусов у них одна и та же.
    """
    if not shipments:
        return TrackingPollResult(checked=0, updated=0, notified=0)

    by_api_key: dict[str, list[Shipment]] = defaultdict(list)
    for shipment in shipments:
        if shipment.sender_profile is None or not shipment.ttn_number:
            continue
        by_api_key[shipment.sender_profile.np_api_key].append(shipment)

    # Чанкуем ОТПРАВЛЕНИЯ, а не номера. Раньше чанк номеров нёс с собой весь список
    # ФОП, и фаза записи обходила его заново на каждый чанк — O(чанки × партия) при
    # >100 ТТН у одного ФОП. Главное же: по чанку номеров нельзя отличить «НП не
    # вернула документ» от «этот документ в этом чанке не спрашивали», а без такого
    # различения нельзя честно проставить `tracking_updated_at`.
    fetch_specs: list[tuple[str, list[Shipment]]] = []
    for api_key, batch in by_api_key.items():
        for chunk in _chunked(batch, size=100):
            fetch_specs.append((api_key, chunk))
    if not fetch_specs:
        return TrackingPollResult(checked=0, updated=0, notified=0)

    # Фаза чтения — независимые НП-вызовы конкурентно (общий httpx.AsyncClient
    # потокобезопасен), но с ограничителем, чтобы не завалить API при многих ФОП.
    # TaskGroup: при сбое одного чтения остальные отменяются структурно (без «висящих»
    # задач и «Task exception was never retrieved»).
    sem = asyncio.Semaphore(_POLL_FETCH_CONCURRENCY)

    async def _fetch(api_key: str, chunk: list[Shipment]) -> list[TrackingStatus]:
        async with sem:
            numbers = [shipment.ttn_number for shipment in chunk if shipment.ttn_number]
            return await methods.get_status_documents(np_client, api_key=api_key, numbers=numbers)

    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(_fetch(api_key, chunk)) for api_key, chunk in fetch_specs]
    fetched = [task.result() for task in tasks]

    # Фаза записи — последовательно на общей `AsyncSession` (не потокобезопасна).
    checked = 0
    updated = 0
    notified = 0
    unanswered = 0
    now = datetime.now(UTC)
    for (_api_key, chunk), rows in zip(fetch_specs, fetched, strict=True):
        checked += len(chunk)
        by_number = {row.number: row for row in rows}
        for shipment in chunk:
            tracking = by_number.get(shipment.ttn_number or "")
            if tracking is None:
                # НП спросили, но строку по документу она не вернула. Отметку времени
                # всё равно ставим: без неё документ навсегда остаётся «самым давно не
                # опрошенным», вечно занимает начало выборки и вытесняет из лимита
                # всех остальных.
                shipment.tracking_updated_at = now
                unanswered += 1
                continue
            changed, pushed = await apply_tracking_status(
                session,
                shipment=shipment,
                tracking=tracking,
                notifier=notifier,
                mutator=mutator,
            )
            updated += int(changed)
            notified += int(pushed)
    if unanswered:
        await session.flush()
        _log.info("tracking.unanswered", count=unanswered)
    return TrackingPollResult(checked=checked, updated=updated, notified=notified)


async def poll_shipments(
    session: AsyncSession,
    *,
    np_client: NovaPoshtaClient,
    notifier: Notifier | None = None,
    mutator: StockSource | None = None,
    settings: Settings | None = None,
) -> TrackingPollResult:
    """Горячий трекинг: довести ТТН до `dispatched`.

    Дальше путь посылки клиент смотрит в приложении НП — нам после отправки нужен
    только факт возврата, и его ловит отдельный редкий проход (`poll_returns`).
    """
    settings = settings or get_settings()
    repo = ShipmentRepository(session)
    stale_before = datetime.now(UTC) - timedelta(days=settings.tracking_stale_days)
    total, never, oldest = await repo.tracking_backlog(stale_before=stale_before)
    shipments = await repo.list_for_tracking(
        limit=settings.tracking_batch_limit,
        stale_before=stale_before,
    )
    # Размер очереди в логе — единственное, по чему видно «трекинг перестал успевать».
    # Если `backlog` упирается в `limit`, часть ТТН не опрашивается в этот проход.
    _log.info(
        "tracking.poll",
        backlog=total,
        never_polled=never,
        oldest_polled_at=oldest.isoformat() if oldest else None,
        batch=len(shipments),
        limit=settings.tracking_batch_limit,
    )
    return await _poll_batch(
        session,
        shipments=shipments,
        np_client=np_client,
        notifier=notifier,
        mutator=mutator,
    )


async def poll_returns(
    session: AsyncSession,
    *,
    np_client: NovaPoshtaClient,
    notifier: Notifier | None = None,
    mutator: StockSource | None = None,
    settings: Settings | None = None,
) -> TrackingPollResult:
    """Поздний проход: не развернула ли НП уже отправленную посылку обратно.

    Ходит редко (часы, не минуты) и по узкому окну `dispatched_at`, поэтому стоит
    единицы запросов в сутки. Нужен потому, что возврат приезжает физически к нам на
    склад и должен вернуться в остаток: полагаться на то, что кто-то заметит коробку
    и нажмёт кнопку, значит поставить точность остатка в зависимость от дисциплины.
    """
    settings = settings or get_settings()
    now = datetime.now(UTC)
    shipments = await ShipmentRepository(session).list_for_return_watch(
        dispatched_from=now - timedelta(days=settings.returns_watch_max_days),
        dispatched_to=now - timedelta(days=settings.returns_watch_min_days),
        recheck_before=now - timedelta(hours=settings.returns_recheck_hours),
        limit=settings.tracking_batch_limit,
    )
    _log.info("tracking.returns_poll", batch=len(shipments))
    return await _poll_batch(
        session,
        shipments=shipments,
        np_client=np_client,
        notifier=notifier,
        mutator=mutator,
    )


async def apply_tracking_status(
    session: AsyncSession,
    *,
    shipment: Shipment,
    tracking: TrackingStatus,
    notifier: Notifier | None = None,
    mutator: StockSource | None = None,
) -> tuple[bool, bool]:
    target_status = map_tracking_status(tracking)
    # Момент прошлого опроса нужен вердикту SLA как нижняя граница интервала, в
    # который случилась отправка, поэтому снимаем его ДО перезаписи.
    previous_poll_at = shipment.tracking_updated_at
    detected_at = datetime.now(UTC)
    shipment.tracking_updated_at = detected_at
    if target_status is None or target_status is shipment.status:
        await session.flush()
        return False, False

    repo = ShipmentRepository(session)
    before_status = shipment.status
    await repo.update_status(shipment, target_status)

    # Не «статус стал ровно `dispatched`», а «документ впервые ушёл дальше
    # `confirmed`». Разница не теоретическая: НП обновляет трек с задержкой, а мы
    # опрашиваем не непрерывно — воркер может лежать, а до правки расписания он
    # ещё и молчал два дня из семи. За такой перерыв посылка успевает уехать И
    # доехать, и следующий опрос видит сразу `in_transit`/`arrived`. Прежнее
    # условие на такой скачок не срабатывало вовсе: остаток не списывался,
    # `dispatched_at` не проставлялся, вердикт SLA не выносился, и ТТН выпадала
    # из отчётов целиком. Заметно это стало только сейчас — legacy-ветка
    # `OR (dispatched_at IS NULL AND status IN (…))` в отчётах такие строки
    # подбирала и тем прятала дыру.
    #
    # `dispatched_at is None` — маркер «ещё не отправляли»: он делает блок
    # идемпотентным, поэтому списание остатка не повторится, даже если статус
    # позже качнётся между пост-отправочными значениями.
    if target_status in POST_DISPATCH_STATUSES and shipment.dispatched_at is None:
        scanned_at = dispatch_scan_time(tracking)
        # `dispatched_at` — время отправки, а не время, когда мы про неё узнали.
        # Фолбэк на момент обнаружения оставлен осознанно: он нужен отчётам как
        # верхняя граница, но на вердикт SLA уже не влияет напрямую.
        shipment.dispatched_at = scanned_at or detected_at
        shipment.sla_met = sla_verdict(
            scanned_at=scanned_at,
            previous_poll_at=previous_poll_at,
            detected_at=detected_at,
            deadline=shipment.sla_deadline,
        )
        if shipment.sla_met is False:
            shipment.fee_free = True
            shipment.fee_amount = 0
        elif shipment.sla_met is None and shipment.sla_deadline is not None:
            # Дедлайн попал между прошлым и текущим опросом: по какую он сторону —
            # неизвестно. Не ставим ни промах, ни успех, но и не прячем случай.
            _log.warning(
                "sla.verdict_unknown",
                shipment_id=str(shipment.id),
                ttn=shipment.ttn_number,
                deadline=shipment.sla_deadline.isoformat(),
                previous_poll_at=previous_poll_at.isoformat() if previous_poll_at else None,
                detected_at=detected_at.isoformat(),
            )
        await _apply_dispatch_stock(session, shipment=shipment, mutator=mutator)

    if target_status is ShipmentStatus.cancelled:
        await _release_reserve_in_ledger(session, shipment=shipment)

    await AuditRepository(session).log(
        "shipment_tracking_status_updated",
        account_id=shipment.account_id,
        affected_entity=f"shipment:{shipment.id}",
        before={"status": before_status.value if before_status else None},
        after={
            "status": target_status.value,
            "np_status": tracking.status,
            "np_status_code": tracking.status_code,
        },
    )

    pushed = False
    if notifier is not None:
        await notifications.notify_shipment_status_changed(session, notifier, shipment=shipment)
        pushed = True
        if target_status in NONSTANDARD_STATUSES:
            await notifications.notify_nonstandard_shipment(
                session,
                notifier,
                client=shipment.client,
                shipment=shipment,
                note=tracking.status,
            )
    return True, pushed


async def _release_reserve_in_ledger(session: AsyncSession, *, shipment: Shipment) -> None:
    """Снять бронь в журнале, когда ТТН удалили в кабинете НП.

    Все ЧЕЛОВЕЧЕСКИЕ пути отмены пишут `ttn_cancel` (клиент, менеджер, удаление
    аккаунта). Этот — нет: НП отдаёт код 2 «Видалено», трекинг переводит ТТН в
    `cancelled`, и `ttn_reserve` остаётся в журнале без пары навсегда.

    Доступный остаток при этом верен: он считается по СТАТУСУ ТТН, а не по
    движениям, — поэтому дефект и был невидим полтора года. Цена другая: журнал
    перестаёт сходиться, а именно по нему разбирают инцидент «куда делся товар».
    Найдено живым прогоном по проду 2026-08-03: ТТН 20451492663031 от 21.07,
    `ttn_reserve` −8 по `Ide00011` без парного движения.

    `movement_exists` обязателен: трекинг идемпотентен и может увидеть «Видалено»
    несколько раз подряд, а второй `ttn_cancel` завысил бы журнал ровно так же,
    как его отсутствие занижало.
    """
    repo = ShipmentRepository(session)
    if await repo.movement_exists(shipment.id, StockMovementType.ttn_cancel):
        return
    if not await repo.movement_exists(shipment.id, StockMovementType.ttn_reserve):
        return
    await StockMovementRepository(session).record_for_items(
        client_id=shipment.client_id,
        account_id=shipment.account_id,
        shipment_id=shipment.id,
        actor_user_id=None,
        items=shipment.items,
        movement_type=StockMovementType.ttn_cancel,
        sign=1,
        comment=f"ТТН {shipment.ttn_number or '—'} видалено в кабінеті НП",
    )


async def _apply_dispatch_stock(
    session: AsyncSession,
    *,
    shipment: Shipment,
    mutator: StockSource | None = None,
) -> None:
    repo = ShipmentRepository(session)
    if await repo.movement_exists(shipment.id, StockMovementType.ttn_dispatch):
        return

    # Куда именно уйдёт списание, решает `stock_write`, а не эта функция: на `pg`
    # оно обязано лечь в `stock_balances`, иначе зеркало увидит расхождение листа с
    # `mirrored_quantity` и примет штатную отправку за ручную правку человека.
    await apply_physical_movement(
        session,
        account=shipment.account,
        client_id=shipment.client_id,
        shipment_id=shipment.id,
        items=items_from_shipment(shipment.items),
        movement_type=StockMovementType.ttn_dispatch,
        sign=-1,
        comment=f"Списання по ТТН {shipment.ttn_number or '—'}",
        mutator=mutator,
    )
    # Синк книги-зеркала оператора — только когда остаток живёт в листе. На `pg`
    # это чистый расход квоты Google: колонку «Кількість» ведёт Postgres, а её
    # проекцию в лист пишет зеркало воркера (`stock_mirror`), а не этот путь.
    if build_inventory_backend().name != "pg":
        await best_effort_sync(
            session,
            client=shipment.client,
            account=shipment.account,
            log_key="tracking_sheet_sync_failed",
            shipment_id=str(shipment.id),
        )


def _chunked[T](items: list[T], *, size: int) -> list[list[T]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
