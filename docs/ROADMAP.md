# Роадмап — процесс разработки и поэтапный план

## Git-гигиена

Сейчас `NovaPostBot` — НЕ git-репозиторий. Первый шаг — инициализация.

1. **Инициализация git + GitHub**
   - `git init`, ветка по умолчанию `main`.
   - Приватный репозиторий на GitHub (gh CLI), `origin`.
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

**Принцип изоляции (один на все фазы):**

- **Граница потоков:** `step` — бот/диалог (всё под `app/bot/*`: хендлеры,
  клавиатуры, тексты, FSM-состояния), плюс **владеет общей инфраструктурой**
  (`dispatcher.py`, `middlewares.py`, `states.py`, `filters.py`, `permissions.py`,
  `main.py`). `alex` — данные/правила: pure-логика без aiogram (`services/`,
  `repositories/`, `novaposhta/`, `sheets/`, `worker.py`, `jobs.py`, `utils/`).
- **`alex` никогда не редактирует `app/bot/*`** → нулевой конфликт с веткой Степана.
- **Моя задача каждой фазы делится на 2 изолированных worktree** (две IDE) по
  непересекающимся доменам файлов. Связь между worktree и с ботом — через
  **инъекцию интерфейсов** (`Protocol`), сигнатуру согласуем заранее, реализуем
  независимо. Wiring (подключение сервисов к хендлерам) — отдельная интеграционная
  задача после мержа.
- Ветки: `feat/<owner>-<short>`, один коммиттер на ветку, PR в защищённый `main`,
  `PROGRESS.md` после каждого коммита. Worktree: `git worktree add ../<dir> <branch> main`.

> Трек B Фазы 1 (каркас бота) — фундамент на критическом пути, мержится первым;
> мои pure-логические треки от него на этапе реализации не зависят
> (тестируются юнитами), wiring — после.

### Фаза 1B — Степан (фундамент)
- **step** `feat/step-phase1-bot-auth`: `dispatcher`/`middlewares`/`states`/`filters`,
  `handlers/start` (`/start`→контакт→гейтинг), `handlers/dev` (`/as`, impersonation,
  kill-switch two-man rule), `keyboards/`+`texts/` (uk), `main.py`.
- **alex**: на этой фазе пишет pure-логику Фазы 3 (см. ниже) параллельно.

### Фаза 2 — Регистрация/подтверждение + клиенты
- **step**: `handlers/clients_manage` (список/карточка/подтверждение/блок/архив),
  `keyboards/manager`+`owner`, тексты; `states.ClientManageState`.
- **alex WT1 «клиенты-данные»**: `repositories/user` (`get_by_status`,
  `update_status`, `soft_delete`, `update_profile`), `services/clients`
  (правила подтверждения/блокировки/архивации + аудит).
- **alex WT2 «ФОП + уведомления»**: `repositories/sender_profile` (CRUD),
  `services/sender_profile` (encrypt/store/выбор дефолта),
  `services/notifications` (скелет: push владельцу при регистрации, клиенту при
  подтверждении; дедуп; отправитель инъекцией).

### Фаза 3 — Кабинет клиента (чтение) + остатки
- **step**: `handlers/client_cabinet`, `keyboards/client`, тексты;
  `states.ClientCabinetState`.
- **alex WT1 «склад→доступно»**: `sheets/inventory` (read-only книга «Склад»),
  `services/inventory` (`available = stock − reserved`; `reserved` инъекцией).
- **alex WT2 «отправления+статистика»**: `repositories/shipment`
  (`get_by_client_and_status`, `reserved_by_sku`), `services/stats` (окна
  today/week/month в Europe/Kyiv, net = відправлено − повернення − втрати).

### Фаза 4 — Интеграция НП + создание ТТН
- **step**: `handlers/ttn` (FSM пресеты/фіз-юр/платник/COD/страховка),
  `handlers/shipment` (відправлення client/manager), `keyboards/ttn`+`shipment`,
  `texts/ttn`; `states.TtnForm`.
- **alex WT1 «НП-клиент»**: `novaposhta/{client,methods,schemas,tracking,exceptions}`
  — async-клиент API, справочники городов/відділень (кэш Redis), расчёт цены,
  валидация ключа.
- **alex WT2 «ТТН-сервис + резерв»**: `services/shipment` (NP-first → Shipment +
  резерв в PG, отмена → возврат резерва), `repositories/shipment`, NP-валидация
  ключа в `services/sender_profile`. Контракт с WT1 — `Protocol` NP-клиента.

### Фаза 5 — Уведомления, трекинг, возвраты (воркер)
- **step**: `handlers/returns` («Повернення замовлення»), `handlers/shipment`
  (SLA-индикатор), `keyboards/returns`, `texts/notifications`, UI настроек
  уведомлений; `states.ReturnForm`.
- **alex WT1 «трекинг-воркер»**: `worker.py` + `jobs.py` (APScheduler-поллинг НП
  статусов, low-stock), `utils/sla` (30 раб. минут, Europe/Kyiv), `services/tracking`
  (списание в «Склад» при «відправлено», SLA-флаги).
- **alex WT2 «уведомления+возвраты»**: `services/notifications` (матрица пушей по
  ролям/статусам), `services/returns` (returned/lost/damaged → движения остатка),
  `repositories/stock_movement` + `repositories/notification_settings`.

### Фаза 6 — Поддержка/дежурство + персонал/аналитика
- **step**: `handlers/{support,manager,staff,analytics}`,
  `keyboards/{support,staff,analytics}`, тексты;
  `states.{SupportForm,StaffForm,AnalyticsForm}`; per-flag-гейтинг в хендлерах.
- **alex WT1 «поддержка+дежурство»**: `services/support` (маршрутизация дежурному,
  очередь без дежурного → владельцу, лог переписок, авто-снятие),
  `repositories/support`, `models/support` (доп. поля), `utils/work_schedule`
  (ротация/смена менеджера).
- **alex WT2 «аналитика+персонал»**: `services/reports` (fee-формула, список
  опоздавших ТТН, сводки), `repositories/reports`, `services/staff` (управление
  per-flag правами + аудит).

### Фаза 7 — Задел CRM/WMS (одна задача, без дробления)
- **alex** (один worktree): абстракция `app/sheets/` → `Protocol StockSource` +
  `GoogleSheetsStockSource` + заглушка `CrmStockSource`; переключатель
  `INVENTORY_SOURCE` в `config`. Хендлеры/сервисы уже через интерфейс — не меняются.
- **step**: изменений нет (бот работает через сервисный слой).

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
