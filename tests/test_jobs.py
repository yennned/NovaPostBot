"""Тесты фоновых Phase 5 jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app import jobs
from app.config import Settings
from app.db.models.enums import MembershipStatus, UserRole, UserStatus
from app.db.repositories import ClientAccountRepository, UserRepository
from app.jobs import _plan_low_stock_updates
from app.services.inventory import InventoryItem
from app.sheets import StockSourceUnavailable, reset_stock_source, use_stock_source
from app.sheets.source import StockRow
from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_notifications import FakeNotifier


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


class _CountingStockSource:
    """Источник склада, который считает обращения к книге.

    Именно счётчик — предмет проверки: раньше джоба читала склад по разу на
    КАЖДОГО участника аккаунта, хотя склад у команды один.
    """

    def __init__(self, rows: list[StockRow], fails_for: set[str] | None = None) -> None:
        self._rows = rows
        self._fails_for = fails_for or set()
        self.reads: list[str] = []

    def read_stock(self, client_key: str) -> list[StockRow]:
        self.reads.append(client_key)
        if client_key in self._fails_for:
            raise StockSourceUnavailable(client_key, 503)
        return list(self._rows)

    def apply_deltas(self, client_key: str, deltas) -> None:  # pragma: no cover — джоба не пишет
        raise AssertionError("low_stock_job не должна писать в склад")


async def _team_account(session: AsyncSession, *, base_id: int, name: str, size: int):
    """Аккаунт с `size` активными участниками — как настоящая команда клиента."""
    users = UserRepository(session)
    accounts = ClientAccountRepository(session)
    owner = await users.create(
        telegram_id=base_id,
        phone=f"+3809900{base_id}",
        full_name=name,
        role=UserRole.client,
        status=UserStatus.active,
        account_name=name,
    )
    membership = await accounts.get_membership(user_id=owner.id)
    account = membership.account
    account.stock_sheet_key = name
    for index in range(1, size):
        employee = await users.create(
            telegram_id=base_id + index,
            phone=f"+3809900{base_id + index}",
            full_name=f"{name} · працівник {index}",
            role=UserRole.client,
            status=UserStatus.active,
            create_account=False,
        )
        invited = await accounts.create_invited_membership(
            account_id=account.id, user=employee, invited_by_user_id=owner.id
        )
        await accounts.set_membership_status(invited, MembershipStatus.active)
    await session.flush()
    return account, [base_id + index for index in range(size)]


def _run_job_on(session: AsyncSession, monkeypatch) -> None:
    """Заставить джобу работать в сессии теста, а не поднимать свою."""

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(jobs, "get_sessionmaker", lambda: _Ctx)


async def test_low_stock_job_reads_account_once_and_tells_whole_team(
    db_session: AsyncSession, monkeypatch
):
    """Одно чтение на аккаунт — и алерт всей команде, а не одному её участнику.

    Два дефекта в одном месте. Джоба ходила по клиентам: три человека в аккаунте
    давали три одинаковых чтения склада (на боевых двадцати аккаунтах это разница
    между 20 и 126 обращениями к Google при квоте 60/мин). А состояние алертов
    хранится ПО АККАУНТУ, поэтому первый же участник помечал SKU как «уже
    уведомили», и остальные двое не получали ничего — кто окажется первым, зависело
    от порядка выборки.

    Мутация: вернуть цикл по клиентам — `reads` станет три, а получателей из
    команды останется один.
    """
    _, team = await _team_account(db_session, base_id=7100, name="Магазин", size=3)
    staff = UserRepository(db_session)
    await staff.create(telegram_id=7001, role=UserRole.owner, status=UserStatus.active)
    duty_manager = await staff.create(
        telegram_id=7002, role=UserRole.manager, status=UserStatus.active
    )
    duty_manager.on_duty = True
    await db_session.flush()

    # 700 позиций, низкая — глубоко в середине: страничная выборка её не увидит.
    rows = [
        StockRow(
            sku=f"SKU-{index:03d}", name="Товар", category="Категорія", quantity=100, price=None
        )
        for index in range(700)
    ]
    rows[500] = StockRow(sku="SKU-500", name="Кава", category="Напої", quantity=1, price=None)
    source = _CountingStockSource(rows)
    notifier = FakeNotifier()
    _run_job_on(db_session, monkeypatch)
    token = use_stock_source(source)
    try:
        result = await jobs.low_stock_job(notifier=notifier, settings=Settings(_env_file=None))
    finally:
        reset_stock_source(token)

    assert source.reads == ["Магазин"]  # ровно одно чтение, а не по числу людей
    assert result.accounts_checked == 1
    assert result.alerts_sent == 1
    recipients = {tid for tid, _ in notifier.sent}
    assert set(team) <= recipients  # алерт получила ВСЯ команда
    assert {7001, 7002} <= recipients  # и персонал
    assert any("SKU-500" in text for _, text in notifier.sent)


async def test_low_stock_job_survives_one_broken_warehouse(db_session: AsyncSession, monkeypatch):
    """Недоступный склад одного аккаунта не отменяет алерты остальным.

    Раньше `StockSourceUnavailable` поднималось наружу и гасило проход целиком:
    один аккаунт без листа — и низкий остаток не увидел никто. Вероятность отказа
    растёт с числом аккаунтов, то есть дефект тем злее, чем больше клиентов.

    Мутация: убрать `except StockSourceUnavailable` — джоба падает, второй аккаунт
    остаётся без уведомления.
    """
    await _team_account(db_session, base_id=7200, name="Зламаний", size=1)
    _, ok_team = await _team_account(db_session, base_id=7300, name="Робочий", size=1)
    rows = [StockRow(sku="SKU-A", name="Кава", category="Напої", quantity=1, price=None)]
    source = _CountingStockSource(rows, fails_for={"Зламаний"})
    notifier = FakeNotifier()
    _run_job_on(db_session, monkeypatch)
    token = use_stock_source(source)
    try:
        result = await jobs.low_stock_job(notifier=notifier, settings=Settings(_env_file=None))
    finally:
        reset_stock_source(token)

    # Ключи, а не вызовы: сломанное чтение ретраится (`sheets_retry_attempts`),
    # и это правильно — проверяем, что джоба дошла до ВТОРОГО аккаунта.
    assert sorted(set(source.reads)) == ["Зламаний", "Робочий"]
    assert result.accounts_checked == 2
    assert result.alerts_sent == 1  # сломанный аккаунт пропущен, рабочий уведомлён
    assert set(ok_team) <= {tid for tid, _ in notifier.sent}
