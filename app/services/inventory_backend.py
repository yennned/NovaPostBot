"""Порт источника остатков: Google Sheets или Postgres.

**Почему отдельный порт, а не третья реализация `StockSource`**
(`app/sheets/source.py`). `StockSource` — это интерфейс адаптера Google, и каждое
его свойство завязано на Google: он синхронный (гоняется через single-worker
executor, потому что gspread блокирующий), адресуется `client_key` — именем
вкладки, а `apply_deltas` применяется мимо транзакции вызывающего, потому что у
Google транзакций нет. Postgres-реализации нужно ровно обратное: `AsyncSession`,
адресация по `account_id` и мутация внутри той же транзакции, что и
`stock_movements`. Натянуть одно на другое можно только ценой лжи в сигнатурах,
поэтому развилка поднята слоем выше — сюда.

`StockSource` при этом остаётся жив и нужен: ингест приёмки, зеркало в лист и
провижн книг ходят в Google и после переключения остатка на Postgres.

Здесь только **чтение**. Запись переезжает не «такой же развилкой»: у Sheets это
`apply_deltas` вслепую, у Postgres — locked check-and-hold в общей транзакции, и
их общий интерфейс был бы фикцией. Пути записи меняются отдельной задачей вместе
с гейтом от oversell.
"""

from __future__ import annotations

import time
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.client_account import ClientAccount
from app.db.repositories import StockBalanceRepository
from app.sheets import (
    StockRow,
    StockSheetNotFound,
    StockSource,
    current_stock_source,
    run_sheets_read,
)

logger = structlog.get_logger(__name__)


def stock_sheet_key(account: ClientAccount) -> str:
    """Ключ листа склада аккаунта.

    Предпочитаем персистентное поле `stock_sheet_key`, чтобы переименование не
    ломало связь с Sheets между чтением и следующей синхронизацией. Fallback —
    для данных, заведённых до миграции ключей.

    Лист принадлежит аккаунту, а не человеку: у работника своего листа нет.

    `.strip()`, а не голый `or`: имя из пробелов — непустая строка, и она прошла бы
    мимо фолбэка. Синк (`client_sheet_sync`) на таком имени берёт `account.id`, и
    расхождение читателя с синком означало бы чтение несуществующей вкладки, то
    есть молча пустой склад.
    """
    return account.stock_sheet_key or account.name.strip() or str(account.id)


class InventoryBackend(Protocol):
    """Откуда сервис-слой берёт остаток аккаунта."""

    #: Для логов и диагностики (`scripts/e2e/preflight.py`).
    name: str

    #: Ошибки, при которых сводка склада показывает «недоступно» вместо падения.
    #: У Sheets это штатная жизнь (нет листа, 429, блип сети), у Postgres таких нет:
    #: сбой БД обязан быть виден, а не притворяться недоступным листом.
    transient_read_errors: tuple[type[Exception], ...]

    async def read_rows(self, session: AsyncSession, account: ClientAccount) -> list[StockRow]: ...


class SheetsInventoryBackend:
    """Сегодняшнее поведение целиком: остаток читается из книги «Склад».

    Он же сеть безопасности отката: пока этот бэкенд жив, `INVENTORY_SOURCE=sheets`
    возвращает систему в прежнее состояние сменой переменной окружения.
    """

    name = "sheets"
    transient_read_errors = (Exception,)

    def __init__(self, source: StockSource | None = None) -> None:
        # Источник резолвится в момент чтения, а не здесь: `current_stock_source()`
        # отдаёт per-update мемо из ContextVar, и захват его в конструкторе
        # приколотил бы бэкенд к чужому апдейту.
        self._source = source

    async def read_rows(self, session: AsyncSession, account: ClientAccount) -> list[StockRow]:
        source = self._source or current_stock_source()
        key = stock_sheet_key(account)
        started = time.monotonic()
        try:
            # Через выделенный single-worker executor, а не `asyncio.to_thread`: клиент
            # gspread один на процесс, и общий пул потоков означал бы гонку по
            # непотокобезопасной сессии. Заодно чтения не конкурируют с записями склада.
            # `StockSourceUnavailable` НЕ глотаем — см. комментарий у самого исключения.
            rows = await run_sheets_read(source.read_stock, key)
        except StockSheetNotFound:
            # Лист склада ещё не заведён/переименован — это пустой остаток, а не сбой:
            # клиент видит «склад порожній», а не падение хендлера створення ТТН.
            logger.warning("inventory.sheet_missing", account_id=str(account.id), key=key)
            rows = []
        logger.info(
            "inventory.sheet_read",
            key=key,
            rows=len(rows),
            duration_ms=round((time.monotonic() - started) * 1000),
            # Сколько РЕАЛЬНЫХ обращений к Sheets сделал источник этого апдейта: по нему
            # видно, что рендер+синк укладываются в одно чтение, и считается расход квоты.
            source_reads=getattr(source, "reads", None),
        )
        return rows


class PgInventoryBackend:
    """Остаток из `stock_balances`. Ни одного обращения к Google на чтение."""

    name = "pg"
    #: Пусто намеренно: `except ()` не ловит ничего. Сбой Postgres — это сбой, а не
    #: «залишки тимчасово недоступні»: молча показать пустой склад из-за упавшего
    #: запроса значит нарисовать правдоподобную неправду.
    transient_read_errors: tuple[type[Exception], ...] = ()

    async def read_rows(self, session: AsyncSession, account: ClientAccount) -> list[StockRow]:
        started = time.monotonic()
        balances = await StockBalanceRepository(session).list_for_account(account.id)
        logger.info(
            "inventory.pg_read",
            account_id=str(account.id),
            rows=len(balances),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        # `StockRow` лежит в `app/sheets/`, но это доменный DTO строки остатка, а не
        # деталь Google: сервис-слой и экраны уже говорят на нём. Переносить его в
        # отдельный пакет — churn ради адреса файла.
        return [
            StockRow(
                sku=balance.sku,
                name=balance.name,
                category=balance.category,
                quantity=balance.quantity,
                price=balance.price,
            )
            for balance in balances
        ]


def build_inventory_backend(settings: Settings | None = None) -> InventoryBackend:
    """Бэкенд остатка согласно `INVENTORY_SOURCE`.

    `crm` уходит в Sheets-бэкенд намеренно: развилка `sheets|crm` живёт уровнем
    ниже, в `build_stock_source`, и `CrmStockSource` честно падает «ще не
    реалізовано». Дублировать её здесь значило бы завести второе место, где
    решается один и тот же вопрос.
    """
    cfg = settings or get_settings()
    if cfg.inventory_source == "pg":
        return PgInventoryBackend()
    return SheetsInventoryBackend()


def resolve_inventory_backend(
    *, reader: StockSource | None = None, backend: InventoryBackend | None = None
) -> InventoryBackend:
    """Какой бэкенд обслуживает вызов: явный `backend`, явный `reader`, конфигурация.

    Явный `reader` продолжает означать ровно то, что означал всегда, — «читать
    остаток вот этим источником Sheets», — и потому побеждает конфигурацию. На нём
    держатся per-update мемо и подмена источника в тестах; если бы
    `INVENTORY_SOURCE=pg` его перебивал, оба механизма отключились бы молча.
    """
    if backend is not None:
        return backend
    if reader is not None:
        return SheetsInventoryBackend(reader)
    return build_inventory_backend()
