# CONTRIBUTING — правила разработки NovaPostBot

Для нас двоих (я + Степан). Цель — чистый процесс: защищённый `main`, ветка на
задачу, точечные коммиты, зелёный CI.

## Рабочая папка

Работаем **только** в `/Users/srozlutskyi/Desktop/NovaPostBot-clone/NovaPostBot-hardening`.
Другие папки не трогаем.

## Ветки и PR

- В `main` — **только через Pull Request**. Защита `main` включена и **форсится
  сервером** (GitHub branch protection): прямой push запрещён **всем, включая
  владельца** (`enforce_admins`), мерж — только при **зелёном CI** (`lint-test`),
  ветка должна быть актуальна (strict). Ревью пока **не обязательно**
  (`required_approving_review_count = 0`) — поднимем до обязательного, когда в
  команде стабильно двое. Force-push и удаление `main` запрещены, история линейна.
- Ветка на задачу от свежего `main`:
  - `feat/<owner>-<short>` — новая функциональность,
  - `fix/<owner>-<short>` — багфикс,
  - `chore/<owner>-<short>` — инфраструктура/процесс.
  - `<owner>` — хэндл: **alex** или **step**. Пример: `feat/alex-ttn-create`.
- Параллельные задачи у агента — отдельный git **worktree** на задачу. У каждого
  разработчика — свой **клон** репозитория.
- В `main` мержим **squash-merge** (merge-commit и rebase отключены в репо) после
  зелёного CI; ветка после мержа **удаляется автоматически**.

## Модель работы — последовательно по фазам (актуально с 2026-06-20)

> Работаем **последовательно по фазам, не параллельно по слоям.** Причина:
> активный писатель сейчас фактически один — выигрыш параллели не реализуется, а
> последовательность даёт всегда-рабочий `main`, отсутствие дрейфа контракта и
> простое владение.

- **Один человек полностью закрывает фазу** (backend + bot/UI).
- **Второй не начинает свою фазу, пока предыдущая не замержена в `main`.**
- Следующий стартует только от свежего `main` (`git fetch` + `rebase`).
- Уже закрыты в `main`: **Phase 2 → alex**, **Phase 3 → step**,
  **Phase 4 → alex**, **Phase 5 → step**.
- Следующая по очереди сейчас: **Phase 6 → alex**.
- После неё по умолчанию: **Phase 7 → step**, если не появится отдельное
  решение перед стартом следующей фазы.
- **Пересмотр в сторону layer-split** (раздел ниже) — когда появится второй
  одновременный писатель или жёсткий дедлайн на ~2× скорость.

PR-гигиена при этом: фазу довести **несколькими мелкими PR одного владельца** (а
не одним big-bang), но второй стартует только после мержа **последнего** PR фазы.

## Зоны ответственности по слою (фолбэк — при двух параллельных писателях)

При двух одновременных писателях возвращаемся к делению **по слою кода** — так
файлы не пересекаются by design и git не может выдать конфликт:

- **alex — доменный слой:** `app/services/`, `app/db/` (модели, репозитории,
  Alembic-миграции), `app/novaposhta/`, `app/sheets/`, `app/worker.py`,
  `app/utils/`. Чистая логика без aiogram (переиспользуема для WebApp).
- **step — бот-слой:** всё под `app/bot/*` (dispatcher, middlewares, states,
  filters, permissions-wiring, handlers, keyboards, texts) + `app/main.py`.

**step не редактирует `app/services|db|...`, alex не редактирует `app/bot/*`.**
Связь между слоями — через явный контракт (сигнатуры сервисов + структуры +
исключения), который alex фиксирует первым. Поэтому фаза «закрывается», когда
оба слоя смержены и связаны (wiring). Полное распределение задач по фазам — в
[docs/ROADMAP.md](docs/ROADMAP.md). Чек-лист текущей фазы — в [PROGRESS.md](PROGRESS.md).

### Параллельная работа без конфликтов
- **Контракт-первый:** alex мержит минимальный контракт (методы/структуры/
  исключения, покрытые тестами) в `main` **раньше**; step ветвится от свежего
  `main` и пишет UI под контракт. Не `cherry-pick` (дублирует коммиты) — только
  `git fetch` + `rebase`.
- **Фундамент мержится первым** (Трек A → B; контракт → bot-layer).
- **Одна Alembic-миграция в полёте.** Только одна ветка за раз трогает модели/
  миграции; вторую ребейзят на первую перед PR — иначе две `head`-ревизии и
  ручной `alembic merge`. Модели/миграции ведёт alex.
- **`pre-commit install` обязателен у обоих** — иначе разные IDE авто-форматят
  по-разному и CI падает на `ruff format --check`. Формат — только через `ruff`,
  не через форматтер редактора. `.editorconfig`/`.gitattributes` фиксируют LF и
  базовый стиль.
- **Ребейз на свежий `main` перед PR** — `main` двигается, не держим ветку долго.

### Зоны ответственности — Фаза 1 (история)

Трек `alex` был **одной неделимой задачей** (permissions зависит от моделей) —
одна ветка и один PR.

| Разработчик | Ветка | Область кода |
|-------------|-------|--------------|
| **alex** | `feat/alex-phase1-db` | `app/db/` (модели + репозитории), Alembic-миграция, `app/sheets/` (скелет), `app/bot/permissions.py` (RBAC + dev-allowlist), bootstrap владельцев, тесты на permissions/репозитории |
| **step** | `feat/step-phase1-bot-auth` | `app/bot/` (dispatcher, middlewares, states, filters, keyboards, texts), `/start`+auth, dev god-mode (`/as`, impersonation), `app/main.py`, тесты на middleware/`/start` |

**Правила координации:**
- **Трек A (`feat/alex-phase1-db`) — фундамент, мержится первым.** Трек B
  импортирует enum ролей и модель `User`: стартует с частей без БД, БД-зависимые
  места привязывает после мержа A — `rebase` ветки `feat/step-*` на свежий `main`.
- **alex** ведёт свою задачу в одном worktree (`NovaPostBot-db`); ветку
  `feat/step-*` ведёт **step** в своём клоне.
- **Один коммиттер на ветку.** В одну ветку коммитит один человек; синхронизация
  через push/pull — без двойных коммитов в одну ветку.

## Коммиты

- **Точечно:** `git add <конкретные файлы>` — **никаких `git add .`**.
- Маленькие осмысленные коммиты, **conventional**-стиль:
  `feat: …`, `fix: …`, `chore: …`, `docs: …`, `test: …`, `refactor: …`.
- В конце сообщения — `Co-Authored-By: Claude ...` (когда коммитит ассистент).

## PROGRESS.md

После **каждого локального коммита** — запись в [`PROGRESS.md`](PROGRESS.md):
дата, ветка, хеш, что сделано, что дальше, открытые вопросы.

## Секреты

> ⚠️ **Репозиторий ПУБЛИЧНЫЙ** (free-тариф; приватность сменим на платном плане).
> Весь код виден всем — попавший в git секрет утекает мгновенно и навсегда
> (история остаётся даже после удаления файла). Дисциплина секретов — критична.

- Секреты только в `.env` (в git — `.env.example` с пустыми значениями).
- **Google service-account JSON** — в `./secrets/` (gitignored) или секрет
  хостинга, НЕ в git.
- Ключ НП каждого ФОП — Fernet, шифруется в Postgres (`sender_profiles.np_api_key`);
  `FERNET_KEY` — из env.
- Перед первым push: `git ls-files` не должен содержать `.env`, ключи, скриншоты,
  venv, дампы БД.

## Линт и тесты

```bash
pip install -r requirements.txt -r requirements-dev.txt
pre-commit install            # хуки ruff на коммит
ruff check . && ruff format --check .
pytest -q
```

**Тесты требуют Postgres** (часть тестов гоняет репозитории/миграции на живой БД,
а не на моках). Локально:

```bash
docker compose --profile dev up -d postgres   # postgres:16 на localhost:5432
cp .env.example .env                           # выставить DATABASE_URL(_DIRECT)
                                               # на localhost и FERNET_KEY
pytest -q
```

`DATABASE_URL`/`DATABASE_URL_DIRECT` для локальной БД:
`postgresql+asyncpg://novapost:novapost@localhost:5432/novapostbot`. `FERNET_KEY`
сгенерировать: `python -c "from app.utils.crypto import generate_key; print(generate_key())"`.
В CI Postgres поднимается автоматически (service-container в `ci.yml`).

CI (GitHub Actions, `.github/workflows/ci.yml`, job `lint-test`) гоняет layer-check,
`ruff` + `ruff format --check`, `compileall` и `pytest` (с Postgres-сервисом) и является
гейтом для merge в `main`.

## Среды и процесс

Три среды с **раздельными** ресурсами — чтобы «проверить на боте» никогда не
значило «на боевом»:

| Среда | Бот-токен | БД | Когда |
|---|---|---|---|
| **local** | **отдельный** тест-бот (@BotFather) | локальный Postgres (`docker-compose.override.yml`) | запуск на машине разработчика |
| **staging** *(этап B — ещё не активна)* | отдельный staging-бот | Neon-ветка | always-on на том же VPS, авто-деплой из PR (см. ниже) |
| **production** | боевой `@novopokrovka_np_bot` | Neon (managed) | только merge в `main` → авто-деплой |

`ENVIRONMENT` (`local`/`staging`/`production`) в `.env` виден в логе старта и по
`/version` — чтобы случайно не спутать тест с продом. На поведение кода не влияет.

**Поток обкатки (последовательная модель):**
```text
git checkout feat/x → docker compose up   # локальный тест-бот, свой токен, локальный PG
   → руками проверяешь в тест-боте → OK?
   → PR → CI (lint-test, гейт) + авто-ревью CodeRabbit → merge в main → авто-деплой в прод
   → проблема в проде? → workflow «Rollback prod» на прошлый :sha-/:vX.Y.Z
```

> **Локально — только тест-бот.** Два процесса на одном токене дают `409 Conflict`
> у Telegram и задевают реальных клиентов. Боевой токен живёт лишь в `.env` на VPS.

**Зачем PR, если ревьюим не глубоко.** PR — это **ворота**, а не только чтение кода
человеком: CI гоняется на *результате слияния* (защищает всегда-зелёный `main`,
который авто-деплоится в прод), плюс self-review своего diff, авто-ревью бота и
точка отката. Прямой push в `main` убрал бы этот буфер перед продом.

**Авто-ревью PR.** Ревьюер — **CodeRabbit** (GitHub App, free для публичных репо):
на каждый PR даёт summary + построчные комментарии. Конфиг — `.coderabbit.yaml`
(ревью на русском, Alembic-версии не ревьюим построчно). **Сигнальный**, не
блокирует merge (гейт — `lint-test`). Активация разово: coderabbit.ai → логин
через GitHub → установить App на репозиторий (секретов/workflow не требует). Плюс
ручной `/code-review` и `/security-review` из Claude Code по требованию.

**Обновление без остановки клиентов.**
- Бот на **long polling**: при деплое контейнер перезапускается ~2–5 с, Telegram
  копит апдейты у себя (~24 ч) и отдаёт после старта — сообщения не теряются.
  Спец-тюнинг zero-downtime не нужен. Blue-green/canary неприменимы (два поллера
  на одном токене → `409`) — только через переход на webhook.
- **Миграции — backward-compatible (expand/contract):** (1) *expand* — миграция
  только добавляет (nullable-колонка / новая таблица), старый код продолжает
  работать; (2) деплой нового кода; (3) *contract* — удаление старого отдельным
  поздним релизом. Так схема совместима и со старым, и с новым кодом в момент
  переключения, и **откат образа не разбивается о новую схему**. Автоматический
  `alembic downgrade` в откате не делаем — только осознанно вручную.

**Откат прода.** Actions → «Rollback prod» (`workflow_dispatch`) → указать тег
образа (`sha-<short>` из `ci.yml` или `vX.Y.Z` из `release.yml`). Workflow по SSH
подменяет `APP_IMAGE` на VPS и перекатывает (`pull && up -d`). Откат меняет **код,
не схему БД** — см. правило expand/contract выше.

**Этап B (позже, когда появятся клиенты и нужен always-on тест-стенд):** тот же
тест-бот переносится на VPS вторым стеком (`~/NovaPostBot-staging`, проект
`-p novapostbot-staging`, свой redis, `ENVIRONMENT=staging`, Neon-ветка) с
авто-деплоем из PR. План заранее совместим — переделывать не нужно.

## CI/CD и деплой

**Непрерывный деплой.** После зелёного `lint-test` **push в `main`** запускает job
`deploy` (`ci.yml`): собирает образ, пушит в **GHCR** (`:latest` + `:sha-<short>`) и
деплоит по SSH на VPS (`docker compose pull && up -d --no-build`). Ручной
`up -d --build` на сервере больше не нужен.

**Версия сборки.** CI прокидывает `--build-arg GIT_SHA=<sha>` → `APP_VERSION` в образе.
Видна в логе старта (`bot.start version=…` / `worker.start`) и по команде `/version`
(dev). Так всегда понятно, что именно крутится в проде.

**Образ в compose.** `docker-compose.yml` параметризован: локально `APP_IMAGE` не задан →
`build` собирает `novapostbot:local`; на VPS в `.env` — `APP_IMAGE=ghcr.io/<owner>/novapostbot:latest`.

**Релизы/вехи.** Тег `vX.Y.Z` (`git tag vX.Y.Z && git push origin vX.Y.Z`) → `release.yml`
создаёт GitHub Release (авто-заметки из PR) + образ с тегом версии. Continuous-деплой
идёт по `main`; теги — для отслеживания версий и отката. Журнал — `CHANGELOG.md`.

### Активация деплоя (разово, нужны права)

1. **Secrets репозитория** (Settings → Secrets and variables → Actions):
   `SSH_HOST`, `SSH_USER`, `SSH_PRIVATE_KEY` (deploy-ключ Hetzner), опц. `DEPLOY_PATH`
   (путь к репо на VPS, дефолт `~/NovaPostBot`). Пока их нет — шаг деплоя мягко
   скипается, образ всё равно пушится в GHCR.
2. **На VPS:** репозиторий с `.env` (+ `APP_IMAGE=ghcr.io/<owner>/novapostbot:latest`) и
   `./secrets/`. GHCR-пакет держим **приватным** — образ тянет только сервер. Разово,
   под тем же пользователем, что `SSH_USER` (креды лягут в его `~/.docker/config.json`):
   ```bash
   # PAT (classic) со scope read:packages — github.com/settings/tokens
   echo '<PAT>' | docker login ghcr.io -u <owner> --password-stdin
   ```
   После этого `docker compose pull` в деплое авторизуется автоматически.
3. **CODEOWNERS** — логины уже проставлены (`@yennned`, `@Stepandj`).
4. **Обязательное ревью** (когда команда стабильно вдвоём): включить
   `required_approving_review_count = 1` в branch protection `main`:
   ```bash
   gh api -X PATCH repos/<owner>/NovaPostBot/branches/main/protection/required_pull_request_reviews \
     -F required_approving_review_count=1
   ```

## Документация

Дизайн и флоу — в [`docs/`](docs/) (оглавление — [`README.md`](README.md)).
Поэтапный план — [`docs/ROADMAP.md`](docs/ROADMAP.md). Контекст для ассистентов —
[`CLAUDE.md`](CLAUDE.md). Любая правка логики синхронизируется с docs.
