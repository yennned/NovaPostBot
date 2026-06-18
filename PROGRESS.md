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
- [ ] `app/bot/dispatcher.py`, `middlewares.py` (inject session/user +
      «эффективная роль/пользователь» из dev-контекста), `states.py`, `filters.py`.
- [ ] `app/bot/handlers/start.py` — `/start` → `request_contact` →
      создание/поиск user, гейтинг `pending`/`active`/`blocked`.
- [ ] `app/bot/handlers/dev.py` — `/as client|manager|owner`, impersonation,
      kill-switch (two-man rule, окна 1ч/3ч), audit (`dev_*`).
- [ ] `app/bot/keyboards/` + `texts/` — рольовые меню (uk) для client/manager/owner.
- [ ] `app/main.py` — сборка и запуск (long polling).
- [ ] `tests/` — middleware/эффективная роль, логика `/start`, two-man rule.

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
