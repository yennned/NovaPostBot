"""Регрессия: экран менеджера «📦 Склад» — строка на аккаунт, а не на человека.

Баг был живым в проде: экран выбирал всех `User.role == client` и звал сводку по
`User`. Работник аккаунта — тоже `role=client` (заводится с `create_account=False`,
`full_name=None`), поэтому его `users.stock_sheet_key` — это телефон. Листа с таким
именем нет → менеджер видел фантомную строку «<телефон> — лист недоступний», а сам
аккаунт при этом дублировался строкой владельца.
"""

from __future__ import annotations

from app.bot.handlers.manager_shipments import _warehouse_text
from app.bot.types import ClientAccountContext
from app.db.models.enums import UserRole, UserStatus
from app.db.repositories import ClientAccountRepository, UserRepository
from app.services import account_team, inventory
from app.sheets.source import StockRow
from sqlalchemy.ext.asyncio import AsyncSession


class _KeyedSource:
    """Лист есть только у ключа «Магазин» — как у единственного аккаунта."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def read_stock(self, client_key: str) -> list[StockRow]:
        self.seen.append(client_key)
        if client_key != "Магазин":
            raise RuntimeError(f"лист не знайдено: {client_key}")
        return [StockRow(sku="A", name="a", category=None, quantity=7, price=None)]


async def test_employee_does_not_get_a_phantom_warehouse_row(db_session: AsyncSession, monkeypatch):
    owner = await UserRepository(db_session).create(
        telegram_id=8100,
        phone="+380990000100",
        full_name="Магазин",
        role=UserRole.client,
        status=UserStatus.active,
        account_name="Магазин",
    )
    accounts = ClientAccountRepository(db_session)
    membership = await accounts.get_membership(user_id=owner.id)
    assert membership is not None
    account = membership.account
    account.stock_sheet_key = "Магазин"

    # Работник аккаунта: тоже role=client, без своего аккаунта и без full_name.
    invited = await account_team.invite_employee(
        db_session,
        context=ClientAccountContext(user=owner, account=account, membership=membership),
        phone="0990000101",
    )
    employee = await UserRepository(db_session).get_by_id(invited.user_id)
    await account_team.activate_employee_contact(
        db_session, user=employee, telegram_id=8101, full_name="Працівник"
    )
    await db_session.flush()
    # У работника нет и не может быть своего ключа склада: колонка снесена, склад —
    # свойство аккаунта. Раньше ключом был его телефон, отсюда и брался фантом.
    assert not hasattr(employee, "stock_sheet_key")

    # Зовём НАСТОЯЩИЙ билдер экрана, а не копию его запроса: иначе откат хендлера
    # на выборку по `User` тест бы не заметил. Фейкается только адаптер Sheets.
    source = _KeyedSource()
    monkeypatch.setattr(inventory, "build_stock_source", lambda *a, **kw: source)
    text = await _warehouse_text(db_session)

    assert "Магазин — 1 поз. / 7 од." in text
    assert "лист недоступний" not in text, f"фантомная строка вернулась:\n{text}"
    assert source.seen == ["Магазин"], f"читались лишние ключи: {source.seen}"
    # Работник не должен попасть на экран ни строкой, ни телефоном.
    assert "Працівник" not in text
    assert "0990000101" not in text
