# Роадмап — процесс разработки и поэтапный план

## Текущий статус

- **Фазы 0–7 закрыты, продукт работает в проде.** Бот развёрнут на Hetzner VPS
  (bot + worker + Redis в Docker), БД — managed Neon.
- **Деплой автоматический:** merge в `main` → CI (`lint-test`, `migration-smoke`) →
  сборка образа в GHCR (`:latest` + `:sha-<short>`) → SSH-деплой на VPS
  (`docker compose pull && up -d --no-build`). Вехи — теги `vX.Y.Z`; есть
  `rollback.yml`. Детали — [CONTRIBUTING.md](../CONTRIBUTING.md).
- **Один бот на проект** (с 2026-07-31): тест-бот и staging упразднены, локально
  `BOT_TOKEN` пустой, единственный гейт перед продом — тесты и CI.
- Phase 7 закрыла seam склада: `StockSource` + Google Sheets adapter +
  `CrmStockSource` stub + переключение `INVENTORY_SOURCE`.

### После фаз 0–7 (работа задачами, не фазами)

Крупные направления, доехавшие в `main` уже после закрытия фаз:

- **Бизнес-аккаунты и команды:** `client_accounts` + `client_account_memberships`
  (`account_owner` / `employee`); ФОП, ТТН, склад, поддержка и статистика скоупятся
  по `account_id`, у ТТН отдельно `created_by_user_id`.
- **Физическое удаление** клиента/менеджера (вместо мягкого архива), история
  сохраняется через `SET NULL` на авторе.
- **Эксплуатация:** политика одного бота, CI/CD + GHCR + rollback, `/version`,
  версия сборки в логе старта.
- **Живая проверка:** харнесс настоящих aiogram-`Update` по всем ролям и пробники
  `scripts/e2e/` (в т.ч. `prod_health` по проду).
- **Хардening по итогам боевых прогонов:** кэш чтений Sheets (квота Google),
  видимость сбоев склада, корректная отмена удалённой в НП ТТН, объёмный вес в
  оценке стоимости, минимальный вес места.

Текущий журнал работ — [PROGRESS.md](../PROGRESS.md) (обновляется после каждого
коммита; по фактическому состоянию он достовернее этого файла).

## Git-гигиена

1. **GitHub + protected `main`**
   - Репозиторий уже подключён к `origin`, основная ветка — `main`.
   - Репозиторий остаётся **публичным**: CI и внешняя проверка важнее скрытности,
     а безопасность держим секретами и шифрованием, а не приватностью кода.
   - Целевой процесс для `main`: попадание только через Pull Request, зелёный CI,
     линейная история.

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

8. **Качество** — `ruff` (lint+format) + `pre-commit`, `pytest` на **реальном
   Postgres** (не sqlite), GitHub Actions CI (layer-check, ruff, compileall, pytest,
   сборка образа) как гейт для merge в `main`.

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
  владельцев, рольовые меню (uk), dev-allowlist + `/as` + impersonation,
  unit-тесты.

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

## Распределение задач по фазам (исторический раздел)

> **Все фазы закрыты; раздел оставлен как история.** Работа давно идёт отдельными
> задачами, а не фазами, и фактически ведётся одним активным писателем. Модель
> sequential-by-phase (один человек закрывает фазу целиком, второй ждёт мержа)
> остаётся действующим правилом на случай возврата к двум писателям — она и её
> фолбэк layer-split описаны в [CONTRIBUTING.md](../CONTRIBUTING.md).

**Владельцы фаз:**

| Фаза | Владелец | Статус |
|------|----------|--------|
| 1 — данные+RBAC (alex) / каркас бота (step) | alex + step | ✅ в `main` |
| 2 — регистрация/подтверждение + клиенты | **alex** | ✅ в `main` |
| 3 — кабинет клиента (чтение) + остатки | **step** | ✅ в `main` |
| 4 — интеграция НП + создание ТТН | **alex** | ✅ в `main` |
| 5 — уведомления/трекинг/возвраты (воркер) | **step** | ✅ в `main` |
| 6 — поддержка/персонал/аналитика | **alex** | ✅ в `main` |
| 7 — задел CRM/WMS | **step** | ✅ в `main` |

Ниже — **scope каждой фазы**: полный набор модулей, который делает её владелец
(оба слоя). Это чек-лист фазы, **не** разделение между людьми.

### Фаза 1 — данные+RBAC + каркас бота (✅ в `main`)
- Доменный слой (alex, Трек A): `db/models`, `db/repositories`, RBAC
  `bot/permissions`, bootstrap владельцев, Alembic.
- Bot/UI (step, Трек B): `dispatcher`/`middlewares`/`states`/`filters`,
  `handlers/start` (`/start`→контакт→гейтинг), `handlers/dev` (`/as`,
  impersonation), `keyboards/`+`texts/` (uk), `main.py`.

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

### Фаза 3 — Кабинет клиента (чтение) + остатки (✅ step, в `main`)
- Доменный слой: `sheets/inventory` (read-only книга «Склад»), `services/inventory`
  (`available = stock − reserved`), `repositories/shipment`
  (`get_by_client_and_status`, `reserved_by_sku`), `services/stats` (окна
  today/week/month в Europe/Kyiv, net = відправлено − повернення − втрати).
- Bot/UI: `handlers/client_cabinet`, `keyboards/client`, тексты,
  `states.ClientCabinetState`.

### Фаза 4 — Интеграция НП + создание ТТН (✅ alex, в `main`)
- Доменный слой: `novaposhta/{client,methods,schemas,tracking,exceptions}`
  (справочники городов/відділень + кэш Redis, расчёт цены, валидация ключа),
  `services/shipment` (NP-first → Shipment + резерв, отмена → возврат), NP-валидация
  ключа в `services/sender_profile`.
- Bot/UI: `handlers/ttn` (FSM пресеты/фіз-юр/платник/COD/страховка),
  `handlers/shipment` (відправлення), `keyboards/ttn`+`shipment`, `texts/ttn`,
  `states.TtnForm`.
- **Реализовано (Express-картка):** PR 8 (composition root) → 9a–9d (кошик →
  параметри → отримувач → адреса → картка з ціною/правкою/COD → ✅ Відправити) +
  NP-aware «Скасувати».
- **Hardening follow-up (#32, #33):** гейт полноты данных отправителя перед ТТН
  (`ensure_sender_dispatchable`: contact/phone/склад, не только `np_sender_ref`) +
  обязательный `sender_phone` при сохранении профиля + стойкость Redis-кэша
  справочников к падению Redis (fallback к loader) + dispatcher-level backstop на
  `DecryptionError` (ротация `FERNET_KEY`). Всё в `main`.
- **Обязательная настройка прода:** в `.env` заданы `NP_SENDER_CITY_REF` +
  `NP_SENDER_WAREHOUSE_REF` (Ref нашего склада-отправителя в справочнике НП) —
  без них расчёт цены и створення ТТН вернут «недоступно». Юнит-тесты идут на
  `httpx.MockTransport`, без сети; живые вызовы — только через `scripts/e2e/`.

### Фаза 5 — Уведомления, трекинг, возвраты (✅ step, в `main`)
- Доменный слой: `worker.py` + `jobs.py` (APScheduler-поллинг НП статусов,
  low-stock), `utils/sla` (30 раб. минут, Europe/Kyiv), `services/tracking`
  (списание в «Склад» при «відправлено», SLA-флаги), `services/returns`
  (returned/lost/damaged → движения остатка), `services/notifications` (матрица
  пушей по ролям/статусам), `repositories/{stock_movement,notification_setting,
  low_stock_alert}`.
- Manager/UI: `services/manager_shipments`, `handlers/manager_shipments`,
  `keyboards/texts/manager_shipments` — полная очередь отправлений менеджера,
  SLA-карточки, ручные `lost/damaged`, приёмка возврата.
- Возвраты: per-item inspection перед приёмкой возврата, раздельно `на склад` /
  `брак`, audit по accepted/rejected quantities.
- Клиентские настройки: UI персональных уведомлений и low-stock anti-spam с
  persisted state, чтобы воркер не спамил одинаковыми алертами на каждом цикле.

### Фаза 6 — Поддержка/дежурство + персонал/аналитика (✅ alex, в `main`)
- Доменный слой: `services/support` (маршрутизация дежурному, очередь без дежурного
  → владельцу, лог переписок), `services/duty` (смена + авто-снятие воркером),
  `repositories/support`, `repositories/reports`, `models/support`,
  `utils/work_schedule`, `services/reports` (fee-итоги, опоздавшие ТТН, сводки),
  `services/staff` (per-flag права + аудит).
- Bot/UI: `handlers/{duty,support,staff,reports,analytics}`,
  `keyboards/{support,staff,reports}`, тексты, `states.{SupportState,StaffState}`,
  per-flag-гейтинг (`can_handle_support`/`can_view_reports`).
- **Реализовано (PR 6a–6e, #37–#41):** 6a — модели `SupportThread/SupportMessage` +
  `users.duty_since` + `utils/work_schedule` (вынесено из `utils/sla`) + реестр прав
  `permissions.PERMISSION_FLAGS`; 6b — дежурство «🟢 Я на зв'язку» + worker-job
  авто-снятия (`DUTY_CHECK_SECONDS`); 6c — релей-чат клиент↔дежурный + инбокс/лог +
  очередь без дежурного → владельцу; 6d — 👔 Персонал (найм/права/блок/снятие роли,
  всё в `audit_logs`); 6e — 📊 Звіти/📈 Аналітика (сводки по клиентам, fee + опоздавшие
  ТТН, поддержка по менеджерам).
- **Отложено (TODO):** аттрибуция ТТН по менеджерам (нет `manager_id` у `shipments`),
  сводка склада в отчётах (зависит от Sheets), произвольный диапазон дат и графики.

### Фаза 7 — Задел CRM/WMS (маленькая, неделимая)
- Абстракция `app/sheets/` → `Protocol StockSource` + `GoogleSheetsStockSource` +
  заглушка `CrmStockSource`; переключатель `INVENTORY_SOURCE` в `config`.
  Хендлеры/сервисы уже через интерфейс — не меняются.
- **Реализовано:** `app/sheets/source.py` (контракт + типы), `build_stock_source`
  в `app/sheets/__init__.py`, перевод дефолтной инициализации read/write путей
  (`services/inventory`, `tracking`, `returns`, `worker`) на конфигурируемый
  источник, регрессионные тесты на factory/config/stub.

## Что осознанно не реализовано

Не баги, а сознательно отложенное. Держим списком здесь, чтобы это не выглядело в
документах как работающая функция:

- **«Останні отримувачі»** (PR 9e) — 1 тап подставляет name/phone/kind/edrpou из
  прошлых ТТН (місто/відділення — заново, в БД нет ref). Точка подключения помечена
  `TODO (PR 9e)` в `keyboards/ttn.build_recipient_kind_kb`.
- **«Власні розміри» коробки** и пресет «Документи» — в UI только три пресета
  (Мала/Середня/Велика) с фиксированными габаритами.
- **Валидация оголошеної (страхової) суми** — сейчас при нетронутом поле в НП
  уходит `0`; проверок «> 0 и ≥ минимума НП» нет.
- **`OptionsSeat` в оценке цены** — `getDocumentPrice` его принимает (проверено
  боем), но мы намеренно считаем объёмный вес локально: оценка не должна зависеть
  от того, сохранит ли НП поддержку этого поля в методе расчёта.
- **Габариты не персистятся** — в `shipments.size_preset` лежит только текстовая
  метка пресета, поэтому по БД нельзя восстановить, какие ДхШхВ ушли в НП по
  конкретной ТТН (расследовать расхождение цены задним числом не выйдет).
- **Аттрибуция ТТН по менеджерам** — у `shipments` нет `manager_id`, отчёт «по
  менеджерам» опирается на поддержку и дежурство.
- **Сводка склада в отчётах**, произвольный диапазон дат и графики в аналитике.
- **Реальный CRM/WMS REST adapter** поверх seam `StockSource` (сейчас — Sheets +
  заглушка `CrmStockSource`).
- **Mini App (WebApp)** — бэкенд уже API-first, подключается при необходимости.

## Проверка (end-to-end)

- **Локально (Docker):** `docker compose up -d --build` → `migrate`, `worker`,
  `redis`; Postgres — Neon по `DATABASE_URL`. Локальный контейнер Postgres объявлен
  под профилем `dev` и обычным `up` **не поднимается** — нужен явный
  `docker compose --profile dev up -d --build`. Sheets — service-account из `.env`.
  Логи: `docker compose logs -f worker`.
  **Бот локально не поллит:** у проекта один боевой токен, `BOT_TOKEN` в локальном
  `.env` пустой намеренно (гард в `app/main.py`) — два процесса на одном токене
  дают `409 Conflict` и перехват сообщений реальных клиентов.
- **Гейт перед продом — тесты:** `pytest -q` на реальном Postgres (permissions,
  окна статистики, inventory ledger, поля ТТН → НП, живые aiogram-`Update` по всем
  ролям через настоящий `build_dispatcher`). CI гоняет их же + `ruff check`,
  `ruff format --check`, layer-check, `compileall` и сборку образа.
- **Живая проверка на реальном НП** — `scripts/e2e/` (см. `scripts/e2e/README.md`):
  `preflight` (валидация ключей ФОП, только чтение) → `cascade` (создаёт настоящие
  ТТН под жёстким межпроцессным бюджетом документов) → `validate` (сверка с НП и
  уборка через `InternetDocument.delete`). Отдельно `prod_health` — пробник живости
  прода по Telegram.
- **Git-гигиена:** `git ls-files | grep -iE '\.env$|\.png|\.jpe?g|venv|\.db'` →
  только `.env.example`; `main` недоступен для прямого push.
