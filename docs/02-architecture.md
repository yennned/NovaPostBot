# 02 — Архитектура и модель данных

## Структура проекта

```
app/
  config.py              pydantic-settings; BOT_TOKEN, DATABASE_URL (pooled),
                         DATABASE_URL_DIRECT (Alembic), REDIS_URL,
                         INVENTORY_SOURCE, GOOGLE_SA_JSON (service account),
                         SHEETS_* (ID книг), FERNET_KEY, OWNER_TELEGRAM_IDS,
                         DEV_TELEGRAM_IDS, TIMEZONE
  logging_config.py      structlog
  main.py                запуск бота (long polling)
  worker.py              APScheduler-воркер (трекинг, low-stock)
  jobs.py                фоновые задачи
  db/                    PostgreSQL — ВСЯ БД (SQLAlchemy async + Alembic)
    base.py              engine/session
    models/              user, sender_profile (ФОП), shipment, support,
                         notifications, audit, enums
    repositories/        user, sender_profile, shipment, support, stats,
                         notifications, audit
  sheets/                seam источника склада (`StockSource`)
    client.py            Sheets API: чтение/запись, лист на клиента, кэш
    inventory.py         Google Sheets adapter + CRM/WMS stub
    source.py            контракт `StockSource`, `StockRow`, `StockDelta`
  bot/
    dispatcher.py        сборка диспетчера и роутеров
    middlewares.py       inject session/sheets/user/t/bot/np, Throttling
    permissions.py       RBAC: иерархия ролей + per-flag права + dev god-mode
    states.py            FSM (Redis)
    filters.py           TextIn и пр.
    keyboards/           меню по ролям
    texts/               украинские строки (реестр + переводчик-обёртка)
    handlers/            start, client_cabinet, clients_manage, ttn, stats,
                         support, notifications, dev, common
  services/
    inventory.py         резерв/списание/возврат: остаток в Sheets, движения в Postgres
    shipment.py          создание/отмена ТТН (NP-first)
    notifications.py     исходящие пуши (надёжная доставка)
    support.py           треды поддержки, дежурство менеджера
    audit.py             append-only аудит (Postgres `audit_logs`)
    reports.py           агрегаты для персонала (склад/отправки)
  novaposhta/            client, methods, schemas, tracking, exceptions
  utils/                 crypto (Fernet), validators
migrations/              Alembic
tests/                   unit-тесты (чистая логика, без живых Postgres/Sheets/TG)
```

## Хранилище данных (гибрид)

### Подключение к Neon (asyncpg + Alembic)

- **Приложение** ходит через **пулер Neon** (`-pooler`-хост, PgBouncer). У asyncpg
  за PgBouncer в transaction-режиме prepared statements не работают → в движке
  ставим **`statement_cache_size=0`** (иначе `DuplicatePreparedStatementError`).
- **Alembic-миграции** гонять через **прямой (non-pooled) коннект** Neon (Neon
  это явно рекомендует; пулер для миграций ненадёжен). Удобно: `DATABASE_URL`
  (pooled) для приложения + `DATABASE_URL_DIRECT` для Alembic.
- Конфиг движка/сессии — `app/db/base.py`.

### PostgreSQL — вся БД (managed Neon)

- **`users`** — клиенты/менеджеры/владелец: роль (`client`/`manager`/`owner`),
  статус (`pending`/`active`/`blocked`/`archived`), телефон, ПІБ, `permissions`
  (JSONB, per-flag для менеджера), дежурство (`on_duty`, `duty_date`),
  таймстемпы.
- **`sender_profiles`** (ФОП) — много на клиента: `client_id`, `name`,
  `np_api_key` (**Fernet-шифр**), `sender_full_name`, `sender_phone`, `org_type`
  (ФОП/ТОВ), `edrpou`, `np_sender_ref`/`np_contact_ref`, `np_sender_warehouse`,
  `is_default`, таймстемпы.
- **`shipments`** + **`shipment_items`** — ТТН: номер/`np_ref`, клиент, ФОП,
  получатель (тип фіз/юр, ПІБ/телефон, місто/відділення), позиции
  (артикул×кол-во), вес/розмір, оплата/COD/страховка, `status`, таймстемпы
  (`created_at`, `dispatched_at`, `status_updated_at`), резерв. **SLA:**
  `sla_deadline`, `sla_met`/`sla_missed`. **Комиссия:** `fee_amount`,
  `fee_free` (true при промахе SLA → fee = 0). Расчёт SLA/fee —
  [08-notifications-tracking-returns.md](08-notifications-tracking-returns.md).
- **`stock_movements`** — журнал движений (append-only): тип
  (`ttn_reserve`/`ttn_dispatch`/`ttn_cancel`/`ttn_return`/`manual`),
  было→стало, кто, когда, ссылка на ТТН.
- **`support_threads`** / **`support_messages`** — поддержка: client_id,
  assigned_manager_id, статус (`open`/`waiting`/`closed`), привязка к ТТН,
  sender_role, текст, таймстемпы.
- **`notification_settings`** — тумблеры уведомлений по типам на пользователя.
- **`audit_logs`** — append-only аудит всех чувствительных действий (вкл.
  `dev_*`).

### Google Sheets — только учёт склада

- **Книга «Склад»** — лист на клиента, текущие остатки (артикул/назва/категорія/
  кількість/ціна). Read-only (Protected), правит только Script/бот.
- **Книга «Приймання»** — лист на клиента, черновик ввода; синк в «Склад»
  кнопкой «Внести» (Apps Script, double confirmation).
- **Лист «Історія»** — журнал приёмок.

Детали механики — [04-warehouse-sheets.md](04-warehouse-sheets.md).

### Связка Postgres ↔ Sheets

- Бот читает остаток из «Склад» (Sheets), а резерв под ТТН и историю движений —
  в Postgres (`stock_movements`).
- `available = quantity(Sheets) − reserved(Postgres)`.
- При «відправлено» бот пишет списание в лист «Склад» (Sheets API), приёмка
  прибавляет (Apps Script) — над одним листом.
- Phase 7 уже вынесла seam `app/sheets/`: сейчас дефолтный источник —
  `GoogleSheetsStockSource`, переключение идёт через `INVENTORY_SOURCE`, а
  будущий CRM/WMS REST adapter подключается без правок handler/service слоя.

## Сквозные параметры

- **Часовой пояс:** Europe/Kyiv (все периоды статистики, расписание, таймстемпы).
- **Язык бота:** украинский (uk); тонкий слой строк `bot/texts/`.
- **FSM:** Redis (`bot/states.py`).
- **Расписание отделения:** dev-конфиг `work_schedule` (часы работы по дням
  недели, Europe/Kyiv); используется SLA-таймером и дежурством менеджера.
- **API-first:** бэкенд проектируется сервисами/репозиториями, отделёнными от
  хендлеров, чтобы будущий Mini App (WebApp) переиспользовал ту же логику.
