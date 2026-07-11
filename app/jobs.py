"""Фоновые задачи воркера Phase 5."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.config import Settings, get_settings
from app.db.base import get_sessionmaker
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import ClientAccountRepository, LowStockAlertRepository, UserRepository
from app.novaposhta.client import NovaPoshtaClient
from app.services import duty, notifications, tracking
from app.services.inventory import InventoryItem, get_inventory_snapshot
from app.services.notifications import Notifier
from app.sheets import StockSource


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
