"""Брони остатка: check-and-hold под локом, привязка к ТТН, снятие, дворник.

Гейт от oversell живёт здесь целиком, потому что его нельзя выразить парой
«прочитать остаток» + «записать резерв»: между ними лежит вызов НП, и любое
решение, принятое до него, к моменту записи уже устарело. Проверка и захват
обязаны быть одной операцией под локом строк остатка.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update

from app.db.models.stock_hold import StockHold
from app.db.repositories.base import BaseRepository
from app.db.repositories.stock_balance import StockBalanceRepository


class InsufficientAvailable(Exception):
    """Свободного остатка не хватает: `available = quantity − бронь − резерв ТТН`."""

    def __init__(self, sku: str, requested: int, available: int) -> None:
        super().__init__(f"недостатньо залишку: {sku} — треба {requested}, доступно {available}")
        self.sku = sku
        self.requested = requested
        self.available = available


class StockHoldRepository(BaseRepository):
    async def active_by_sku(
        self, account_id: uuid.UUID, skus: Sequence[str] | None = None
    ) -> dict[str, int]:
        """Сумма активных броней по SKU."""
        stmt = (
            select(StockHold.sku, func.coalesce(func.sum(StockHold.quantity), 0))
            .where(StockHold.account_id == account_id, StockHold.released_at.is_(None))
            .group_by(StockHold.sku)
        )
        if skus:
            stmt = stmt.where(StockHold.sku.in_(tuple(skus)))
        rows = await self.session.execute(stmt)
        return {sku: int(total) for sku, total in rows}

    async def by_submit_key(self, submit_key: str) -> list[StockHold]:
        stmt = select(StockHold).where(StockHold.submit_key == submit_key)
        return list(await self.session.scalars(stmt))

    async def hold(
        self,
        *,
        account_id: uuid.UUID,
        client_id: uuid.UUID | None,
        submit_key: str,
        wanted: dict[str, int],
        reserved: dict[str, int],
        ttl_seconds: int,
    ) -> list[StockHold]:
        """Проверить свободный остаток и захватить бронь — **одной** операцией.

        Именно одной: между «проверили» и «записали» в старой схеме лежал вызов
        НП на 2,5 секунды, и два одновременных сабмита одного аккаунта видели один
        снимок остатка. Здесь строки остатка залочены `FOR UPDATE` c `ORDER BY sku`
        (без сортировки пересекающиеся корзины дают дедлок), поэтому второй сабмит
        ждёт первого и видит уже его бронь.

        `reserved` — резерв под живыми ТТН, приходит от вызывающего: он считается
        по `shipments`, а не по этой таблице, и тащить сюда зависимость от
        репозитория отправлений незачем.

        Повторный вызов с тем же `submit_key` возвращает уже созданные брони и
        ничего не захватывает заново — двойной тап не должен резервировать дважды.
        """
        existing = [h for h in await self.by_submit_key(submit_key) if h.released_at is None]
        if existing:
            return existing

        skus = sorted(wanted)
        balances = await StockBalanceRepository(self.session).lock_for_update(
            account_id=account_id, skus=skus
        )
        held = await self.active_by_sku(account_id, skus)

        for sku in skus:
            balance = balances.get(sku)
            quantity = 0 if balance is None else balance.quantity
            available = quantity - held.get(sku, 0) - int(reserved.get(sku, 0))
            requested = wanted[sku]
            if requested <= 0 or requested > available:
                raise InsufficientAvailable(sku, requested, max(available, 0))

        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        holds = [
            StockHold(
                account_id=account_id,
                client_id=client_id,
                sku=sku,
                quantity=wanted[sku],
                submit_key=submit_key,
                expires_at=expires_at,
            )
            for sku in skus
        ]
        self.session.add_all(holds)
        await self.session.flush()
        return holds

    async def attach(self, submit_key: str, *, shipment_id: uuid.UUID) -> int:
        """Привязать брони попытки к созданной ТТН и снять их.

        Снять — потому что с этого момента остаток держит статус отправления
        (`RESERVING_STATUSES`), и активная бронь поверх него вычла бы то же
        количество второй раз.
        """
        return await self._release(submit_key, shipment_id=shipment_id)

    async def release(self, submit_key: str) -> int:
        """Снять брони попытки: НП отказала, пользователь передумал, сбой."""
        return await self._release(submit_key, shipment_id=None)

    async def _release(self, submit_key: str, *, shipment_id: uuid.UUID | None) -> int:
        values: dict[str, object] = {"released_at": datetime.now(UTC)}
        if shipment_id is not None:
            values["shipment_id"] = shipment_id
        stmt = (
            update(StockHold)
            .where(StockHold.submit_key == submit_key, StockHold.released_at.is_(None))
            .values(**values)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return int(result.rowcount or 0)

    async def sweep_expired(self, *, now: datetime | None = None) -> int:
        """Снять брони, пережившие свой TTL.

        Нужен, потому что процесс может упасть между фазами сабмита: тогда бронь
        останется висеть, `available` будет занижен, и клиент не сможет продать
        собственный товар. Заниженный остаток — недопродажа, то есть та сторона
        ошибки, которую можно вычищать фоном; oversell так вычистить нельзя.
        """
        stmt = (
            update(StockHold)
            .where(
                StockHold.released_at.is_(None),
                StockHold.expires_at < (now or datetime.now(UTC)),
            )
            .values(released_at=now or datetime.now(UTC))
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return int(result.rowcount or 0)


def available_from(*, quantity: int, reserved: int, held: int) -> int:
    """`available` по одной позиции — единственная формула на всю систему.

    Вынесена, чтобы экран, гейт и сверка считали доступное одинаково: разойдись
    они, экран показывал бы одно, а сабмит отказывал по другому.
    """
    return max(quantity - reserved - held, 0)
