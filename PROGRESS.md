# PROGRESS — журнал разработки NovaPostBot

Журнал ведётся **после каждого локального коммита**: дата, ветка, хеш, что
сделано, что дальше, открытые вопросы. Это не BRD — декомпозиция задач в
[`docs/ROADMAP.md`](docs/ROADMAP.md) и в плане.

Формат записи:

```
## YYYY-MM-DD · <ветка> · <короткий хеш>
- Сделано: …
- Дальше: …
- Открытые вопросы: …
```

---

## 2026-06-18 · feat/alex-clients · смена модели работы
- **Решение:** перешли на **sequential-by-phase** (последовательно по фазам, не
  параллельно по слоям). Один владелец на фазу (backend+UI), второй ждёт мержа.
  **Phase 2 → alex целиком, Phase 3 → step целиком.** Причина: активный писатель
  сейчас один (Степан разгоняется) → throughput-издержка ≈ 0, плюсы (всегда
  рабочий `main`, нет дрейфа контракта, WIP=1) перевешивают. Принято после council
  (3/4 за гибрид, но выбран sequential осознанно). Триггер возврата к layer-split —
  второй одновременный писатель / дедлайн на 2×. Зафиксировано в
  [CONTRIBUTING.md](CONTRIBUTING.md) и [docs/ROADMAP.md](docs/ROADMAP.md). Контракт
  Фазы 2 (ниже) = backend-половина, alex доводит фазу до UI и мержит.
- **Открытые вопросы:** нет.

## 2026-06-18 · feat/alex-clients · 60d8956
- **Сделано:** **контракт Фазы 2** (слой alex, контракт-первый). `services/clients.py`
  — доменный API управления клиентами (list/card/approve/block/unblock/archive/
  restore/update_profile), frozen-структуры `ClientListItem`/`ClientPage`/
  `ClientCard`, карта переходов статусов, проверки `can_manage` + per-flag
  (`can_manage_clients`/`can_edit_clients`), аудит. `services/exceptions.py`
  (`ClientServiceError` → NotFound/PermissionDenied/TransitionForbidden/
  AlreadyInStatus). `services/notifications.py` — `Notifier`-протокол +
  `notify_new_client_registered` (владельцам+дежурным) / `notify_client_approved`,
  uk-тексты backend-owned. `repositories/user.py`: `list_by_status`
  (фильтр/поиск/пагинация) + `count_by_status`. Бриф Степану —
  `docs/phase2-stepan-brief.md`. Тесты на Postgres + mock Notifier — полный сьют
  зелёный, ruff чист.
- **Дальше:** контракт-PR в `main` первым; Степан ветвится от `main` и пишет
  bot-layer Фазы 2 по брифу. Параллельно `feat/alex-senders` — sender_profile
  backend-ready.
- **Открытые вопросы:** мусорные дубликаты « 2.py» в worktree (артефакт
  файл-синка) — почистить, в git не коммитим.

## 2026-06-17 · main · c3e3fb0
- **Сделано:** смержен **Track B / step / Phase 1 bot-auth** через PR
  [#5](https://github.com/yennned/NovaPostBot/pull/5). В `main` вошли bot-layer
  (`app/bot/`), wiring в `app/main.py`, `/start` с auth-гейтингом по
  `pending/active/blocked/archived`, dev-команды `/as`, `/as_user`,
  `/kill_switch`, role-based меню и focused-тесты bot-слоя. Перед merge ветка
  была перебазирована на актуальный `main`; отдельно закрыт баг с enum-статусами
  и расширен гейтинг контакта только на auth-state.
- **Дальше:** идти в следующий продуктовый кусок поверх Phase 1: owner/manager
  approval-flow для новых `pending`-клиентов, push-уведомления и переход dev-state
  из in-memory в постоянное хранилище.
- **Открытые вопросы:** runtime-хранилище dev-контекста и kill-switch state
  (FSM/Redis/БД) ещё не выбрано.

## 2026-06-17 · feat/step-phase1-bot-auth · e9a8e2c
- **Сделано:** старт трека **step / Phase 1 bot-auth**. Добавлен каркас
  `app/bot/` (dispatcher, middlewares, filters, states, handlers, keyboards,
  texts), реализованы `/start` + запрос контакта + создание `pending`-клиента,
  dev-команды `/as`, `/as_user`, `/kill_switch`, role-based меню и wiring в
  `app/main.py`. После мержа трека A bot-layer переведён на реальные
  `User`/`UserRole`/репозитории из data-layer; на in-memory пока оставлено только
  dev-state для impersonation/kill-switch. Покрыто focused-тестами на
  start/dev/effective context.
- **Дальше:** добрать DB-зависимые участки flow и заменить in-memory dev-state на
  постоянное хранилище (Redis/БД), когда будет согласован final runtime-контур.
- **Открытые вопросы:** где хранить dev-контекст и kill-switch state до появления
  постоянного Redis/FSM-контура.

## Фаза 1 — распределение задач

Два трека по границе «данные/правила» ↔ «бот/диалог»: **`alex` — данные + RBAC-ядро
(одна неделимая задача, одна ветка), `step` — каркас бота + auth.** Внутри трека A
порядок последовательный (permissions импортирует `enums`/`User`), поэтому это
**один worktree и один PR**, без дробления. **Трек A мержится первым** (фундамент);
трек B импортирует `enums`/`User`, поэтому ребейзится на свежий `main` после мержа
A. **Один коммиттер на ветку.** Подробности — в [CONTRIBUTING.md](CONTRIBUTING.md).

### Трек A — `alex`: данные + RBAC-ядро · `feat/alex-phase1-db`
- [x] `app/db/models/` — `enums` (роли `client<manager<owner`, статусы), `user`
      (role, status, phone, permissions JSONB), `sender_profile` (ФОП,
      `np_api_key` Fernet), `audit`.
- [x] `app/db/repositories/` — `user`, `sender_profile`, `audit`.
- [x] Alembic — начальная миграция схемы (`migrations/versions/`).
- [x] `app/sheets/client.py` — read-only скелет клиента Sheets (каркас).
- [x] `app/bot/permissions.py` — иерархия ролей, `can_manage(actor, target)`,
      per-flag `has_permission(user, flag)`, dev-allowlist проверяется первой.
- [x] bootstrap владельцев из `OWNER_TELEGRAM_IDS` (`app/services/bootstrap.py`).
- [x] `tests/` — репозитории + crypto + permissions + bootstrap (на реальном Postgres).

### Трек B — `step`: каркас бота + auth + меню + dev god-mode · `feat/step-phase1-bot-auth`
- [x] `app/bot/dispatcher.py`, `middlewares.py` (inject session/user +
      «эффективная роль/пользователь» из dev-контекста), `states.py`, `filters.py`.
- [x] `app/bot/handlers/start.py` — `/start` → `request_contact` →
      создание/поиск user, гейтинг `pending`/`active`/`blocked`.
- [x] `app/bot/handlers/dev.py` — `/as client|manager|owner`, impersonation,
      kill-switch (two-man rule, окна 1ч/3ч), audit (`dev_*`).
- [x] `app/bot/keyboards/` + `texts/` — рольовые меню (uk) для client/manager/owner.
- [x] `app/main.py` — сборка и запуск (long polling).
- [x] `tests/` — middleware/эффективная роль, логика `/start`, two-man rule.

---

## 2026-06-18 · fix/alex-phase1-hardening · d938267
- **Сделано:** хардениг по итогам ревью кода Трека A (баги, не стиль).
  (1) `get_settings()` обёрнут в `@lru_cache` — раньше конструировался новый
  `Settings()` (чтение `.env` + парс ID) на каждый вызов, а он в горячем пути
  `is_dev`/`can_manage`/`has_permission` (на каждый апдейт Telegram). В тестах кеш
  сбрасывается autouse-фикстурой `_clear_settings_cache` (`get_settings.cache_clear()`).
  (2) bootstrap-аудит: `user_id=None` (системное действие — актора нет; раньше
  новый владелец писался актором собственного создания). (3) `crypto.decrypt()`
  оборачивает `InvalidToken` в доменное `DecryptionError` — чтобы битый/ротированный
  `FERNET_KEY` не ронял загрузку ORM в Фазе 2/4. Тесты: кеш `get_settings`,
  `user_id IS NULL` в bootstrap, `DecryptionError` на битом токене. **25 passed**,
  ruff чист.
- **Дальше:** распределение Фаз 2–7 зафиксировано в
  [docs/ROADMAP.md](docs/ROADMAP.md) («Распределение задач по фазам»). Старт
  параллельных треков: Степан — Трек B (каркас бота), я — два worktree Фазы 3
  (склад/остатки и отправления/статистика).
- **Открытые вопросы:** нет.

## 2026-06-17 · feat/alex-phase1-db · a8847c3 (RBAC-часть трека A)
- **Сделано:** **RBAC-ядро Фазы 1**. `app/bot/permissions.py` (чистая логика, без
  aiogram/БД — переиспользуемо для WebApp): `role_at_least`, `can_manage`
  (строго сверху вниз; менеджеры друг другом не управляют; собой нельзя),
  `has_permission` (per-flag, по умолчанию включено, owner/dev — всё),
  dev-allowlist (`DEV_TELEGRAM_IDS`) проверяется **первым**. Bootstrap владельцев
  `app/services/bootstrap.ensure_owners` (создаёт/повышает/активирует из
  `OWNER_TELEGRAM_IDS`, пишет в `audit_logs`, идемпотентно). Тесты permissions +
  bootstrap — всего 23 passed, ruff чист. **Трек A закрыт целиком (данные + RBAC).**
- **Дальше:** PR трека A в `main`; затем `step` ребейзится и стартует трек B.
- **Открытые вопросы:** нет.

## 2026-06-17 · feat/alex-phase1-db · 0e0e98f (DB-часть трека A)
- **Сделано:** **слой данных Фазы 1**. База: `Base.metadata` с naming_convention,
  `app/db/mixins.py` (UUID PK через `uuid4`, таймстемпы), `app/utils/crypto.py`
  (Fernet поверх `FERNET_KEY`), `app/db/types.EncryptedString` (прозрачный шифр
  ключа НП). Модели: `enums` (`UserRole`/`UserStatus`/`OrgType` — `StrEnum`,
  нативные PG-enum), `User`, `SenderProfile`, `AuditLog`. Репозитории:
  `user`/`sender_profile`/`audit` (тонкий слой над `AsyncSession`,
  эксклюзивный `set_default`). Начальная Alembic-миграция (проверены
  upgrade→downgrade→upgrade и `alembic check`; явный DROP TYPE для enum в
  downgrade). Read-only скелет `app/sheets/client.py`. Тесты на **реальном
  Postgres** (`conftest` с per-test rollback) + crypto — 11 passed, ruff чист.
  CI: postgres-service; docker-compose: профиль `dev` с локальным postgres.
- **Дальше:** RBAC-часть трека A — `app/bot/permissions.py` (иерархия,
  `can_manage`, per-flag права, dev-allowlist первым), bootstrap владельцев из
  `OWNER_TELEGRAM_IDS`, тесты permissions. Затем PR трека A в `main`.
- **Открытые вопросы:** нет.

## 2026-06-14 · feat/alex-phase0-infra · 776f15b
- **Сделано:** старт **Фазы 0**. `git init` (main). Каркас инфраструктуры:
  `.gitignore`, `.dockerignore`, `.env.example`, `CONTRIBUTING.md`, этот журнал;
  тулинг (`pyproject.toml` — ruff+pytest, `.pre-commit-config.yaml`,
  `requirements*.txt`); CI (`.github/workflows/ci.yml` — ruff+pytest); Docker
  (`Dockerfile`, `docker-compose.yml` — redis/migrate/bot/worker); каркас
  приложения (`app/config.py` — pydantic-settings, `app/logging_config.py` —
  structlog, `app/db/base.py` — async engine с `statement_cache_size=0`);
  alembic-скелет (`alembic.ini`, `migrations/env.py`); юнит-тест конфигурации.
- **Дальше:** деплой на GitHub (приватный репозиторий, защита `main`, PR),
  затем **Фаза 1** (модели БД, RBAC, `/start`, рольовые меню, dev god-mode).
- **Открытые вопросы:** имя/владелец GitHub-репозитория; для Фазы 4 — сверить
  COD-тариф НП и точный набор обязательных полей `InternetDocument.save`.
