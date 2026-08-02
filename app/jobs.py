"""Фоновые задачи воркера Phase 5."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import structlog

from app.config import Settings, get_settings
from app.db.base import get_sessionmaker
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import (
    ClientAccountRepository,
    LowStockAlertRepository,
    StockHoldRepository,
    UserRepository,
)
from app.novaposhta.client import NovaPoshtaClient
from app.services import (
    duty,
    notifications,
    stock_ingest,
    stock_mirror,
    stock_reconcile,
    tracking,
)
from app.services.inventory import InventoryItem, get_inventory_snapshot
from app.services.notifications import Notifier
from app.sheets import StockSource

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LowStockResult:
    clients_checked: int
    alerts_sent: int


@dataclass(frozen=True, slots=True)
class DutyExpiryResult:
    cleared: int


class _KnownLowStockAlert(Protocol):
    is_low: bool
    last_notified_at: datetime | None


@dataclass(frozen=True, slots=True)
class LowStockPlannedUpdate:
    sku: str
    is_low: bool
    last_available: int
    last_notified_at: datetime | None


def _plan_low_stock_updates(
    *,
    threshold: int,
    items: list[InventoryItem],
    known: dict[str, _KnownLowStockAlert],
    now: datetime,
) -> tuple[list[InventoryItem], list[LowStockPlannedUpdate]]:
    should_notify: list[InventoryItem] = []
    updates: list[LowStockPlannedUpdate] = []
    for item in items:
        row = known.get(item.sku)
        is_low = item.available <= threshold
        was_low = bool(row and row.is_low)
        if is_low and not was_low:
            should_notify.append(item)
        updates.append(
            LowStockPlannedUpdate(
                sku=item.sku,
                is_low=is_low,
                last_available=item.available,
                last_notified_at=(
                    now if is_low and not was_low else row.last_notified_at if row else None
                ),
            )
        )
    return should_notify, updates


async def _collect_low_stock_alerts(
    session,
    *,
    client,
    account_id=None,
    threshold: int,
    items: list[InventoryItem],
) -> list[InventoryItem]:
    repo = LowStockAlertRepository(session)
    known_rows = (
        await repo.list_for_account(account_id)
        if account_id is not None
        else await repo.list_for_client(client.id)
    )
    known = {row.sku: row for row in known_rows}
    now = datetime.now(UTC)
    should_notify, updates = _plan_low_stock_updates(
        threshold=threshold,
        items=items,
        known=known,
        now=now,
    )
    for update in updates:
        await repo.upsert_state(
            client_id=client.id,
            account_id=account_id,
            sku=update.sku,
            is_low=update.is_low,
            last_available=update.last_available,
            last_notified_at=update.last_notified_at,
        )

    return should_notify


async def poll_tracking_job(
    *,
    np_client: NovaPoshtaClient,
    notifier: Notifier | None = None,
    mutator: StockSource | None = None,
    settings: Settings | None = None,
) -> tracking.TrackingPollResult:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await tracking.poll_shipments(
            session,
            np_client=np_client,
            notifier=notifier,
            mutator=mutator,
            settings=settings or get_settings(),
        )
        await session.commit()
        return result


async def poll_returns_job(
    *,
    np_client: NovaPoshtaClient,
    notifier: Notifier | None = None,
    mutator: StockSource | None = None,
    settings: Settings | None = None,
) -> tracking.TrackingPollResult:
    """Поздний проход возвратов — отдельной джобой, с частотой в часах, а не минутах."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await tracking.poll_returns(
            session,
            np_client=np_client,
            notifier=notifier,
            mutator=mutator,
            settings=settings or get_settings(),
        )
        await session.commit()
        return result


async def stock_ingest_job(
    *,
    notifier: Notifier | None = None,
    settings: Settings | None = None,
) -> stock_ingest.IngestResult:
    """Перенести новые события приёмки из листа «Історія» в `stock_balances`.

    Коммит один на проход: дельты и водораздел обязаны уехать вместе. При
    остановке (разошёлся отпечаток строки-водораздела) коммита нет вовсе — в БД не
    должно остаться половины пачки.
    """
    current_settings = settings or get_settings()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await stock_ingest.ingest_intake_history(session, settings=current_settings)
        if result.halted_reason is None:
            await session.commit()
        else:
            await session.rollback()
            if notifier is not None and stock_ingest.should_notify_halt(
                current_settings.sheets_stock_book_id, result.halted_reason
            ):
                await notifications.notify_stock_ingest_halted(
                    session, notifier, reason=result.halted_reason, settings=current_settings
                )
        return result


async def stock_mirror_job(
    *,
    notifier: Notifier | None = None,
    settings: Settings | None = None,
) -> list[stock_mirror.AccountMirrorResult]:
    """Зеркало PG → лист «Склад» + приём ручных правок количества.

    Порядок в цикле воркера важен: сначала ингест приёмки, потом зеркало. Приёмка,
    попавшая между ними, доедет следующим циклом; обратный порядок означал бы, что
    зеркало пишет в лист остаток, ещё не знающий о только что внесённой приёмке.
    """
    current_settings = settings or get_settings()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        results = await stock_mirror.mirror_all_accounts(session, settings=current_settings)
        await session.commit()
        if notifier is not None:
            for result in results:
                applied = [(e.sku, e.was, e.now) for e in result.edits if e.applied]
                rejected = [(e.sku, e.was, e.now, e.reason) for e in result.edits if not e.applied]
                await notifications.notify_stock_manual_edits(
                    session,
                    notifier,
                    account_label=result.key,
                    applied=applied,
                    rejected=rejected,
                    settings=current_settings,
                )
        return results


async def stock_hold_sweep_job(*, settings: Settings | None = None) -> int:
    """Снять брони, пережившие TTL: процесс мог упасть между фазами сабмита.

    Без дворника такая бронь висит вечно, `available` занижен, и клиент не может
    продать собственный товар. Заниженный остаток — та сторона ошибки, которую
    можно вычищать фоном; oversell так вычистить нельзя.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        released = await StockHoldRepository(session).sweep_expired()
        await session.commit()
        if released:
            logger.info("stock_holds.swept", released=released)
        return released


async def stock_reconcile_job(
    *,
    notifier: Notifier | None = None,
    settings: Settings | None = None,
) -> list[stock_reconcile.AccountReconcileResult]:
    """Сверка остатка: PG против листа и PG против собственного журнала.

    Только читает — ничего не чинит. «Усыновить» число из листа значило бы
    превратить опечатку человека в разрешение продать несуществующий товар.
    """
    current_settings = settings or get_settings()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        results = await stock_reconcile.reconcile_all_accounts(session)
        if notifier is not None:
            for result in results:
                text = stock_reconcile.report_text(result)
                if text is not None:
                    await notifications.notify_staff(
                        session, notifier, text=text, settings=current_settings
                    )
        return results


async def clear_expired_duty_job(
    *,
    notifier: Notifier | None = None,
    settings: Settings | None = None,
) -> DutyExpiryResult:
    """Снять дежурство у менеджеров после закрытия отделения; опц. уведомить их."""
    current_settings = settings or get_settings()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        cleared = await duty.clear_expired_duty(session, settings=current_settings)
        recipient_ids = [user.telegram_id for user in cleared]  # до commit (expire)
        await session.commit()
    if notifier is not None and recipient_ids:
        text = notifications.duty_shift_ended_text()
        for telegram_id in recipient_ids:
            await notifier.send_message(telegram_id, text)
    return DutyExpiryResult(cleared=len(recipient_ids))


async def low_stock_job(
    *,
    notifier: Notifier,
    settings: Settings | None = None,
) -> LowStockResult:
    current_settings = settings or get_settings()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        repo = UserRepository(session)
        clients, _ = await repo.list_by_status(
            role=UserRole.client, status=UserStatus.active, limit=500
        )
        alerts = 0
        for client in clients:
            account_scope = await ClientAccountRepository(session).get_context_for_user(client.id)
            account = account_scope[0] if account_scope is not None else None
            account_id = account.id if account is not None else None
            items = await get_inventory_snapshot(
                session,
                client=client,
                account_id=account_id,
                account=account,
            )
            low = await _collect_low_stock_alerts(
                session,
                client=client,
                account_id=account_id,
                threshold=current_settings.low_stock_threshold,
                items=items,
            )
            if not low:
                continue
            await notifications.notify_low_stock(session, notifier, client=client, items=low)
            alerts += 1
        await session.commit()
        return LowStockResult(clients_checked=len(clients), alerts_sent=alerts)
