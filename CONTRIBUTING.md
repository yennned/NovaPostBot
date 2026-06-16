# CONTRIBUTING — правила разработки NovaPostBot

Для нас двоих (я + Степан). Цель — чистый процесс: защищённый `main`, ветка на
задачу, точечные коммиты, зелёный CI.

## Рабочая папка

Работаем **только** в `/Users/yenin/Desktop/NovaPostBot`. Другие папки не трогаем.

## Ветки и PR

- В `main` — **только через Pull Request** (защищённый main, обязательный ревью +
  зелёный CI). Прямой push в `main` запрещён.
- Ветка на задачу от свежего `main`:
  - `feat/<owner>-<short>` — новая функциональность,
  - `fix/<owner>-<short>` — багфикс,
  - `chore/<owner>-<short>` — инфраструктура/процесс.
  - `<owner>` — хэндл: **alex** или **step**. Пример: `feat/alex-ttn-create`.
- Параллельные задачи у агента — отдельный git **worktree** на задачу. У каждого
  разработчика — свой **клон** репозитория.
- В `main` мержим **squash-merge** после ревью и зелёного CI.

## Коммиты

- **Точечно:** `git add <конкретные файлы>` — **никаких `git add .`**.
- Маленькие осмысленные коммиты, **conventional**-стиль:
  `feat: …`, `fix: …`, `chore: …`, `docs: …`, `test: …`, `refactor: …`.
- В конце сообщения — `Co-Authored-By: Claude ...` (когда коммитит ассистент).

## PROGRESS.md

После **каждого локального коммита** — запись в [`PROGRESS.md`](PROGRESS.md):
дата, ветка, хеш, что сделано, что дальше, открытые вопросы.

## Секреты

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

CI (GitHub Actions, `.github/workflows/ci.yml`) гоняет `ruff` + `pytest` и
является гейтом для merge в `main`.

## Документация

Дизайн и флоу — в [`docs/`](docs/) (оглавление — [`README.md`](README.md)).
Поэтапный план — [`docs/ROADMAP.md`](docs/ROADMAP.md). Контекст для ассистентов —
[`CLAUDE.md`](CLAUDE.md). Любая правка логики синхронизируется с docs.
