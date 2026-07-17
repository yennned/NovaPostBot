"""Репозиторий аудита (`audit_logs`) — запись (append-only) + чистка ПИИ при удалении."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from app.db.models.audit import AuditLog
from app.db.repositories.base import BaseRepository

# Ключи payload'а, в которых оседают персональные данные человека.
# `client_settings`/`clients` пишут сюда `full_name`/`phone` при правке профиля,
# `account_team` — `phone` при приглашении.
_PII_KEYS = ("full_name", "phone", "telegram_id")


class AuditRepository(BaseRepository):
    async def log(
        self,
        action: str,
        *,
        user_id: uuid.UUID | None = None,
        account_id: uuid.UUID | None = None,
        affected_entity: str | None = None,
        before: dict | None = None,
        after: dict | None = None,
        notes: str | None = None,
    ) -> AuditLog:
        """Записать действие в аудит.

        `user_id` — **актор** (кто сделал). `account_id` — **субъект**: чей
        клиентский аккаунт затронут действием. Это разные вещи, и выводить одно
        из другого нельзя: менеджер подтверждает отправление клиента (актор —
        менеджер, субъект — аккаунт клиента), а дежурство менеджера не касается
        клиентских аккаунтов вовсе.

        Поэтому `account_id` проставляет вызывающий и только явно. У действия нет
        аккаунта-субъекта (дежурство, персонал, bootstrap, dev) — остаётся `None`.
        Раньше метод догружал членство актора и писал его аккаунт: staff-действия
        о клиенте получали `NULL`, а дежурство — чужой аккаунт.
        """
        entry = AuditLog(
            action=action,
            user_id=user_id,
            account_id=account_id,
            affected_entity=affected_entity,
            before=before,
            after=after,
            notes=notes,
        )
        await self._add(entry)
        return entry

    async def scrub_user_pii(self, user_id: uuid.UUID) -> int:
        """Вычистить ПИИ человека из payload'ов аудита перед его физическим удалением.

        **Единственное исключение из append-only** (см. докстринг модели), и оно
        осознанное: удаление обязано убрать ПИБ/телефон/Telegram ID отовсюду, иначе
        они переживут человека в `before`/`after`. Сами строки остаются — тип
        действия, время и `account_id` продолжают отвечать на вопрос «что было»,
        просто уже без персональных данных.

        FK `user_id` обнулит сам `ondelete=SET NULL`, но до payload'а он не
        добирается — JSONB для БД непрозрачен. Отсюда явный UPDATE.

        Берём и строки, где человек — актор (`user_id`), и где он субъект
        (`affected_entity = "user:<id>"`): ПИИ пишут обе стороны.
        """
        removals = "".join(f" - '{key}'" for key in _PII_KEYS)
        # `jsonb_typeof(...) = 'object'` — не перестраховка: SQLAlchemy пишет
        # `before=None` в JSONB как JSON `null`, а не как SQL NULL, и оператор `-`
        # на таком скаляре падает `cannot delete from scalar`, обрушивая удаление.
        result = await self.session.execute(
            text(
                f"""
                UPDATE audit_logs
                   SET before = CASE WHEN jsonb_typeof(before) = 'object'
                                     THEN before{removals} ELSE before END,
                       after  = CASE WHEN jsonb_typeof(after) = 'object'
                                     THEN after{removals} ELSE after END
                 WHERE user_id = :user_id OR affected_entity = :entity
                """  # noqa: S608 — `_PII_KEYS` константа модуля, не ввод
            ),
            {"user_id": user_id, "entity": f"user:{user_id}"},
        )
        return result.rowcount
