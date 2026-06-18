# Бриф Степану — Фаза 2 (bot-layer поверх контракта Alex)

Документ для шаринга. Контракт стабилен и живёт в `app/services/*` (слой Alex) —
ты пишешь **только `app/bot/*`** и вызываешь эти функции. Внизу есть блок
**«В память IDE»** — скопируй его в память своей IDE.

## 0. Как забрать контракт (git-флоу)

1. Дождись, пока контракт-PR (`feat/alex-clients`) **смержен в `main`**.
2. `git fetch origin` → `git rebase origin/main` (НЕ клонируй заново, НЕ
   `cherry-pick` — он дублирует коммиты и даёт конфликты на мерже).
3. Ветка: `feat/step-phase2-clients` от свежего `main`.
4. Если очень нужно начать ДО мержа контракта — ветвись от
   `origin/feat/alex-clients` и пере-стекнись (`git rebase origin/main`) после мержа.

## 1. Контракт (стабильные сигнатуры)

`app/services/clients.py` — все функции принимают `session: AsyncSession` и
именованный `actor: User`; возвращают frozen-датаклассы; кидают подтипы
`ClientServiceError`. Транзакцию (`commit`) делает твой middleware — сервис только
`flush`-ит.

```python
list_clients(session, *, actor, status=None, query=None, limit=20, offset=0) -> ClientPage
get_client_card(session, *, actor, client_id) -> ClientCard
approve_client(session, *, actor, client_id) -> ClientCard          # pending → active
block_client(session, *, actor, client_id, reason=None) -> ClientCard
unblock_client(session, *, actor, client_id) -> ClientCard          # blocked → active
archive_client(session, *, actor, client_id) -> ClientCard
restore_client(session, *, actor, client_id) -> ClientCard          # archived → active
update_client_profile(session, *, actor, client_id, full_name=None, phone=None) -> ClientCard
```

Структуры (`app/services/clients.py`):
- `ClientListItem(id, telegram_id, full_name, phone, status, created_at)`
- `ClientPage(items: list[ClientListItem], total, status_counts: dict[UserStatus,int], limit, offset)`
- `ClientCard(id, telegram_id, full_name, phone, role, status, created_at, sender_profiles_count, default_sender_name)`

Исключения (`app/services/exceptions.py`) — лови и рендери uk-текст:
- `ClientNotFound` — нет такого клиента;
- `PermissionDenied` — нет прав (иерархия `can_manage` или отозван per-flag);
- `TransitionForbidden(from_status, to_status)` — недопустимый переход;
- `AlreadyInStatus(status)` — уже в этом статусе (подтип `TransitionForbidden`).

Уведомления (`app/services/notifications.py`):
```python
class Notifier(Protocol):
    async def send_message(self, telegram_id: int, text: str) -> None: ...

notify_new_client_registered(session, notifier, *, client) -> None   # владельцам + дежурным
notify_client_approved(notifier, *, client) -> None                  # клиенту
```
Тексты пушей (uk, HTML-разметка `<b>`) уже внутри модуля — тебе их формировать
не нужно.

## 2. Твои задачи Фазы 2 (только `app/bot/*`)
- `handlers/clients_manage.py` — список со статус-вкладками (`status_counts`),
  поиск/пагинация (`limit/offset`, `total`), карточка клиента, кнопки
  Підтвердити / Блок / Розблок / Архів / Відновити / Редагувати.
- `keyboards/manager.py` (+ `owner.py`) — клавиатуры списка/карточки/подтверждений.
- `texts/` — uk-строки экранов и ошибок (маппинг из исключений).
- `states.py` — `ClientManageState` (поиск, подтверждение блок/архив, правка ПІБ).

## 3. Wiring (как связать с контрактом)
- В хендлер middleware уже инъектит `db_session` и `effective_context`. Зови:
  ```python
  ctx = data["effective_context"]
  card = await clients.approve_client(session, actor=ctx.actor_user, client_id=cid)
  ```
  **`actor` = `ctx.actor_user`** (реальная личность) — сервис сам разрулит
  dev-allowlist/иерархию/флаги. Оборачивай вызовы в try/except на
  `ClientServiceError` → uk-сообщение.
- **`BotNotifier`** (реализация `Notifier`) — в `app/bot/` поверх aiogram `Bot`:
  ```python
  class BotNotifier:
      def __init__(self, bot: Bot) -> None: self.bot = bot
      async def send_message(self, telegram_id: int, text: str) -> None:
          try:
              await self.bot.send_message(telegram_id, text, parse_mode="HTML")
          except TelegramAPIError:
              ...  # лог + проглотить: сбой одного получателя не валит флоу
  ```
- **Триггеры пушей:**
  - в start-флоу, когда `StartResult.created is True` →
    `await notifications.notify_new_client_registered(session, bot_notifier, client=result.user)`;
  - после `approve_client` →
    `await notifications.notify_client_approved(bot_notifier, client=...)` (telegram_id есть в `ClientCard`).

## 4. Правила параллельной работы (см. CONTRIBUTING)
- **Не редактируй `app/services/*` и `app/db/*`** — это слой Alex. Нужна правка
  контракта → согласуй, Alex меняет у себя и мержит.
- Один коммиттер на ветку; `pre-commit install` обязателен; формат — только
  `ruff format` (не форматтер IDE); ребейз на свежий `main` перед PR.
- Миграции не трогаешь (их ведёт Alex) — одна миграция в полёте.

---

## В память IDE (скопировать)
```
Проект NovaPostBot. Я (step) пишу ТОЛЬКО app/bot/* (хендлеры, клавиатуры, тексты,
FSM). Доменную логику не дублирую — зову app/services/* (слой alex):
- clients: approve/block/unblock/archive/restore/update_client_profile/list_clients/
  get_client_card(session, *, actor=ctx.actor_user, ...). Ловлю ClientServiceError
  (ClientNotFound/PermissionDenied/TransitionForbidden/AlreadyInStatus) → uk-текст.
- notifications: notify_new_client_registered / notify_client_approved + Notifier
  (реализую BotNotifier поверх aiogram Bot, parse_mode=HTML, ошибки глотаю).
Транзакцию делает middleware (commit/rollback) — сервисы только flush.
Не редактирую app/services|db, не трогаю миграции. pre-commit + ruff format
обязательны. Контракт беру из main (fetch+rebase), не cherry-pick.
```
