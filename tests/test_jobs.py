"""Тесты фоновых Phase 5 jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

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
