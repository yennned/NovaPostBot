"""Тесты фоновых Phase 5 jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from app import jobs
from app.config import Settings
from app.db.models.enums import UserRole, UserStatus
from app.jobs import _plan_low_stock_updates
from app.services.inventory import InventoryItem


@dataclass
class _KnownAlert:
    is_low: bool
    last_notified_at: datetime | None = None


def test_plan_low_stock_updates_notifies_only_on_transition():
    item_low = InventoryItem(
        sku="SKU-LOW",
        name="Кава",
        category="Кава",
        stock=2,
        reserved=0,
        available=2,
        price=Decimal("100"),
    )
    item_ok = InventoryItem(
        sku="SKU-LOW",
        name="Кава",
        category="Кава",
        stock=7,
        reserved=0,
        available=7,
        price=Decimal("100"),
    )
    now = datetime.now(UTC)

    first_notify, first_updates = _plan_low_stock_updates(
        threshold=3,
        items=[item_low],
        known={},
        now=now,
    )
    second_notify, second_updates = _plan_low_stock_updates(
        threshold=3,
        items=[item_low],
        known={"SKU-LOW": _KnownAlert(is_low=True, last_notified_at=now)},
        now=now,
    )
    recovery_notify, recovery_updates = _plan_low_stock_updates(
        threshold=3,
        items=[item_ok],
        known={"SKU-LOW": _KnownAlert(is_low=True, last_notified_at=now)},
        now=now,
    )
    third_notify, third_updates = _plan_low_stock_updates(
        threshold=3,
        items=[item_low],
        known={"SKU-LOW": _KnownAlert(is_low=False, last_notified_at=now)},
        now=now,
    )

    assert [item.sku for item in first_notify] == ["SKU-LOW"]
    assert first_updates[0].is_low is True
    assert first_updates[0].last_notified_at == now
    assert second_notify == []
    assert second_updates[0].last_notified_at == now
    assert recovery_notify == []
    assert recovery_updates[0].is_low is False
    assert [item.sku for item in third_notify] == ["SKU-LOW"]
    assert third_updates[0].last_notified_at == now


async def test_low_stock_job_uses_full_inventory_snapshot(monkeypatch):
    client = SimpleNamespace(
        id="client-1",
        telegram_id=700,
        role=UserRole.client,
        status=UserStatus.active,
    )
    session = SimpleNamespace(committed=False)

    async def commit():
        session.committed = True

    session.commit = commit

    class _SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Repo:
        def __init__(self, current_session):
            assert current_session is session

        async def list_by_status(self, **kwargs):
            return [client], 1

    items = [
        InventoryItem(
            sku=f"SKU-{index}",
            name="Товар",
            category="Категорія",
            stock=1,
            reserved=0,
            available=1,
            price=Decimal("10"),
        )
        for index in range(700)
    ]
    observed = {"snapshot": 0, "notify": 0}

    async def fake_get_inventory_snapshot(current_session, *, client, reader=None):
        assert current_session is session
        observed["snapshot"] = len(items)
        return items

    async def fake_collect_low_stock_alerts(current_session, *, client, threshold, items):
        assert current_session is session
        return items

    async def fake_notify_low_stock(current_session, notifier, *, client, items):
        assert current_session is session
        observed["notify"] = len(items)

    monkeypatch.setattr(jobs, "get_sessionmaker", lambda: lambda: _SessionContext())
    monkeypatch.setattr(jobs, "UserRepository", _Repo)
    monkeypatch.setattr(jobs, "get_inventory_snapshot", fake_get_inventory_snapshot)
    monkeypatch.setattr(jobs, "_collect_low_stock_alerts", fake_collect_low_stock_alerts)
    monkeypatch.setattr(jobs.notifications, "notify_low_stock", fake_notify_low_stock)

    result = await jobs.low_stock_job(notifier=object(), settings=Settings(_env_file=None))

    assert observed["snapshot"] == 700
    assert observed["notify"] == 700
    assert session.committed is True
    assert result.clients_checked == 1
    assert result.alerts_sent == 1
