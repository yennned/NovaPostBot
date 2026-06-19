# Роадмап — процесс разработки и поэтапный план

## Git-гигиена

Сейчас `NovaPostBot` — НЕ git-репозиторий. Первый шаг — инициализация.

1. **Инициализация git + GitHub**
   - `git init`, ветка по умолчанию `main`.
   - **Публичный** репозиторий на GitHub (gh CLI), `origin`. Сознательно
     публичный, а не приватный: иначе CI (GitHub Actions) недоступен/ограничен на
     бесплатном тарифе. Безопасность держим на уровне секретов (Fernet-ключи НП в
     Postgres, `.env`/service-account вне git), а не на закрытости репозитория.
     Приватным **не делаем**.
   - Защита `main`: попадание только через Pull Request (branch protection +
     обязательный review + зелёный CI).

2. **.gitignore (до первого коммита)** — не коммитим: `.env`, `*.env` (кроме
   `.env.example`), `.venv/`/`venv/`, `__pycache__/`, `*.py[cod]`,
   `.pytest_cache/`/`.ruff_cache/`/`.mypy_cache/`, `*.db`/`*.sqlite3`/`*.dump`,
   `backups/`, скриншоты (`*.png`/`*.jpg`/`*.jpeg`/`*.webp`), **service-account
   `*.json`/credentials**, `.DS_Store`, `.idea/`/`.vscode/`,
   `.claude/settings.local.json`.

3. **Секреты и шифрование**
   - Секреты только в `.env` (в git — `.env.example` с пустыми значениями).
   - **Google service-account JSON** — в `.env`/секрет, НЕ в git.
   - Ключ НП каждого ФОП — Fernet, хранится **зашифрованным в Postgres**
     (`sender_profiles.np_api_key`). `FERNET_KEY` — из env.
   - Перед первым push: `git ls-files` не содержит `.env`, ключи, скриншоты,
     venv, дампы БД.
   - PII (телефон/ФІО) персонал видит **открыто** (≤20 клиентов); чувствительные
     действия — в `audit_logs`.

4. **Изоляция двух разработчиков (я + Степан)**
   - Каждый — свой **отдельный клон** репозитория.
   - Для параллельных задач у агента — git **worktree** на задачу.
   - Ветка на задачу: `feat/<owner>-<short>`, `fix/<owner>-<short>` от свежего
     `main`.

5. **Коммиты** — точечно (`git add <файлы>`, без `git add .`); маленькие
   осмысленные коммиты; conventional-стиль; в `main` — только squash-merge PR
   после ревью и зелёного CI.

6. **PROGRESS.md** — после **каждого локального коммита**: дата, ветка, хеш, что
   сделано, что дальше, открытые вопросы. Журнал, не BRD.

7. **CONTRIBUTING.md** — правила веток/PR/коммитов, gitignore-политика, секреты,
   как обновлять PROGRESS.md, как запускать линт/тесты.

8. **Качество** — `ruff` (lint+format) + `pre-commit`, `pytest` на чистой логике,
   GitHub Actions CI (pytest + ruff) как гейт для merge в `main`.

## Поэтапный план (Фазы 0–7)

Каждая фаза — одна/несколько веток-задач, мердж через PR.

- **Фаза 0 — Инфраструктура и процесс.** `git init` + GitHub + branch protection,
  `.gitignore`, `.env.example`, `PROGRESS.md`, `CONTRIBUTING.md`, ruff/pre-commit/
  CI, Dockerfile + docker-compose (bot/worker/redis), `config.py`,
  `logging_config.py`, подключение **managed Postgres Neon** (`DATABASE_URL`
  pooled + `DATABASE_URL_DIRECT` для Alembic; asyncpg `statement_cache_size=0` за
  пулером — см. [02-architecture.md](02-architecture.md)) + Alembic, заготовка
  Google service-account. Очистка устаревших доков.

- **Фаза 1 — Слой данных (Postgres) + каркас бота, авторизация, роли, dev
  god-mode.** `app/db/` (модели + репозитории), `app/sheets/` (только склад),
  RBAC (3 роли + per-flag), middleware, `/start` (телефон), bootstrap
  владельцев, рольовые меню (uk), dev-allowlist + `/as` + impersonation +
  kill-switch, unit-тесты.

- **Фаза 2 — Регистрация/подтверждение + управление клиентами.** Надёжное
  уведомление владельцу/менеджеру; рабочее подтверждение (статус меняется, кнопки
  исчезают); блокировка/удаление; список и карточка клиента (PII открыто).

- **Фаза 3 — Кабинет клиента (чтение) + остатки из Sheets.** Книга «Склад», чтение
  остатков; просмотр товаров (поиск/пагинация, без ручного добавления); статистика
  today/week/month + выбор дня + остаток. Приёмка (Google + Apps Script) вне бота.

- **Фаза 4 — Интеграция НП и создание ТТН.** `novaposhta/*`, ФОП/SenderProfile
  (ключ Fernet в Postgres), FSM создания ТТН (пресеты размеров, фіз/юр, платник/
  оплата/COD/страховка → поля НП, ценообразование онлайн) — NP-first → Shipment +
  резерв, отмена → возврат; «Відправлення» клиента и менеджера.

- **Фаза 5 — Уведомления, трекинг, возвраты/проблемы (воркер).** Матрица пушей
  по ролям, APScheduler-трекинг → статусы, списание в «Склад» при «відправлено»;
  возвраты/lost/damaged, «Повернення замовлення», low-stock; SLA-таймер
  (30 рабочих минут, триггер `dispatched`) + бесплатная обработка при промахе.

- **Фаза 6 — Поддержка и дежурство + персонал/аналитика.** Чат клиент↔дежурный,
  «я на зв'язку» (авто-снятие), очередь без дежурного → владельцу, лог переписок;
  сессии менеджера = смена; 👔 Персонал (per-flag права), 📊 Звіти/Аналітика —
  финотчёт (fee-формула) + список опоздавших ТТН.

- **Фаза 7 — Задел на CRM/WMS для склада.** За абстракцией `app/sheets/` —
  альтернативный источник (CRM/WMS REST) без изменения хендлеров/сервисов;
  переключение через конфиг. (Postgres — уже с Фазы 0.)

- **Задел: Mini App (WebApp)** — для тяжёлых экранов (форма ТТН, каталог товаров,
  дашборд аналитики); подключается при необходимости. Бэкенд уже API-first
  (сервисы/репозитории отделены от хендлеров — [02-architecture.md](02-architecture.md)),
  Mini App переиспользует ту же логику.

## Распределение задач по фазам (alex / step)

**Модель работы (актуально с 2026-06-18): sequential-by-phase.** Один человек
полностью закрывает фазу — и доменный слой, и bot/UI; второй **не начинает** свою
фазу, пока предыдущая не в `main`; следующий стартует от свежего `main`. Правила и
причина (активный писатель сейчас один) — в [CONTRIBUTING.md](../CONTRIBUTING.md).
Фолбэк при двух одновременных писателях — layer-split + контракт-первый (тоже в
CONTRIBUTING).

**Владельцы фаз:**

| Фаза | Владелец | Статус |
|------|----------|--------|
| 1 — данные+RBAC (alex) / каркас бота (step) | alex + step | ✅ в `main` |
| 2 — регистрация/подтверждение + клиенты | **alex** | ✅ в `main` (остался UI правки профиля) |
| 3 — кабинет клиента (чтение) + остатки | **step** | следующая |
| 4 — интеграция НП + создание ТТН | назначается перед стартом (по умолч. alex) | — |
| 5 — уведомления/трекинг/возвраты (воркер) | назначается (по умолч. step) | — |
| 6 — поддержка/персонал/аналитика | назначается (по умолч. alex) | — |
| 7 — задел CRM/WMS | назначается (по умолч. step) | — |

Ниже — **scope каждой фазы**: полный набор модулей, который делает её владелец
(оба слоя). Это чек-лист фазы, **не** разделение между людьми.

### Фаза 1 — данные+RBAC + каркас бота (✅ в `main`)
- Доменный слой (alex, Трек A): `db/models`, `db/repositories`, RBAC
  `bot/permissions`, bootstrap владельцев, Alembic.
- Bot/UI (step, Трек B): `dispatcher`/`middlewares`/`states`/`filters`,
  `handlers/start` (`/start`→контакт→гейтинг), `handlers/dev` (`/as`, impersonation,
  kill-switch), `keyboards/`+`texts/` (uk), `main.py`.

### Фаза 2 — Регистрация/подтверждение + клиенты (✅ alex, в `main`)
- Доменный слой: `repositories/user` (`list_by_status`/`count_by_status`),
  `services/clients` (подтверждение/блок/разблок/архив/восстановление, переходы,
  права, аудит), `services/exceptions`, `services/notifications`,
  `services/sender_profile` (backend-ready, без NP-валидации — она в Фазе 4).
- Bot/UI: `handlers/clients_manage` (список/вкладки/поиск/карточка/действия),
  `keyboards/clients`, `texts/clients`, `states.ClientManageState`,
  `notify.BotNotifier`, пуши при регистрации/подтверждении.
- **Остаток:** UI правки профиля клиента (ПІБ/телефон) — бэкенд готов
  (`clients.update_client_profile`).

### Фаза 3 — Кабинет клиента (чтение) + остатки (step)
- Доменный слой: `sheets/inventory` (read-only книга «Склад»), `services/inventory`
  (`available = stock − reserved`), `repositories/shipment`
  (`get_by_client_and_status`, `reserved_by_sku`), `services/stats` (окна
  today/week/month в Europe/Kyiv, net = відправлено − повернення − втрати).
- Bot/UI: `handlers/client_cabinet`, `keyboards/client`, тексты,
  `states.ClientCabinetState`.

### Фаза 4 — Интеграция НП + создание ТТН
- Доменный слой: `novaposhta/{client,methods,schemas,tracking,exceptions}`
  (справочники городов/відділень + кэш Redis, расчёт цены, валидация ключа),
  `services/shipment` (NP-first → Shipment + резерв, отмена → возврат), NP-валидация
  ключа в `services/sender_profile`.
- Bot/UI: `handlers/ttn` (FSM пресеты/фіз-юр/платник/COD/страховка),
  `handlers/shipment` (відправлення), `keyboards/ttn`+`shipment`, `texts/ttn`,
  `states.TtnForm`.
- **Реализовано (Express-картка):** PR 8 (composition root) → 9a–9d (кошик →
  параметри → отримувач → адреса → картка з ціною/правкою/COD → ✅ Відправити) +
  NP-aware «Скасувати». 225 тестов, всё в `main`.
- **Отложено (стаб, PR 9e — опц.):** «Останні отримувачі» — 1 тап подставляет
  name/phone/kind/edrpou из прошлых ТТН (місто/відділення — заново, в БД нет ref).
  Точка подключения помечена `TODO (PR 9e)` в `keyboards/ttn.build_recipient_kind_kb`.
- **Перед боевым запуском:** задать в `.env` `NP_SENDER_CITY_REF` +
  `NP_SENDER_WAREHOUSE_REF` (Ref нашего склада-отправителя в справочнике НП) —
  иначе расчёт цены/створення ТТН вернут «недоступно». E2E к реальному НП — при
  наличии ключа/песочницы (юнит-тесты идут на `MockTransport`, без сети).

### Фаза 5 — Уведомления, трекинг, возвраты (воркер)
- Доменный слой: `worker.py` + `jobs.py` (APScheduler-поллинг НП статусов,
  low-stock), `utils/sla` (30 раб. минут, Europe/Kyiv), `services/tracking`
  (списание в «Склад» при «відправлено», SLA-флаги), `services/returns`
  (returned/lost/damaged → движения остатка), `services/notifications` (матрица
  пушей по ролям/статусам), `repositories/stock_movement` + `notification_settings`.
- Bot/UI: `handlers/returns` («Повернення замовлення»), `handlers/shipment`
  (SLA-индикатор), `keyboards/returns`, `texts/notifications`, UI настроек
  уведомлений, `states.ReturnForm`.

### Фаза 6 — Поддержка/дежурство + персонал/аналитика
- Доменный слой: `services/support` (маршрутизация дежурному, очередь без дежурного
  → владельцу, лог переписок, авто-снятие), `repositories/support`, `models/support`,
  `utils/work_schedule`, `services/reports` (fee-формула, опоздавшие ТТН, сводки),
  `repositories/reports`, `services/staff` (per-flag права + аудит).
- Bot/UI: `handlers/{support,manager,staff,analytics}`,
  `keyboards/{support,staff,analytics}`, тексты,
  `states.{SupportForm,StaffForm,AnalyticsForm}`, per-flag-гейтинг.

### Фаза 7 — Задел CRM/WMS (маленькая, неделимая)
- Абстракция `app/sheets/` → `Protocol StockSource` + `GoogleSheetsStockSource` +
  заглушка `CrmStockSource`; переключатель `INVENTORY_SOURCE` в `config`.
  Хендлеры/сервисы уже через интерфейс — не меняются.

## Проверка (end-to-end)

- **Локально (Docker):** `docker compose up -d --build` → `bot`, `worker`, `redis`;
  Postgres — Neon по `DATABASE_URL`; Sheets — service-account из `.env`; миграции
  `alembic upgrade head`. Логи: `docker compose logs -f bot`.
- **Данные:** Postgres (миграции создают схему) + тестовые книги Google Sheets
  «Склад» и «Приймання» (лист на клиента) с Apps Script.
- **Сценарии в Telegram:** новый клиент `/start` → уведомление → подтверждение
  меняет статус и убирает кнопки → блокировка закрывает доступ; dev `/as manager`;
  kill-switch требует второго dev; статистика today/week/month + день; создание
  ТТН → резерв; отмена возвращает; «відправлено» по трекингу → списание в «Склад»;
  возврат → «Повернення замовлення».
- **Тесты:** `pytest -q` (permissions, validators, stats-окна, inventory ledger,
  поля ТТН → НП). CI — гейт для merge.
- **Git-гигиена:** `git ls-files | grep -iE '\.env$|\.png|\.jpe?g|venv|\.db'` →
  только `.env.example`; `main` недоступен для прямого push.
