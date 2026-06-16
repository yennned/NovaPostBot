"""Bootstrap владельцев из `OWNER_TELEGRAM_IDS`.

Вызывается на старте бота: гарантирует, что каждый объявленный в конфиге владелец
существует в БД с ролью `owner` и статусом `active`. Уже существующего
пользователя — повышает/активирует. Каждое изменение пишется в `audit_logs`.
Идемпотентно: повторный запуск без изменений ничего не делает.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User
from app.db.repositories import AuditRepository, UserRepository


async def ensure_owners(session: AsyncSession, settings: Settings | None = None) -> list[User]:
    """Создать/повысить владельцев из конфига. Возвращает их актуальные записи."""
    settings = settings or get_settings()
    users = UserRepository(session)
    audit = AuditRepository(session)

    result: list[User] = []
    for telegram_id in settings.owner_telegram_ids:
        user = await users.get_by_telegram_id(telegram_id)

        if user is None:
            user = await users.create(
                telegram_id=telegram_id,
                role=UserRole.owner,
                status=UserStatus.active,
            )
            await audit.log(
                "owner_bootstrapped",
                user_id=user.id,
                affected_entity=f"user:{user.id}",
                after={"role": UserRole.owner, "status": UserStatus.active},
                notes="создан владелец из OWNER_TELEGRAM_IDS",
            )
            result.append(user)
            continue

        before = {"role": user.role, "status": user.status}
        changed = False
        if user.role is not UserRole.owner:
            await users.update_role(user, UserRole.owner)
            changed = True
        if user.status is not UserStatus.active:
            await users.update_status(user, UserStatus.active)
            changed = True
        if changed:
            await audit.log(
                "owner_bootstrapped",
                user_id=user.id,
                affected_entity=f"user:{user.id}",
                before=before,
                after={"role": user.role, "status": user.status},
                notes="повышение до владельца из OWNER_TELEGRAM_IDS",
            )
        result.append(user)

    return result
