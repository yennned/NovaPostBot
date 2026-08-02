"""Репозиторий остатков склада — единственная точка мутации количества.

Баланс первичен, движения — журнал. Не event-sourcing: экран товаров и гейт от
oversell обязаны быть одним индексированным запросом, а суммирование журнала на
каждое чтение — O(движений). Плюс журнал физически неполон: приёмку делает Apps
Script в обход бота, и до этой задачи она в `stock_movements` не попадала вовсе.

Но баланс и движение пишутся **в одной транзакции под `FOR UPDATE` по строке
баланса** — отсюда и честные `quantity_before`/`quantity_after`, и проверяемый
инвариант «сумма дельт по физическим типам == quantity», который ловит баг в
нашем коде без всякого сравнения с Google.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import Select, func, or_, select

from app.db.models.enums import StockMovementType
from app.db.models.stock_balance import StockBalance
from app.db.models.stock_movement import StockMovement
from app.db.repositories.base import BaseRepository

#: Типы, реально двигающие количество. `ttn_reserve`/`ttn_cancel` — про бронь,
#: которая выводится из статуса ТТН, поэтому количество они не трогают.
PHYSICAL_MOVEMENT_TYPES = (
    StockMovementType.intake,
    StockMovementType.ttn_dispatch,
    StockMovementType.ttn_return,
    StockMovementType.manual,
)


class StockBalanceRepository(BaseRepository):
    async def get(self, *, account_id: uuid.UUID, sku: str) -> StockBalance | None:
        stmt = select(StockBalance).where(
            StockBalance.account_id == account_id, StockBalance.sku == sku
        )
        return await self.session.scalar(stmt)

    async def list_for_account(self, account_id: uuid.UUID) -> list[StockBalance]:
        stmt = (
            select(StockBalance)
            .where(StockBalance.account_id == account_id)
            .order_by(StockBalance.category, StockBalance.name)
        )
        return list(await self.session.scalars(stmt))

    #: Порядок строк на экране склада. `coalesce(category, '')` — чтобы позиции без
    #: категории шли первыми и одной группой, а не расползались по NULLS-политике
    #: Postgres; `lower` — потому что человек не различает регистр при сортировке.
    _DISPLAY_ORDER = (
        func.lower(func.coalesce(StockBalance.category, "")),
        func.lower(StockBalance.name),
        func.lower(StockBalance.sku),
    )

    def _page_conditions(self, account_id: uuid.UUID, query: str | None, category: str | None):
        conditions = [StockBalance.account_id == account_id]
        if query:
            needle = f"%{query.strip()}%"
            conditions.append(
                or_(
                    StockBalance.sku.ilike(needle),
                    StockBalance.name.ilike(needle),
                    func.coalesce(StockBalance.category, "").ilike(needle),
                )
            )
        if category:
            conditions.append(
                func.lower(func.coalesce(StockBalance.category, "")) == category.strip().lower()
            )
        return conditions

    async def page(
        self,
        account_id: uuid.UUID,
        *,
        query: str | None = None,
        category: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[StockBalance], int]:
        """Страница остатка + сколько всего подходит под фильтр.

        Фильтр, сортировка и срез — в SQL. Раньше экран читал остаток целиком и
        резал страницу в Python: у аккаунта с 1636 позициями каждый тап пагинации
        стоил полной выгрузки ради восьми строк.
        """
        conditions = self._page_conditions(account_id, query, category)
        total = await self.session.scalar(
            select(func.count()).select_from(StockBalance).where(*conditions)
        )
        rows = await self.session.scalars(
            select(StockBalance)
            .where(*conditions)
            .order_by(*self._DISPLAY_ORDER)
            .limit(limit)
            .offset(offset)
        )
        return list(rows), int(total or 0)

    async def categories(self, account_id: uuid.UUID) -> list[str]:
        """Категории аккаунта — для кнопок фильтра.

        Считаются по всему остатку, а не по текущей странице и не по текущему
        фильтру: иначе выбор категории убирал бы с экрана все остальные кнопки.
        """
        rows = await self.session.scalars(
            select(StockBalance.category)
            .where(StockBalance.account_id == account_id, StockBalance.category.is_not(None))
            .distinct()
            .order_by(StockBalance.category)
        )
        return [category for category in rows if category]

    async def totals_by_account(
        self, account_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[int, int]]:
        """`{account_id: (позиций, единиц)}` одним запросом на все аккаунты.

        Экран менеджера «📦 Склад» показывает эти два числа по каждому аккаунту.
        На Sheets это принципиально чтение на книгу — при 20 аккаунтах 20 запросов
        и две трети минутной квоты на один тап. Здесь — один `GROUP BY`.

        Аккаунтов без строк остатка в ответе нет: подставить им нули — дело
        вызывающего, потому что «нет строк» и «нет такого аккаунта» здесь
        неразличимы, а на экране это разные вещи.
        """
        if not account_ids:
            return {}
        rows = await self.session.execute(
            select(
                StockBalance.account_id,
                func.count(),
                func.coalesce(func.sum(StockBalance.quantity), 0),
            )
            .where(StockBalance.account_id.in_(tuple(account_ids)))
            .group_by(StockBalance.account_id)
        )
        return {account_id: (int(positions), int(units)) for account_id, positions, units in rows}

    @staticmethod
    def lock_stmt(account_id: uuid.UUID, skus: Sequence[str]) -> Select[tuple[StockBalance]]:
        """Запрос захвата строк остатка. Вынесен отдельно, чтобы его можно было
        проверить компиляцией: гарантии `FOR UPDATE` и `ORDER BY sku` иначе
        держались бы только на поведенческом тесте, а он проходит и без них —
        Postgres и так обычно отдаёт строки по индексу."""
        return (
            select(StockBalance)
            .where(StockBalance.account_id == account_id, StockBalance.sku.in_(tuple(skus)))
            .order_by(StockBalance.sku)
            .with_for_update()
        )

    async def lock_for_update(
        self, *, account_id: uuid.UUID, skus: Sequence[str]
    ) -> dict[str, StockBalance]:
        """Залочить строки остатка по SKU. Порядок `ORDER BY sku` обязателен.

        Без него два сабмита с пересекающимися корзинами (`[A, B]` и `[B, A]`)
        берут строки в разном порядке и получают взаимоблокировку — Postgres
        снимет одного из них `DeadlockDetected`. Сортировка делает порядок
        захвата одинаковым для всех.
        """
        if not skus:
            return {}
        rows = await self.session.scalars(self.lock_stmt(account_id, skus))
        return {row.sku: row for row in rows}

    async def upsert_meta(
        self,
        *,
        account_id: uuid.UUID,
        sku: str,
        name: str | None = None,
        category: str | None = None,
        price: Decimal | None = None,
    ) -> StockBalance:
        """Создать строку остатка или обновить описательные поля, НЕ трогая количество.

        Описательными полями владеет лист: человек правит их прямо в «Складі», и
        ингест приносит правку сюда. Разделение по колонкам — то, что позволяет
        оставить ручную правку рабочей и при этом держать количество в PG.
        """
        balance = await self.get(account_id=account_id, sku=sku)
        if balance is None:
            balance = StockBalance(account_id=account_id, sku=sku, name=name or sku, quantity=0)
            await self._add(balance)
        if name is not None:
            balance.name = name
        if category is not None:
            balance.category = category
        if price is not None:
            balance.price = price
        await self.session.flush()
        return balance

    async def apply_movement(
        self,
        *,
        account_id: uuid.UUID,
        sku: str,
        delta: int,
        movement_type: StockMovementType,
        client_id: uuid.UUID | None = None,
        shipment_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        comment: str | None = None,
    ) -> StockMovement:
        """Изменить остаток и записать движение — атомарно, с честными before/after.

        Единственная точка мутации количества. Всё остальное (приёмка, отгрузка,
        возврат, ручная коррекция) ходит сюда, поэтому инвариант «сумма физических
        дельт == quantity» держится по построению, а не по дисциплине вызывающих.
        """
        locked = await self.lock_for_update(account_id=account_id, skus=[sku])
        balance = locked.get(sku)
        if balance is None:
            balance = StockBalance(account_id=account_id, sku=sku, name=sku, quantity=0)
            await self._add(balance)

        before = balance.quantity
        # Бронь количество не двигает: её держит статус ТТН, а не остаток.
        after = before + delta if movement_type in PHYSICAL_MOVEMENT_TYPES else before
        balance.quantity = after

        movement = StockMovement(
            client_id=client_id,
            account_id=account_id,
            shipment_id=shipment_id,
            actor_user_id=actor_user_id,
            sku=sku,
            movement_type=movement_type,
            quantity_delta=delta,
            quantity_before=before,
            quantity_after=after,
            comment=comment,
        )
        await self._add(movement)
        return movement

    async def ledger_matches_balance(self, account_id: uuid.UUID) -> list[tuple[str, int, int]]:
        """SKU, где сумма физических дельт разошлась с остатком: `(sku, ожидание, факт)`.

        Внутренняя сверка PG, не имеющая отношения к Google: она ловит баг в нашем
        коде, чего сравнение с листом дать не может в принципе.
        """
        ledger = (
            select(
                StockMovement.sku.label("sku"),
                func.sum(StockMovement.quantity_delta).label("total"),
            )
            .where(
                StockMovement.account_id == account_id,
                StockMovement.movement_type.in_(PHYSICAL_MOVEMENT_TYPES),
            )
            .group_by(StockMovement.sku)
            .subquery()
        )
        stmt = (
            select(StockBalance.sku, func.coalesce(ledger.c.total, 0), StockBalance.quantity)
            .outerjoin(ledger, ledger.c.sku == StockBalance.sku)
            .where(StockBalance.account_id == account_id)
        )
        rows = await self.session.execute(stmt)
        return [
            (sku, int(expected), int(actual))
            for sku, expected, actual in rows
            if expected != actual
        ]
