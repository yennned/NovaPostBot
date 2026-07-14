"""Bootstrap владельцев из `OWNER_TELEGRAM_IDS`.

Вызывается на старте бота: гарантирует, что каждый объявленный в конфиге владелец
существует в БД с ролью `owner` и статусом `active`. Уже существующего
пользователя — повышает/активирует. Каждое изменение пишется в `audit_logs`.
Идемпотентно: повторный запуск без изменений ничего не делает.

Допущение (решение владельца): клиент/его работники и менеджер платформы —
непересекающиеся множества, менеджер всегда отдельное лицо со стороны НП. Поэтому
повышение здесь ничего не делает с клиентскими данными: сценария «бывший клиент с
легаси-данными» в проде нет, а попадание клиента в `OWNER_TELEGRAM_IDS` — ошибка
конфига. Молча отказать от повышения нельзя (владелец остался бы клиентом и не
понял почему), но и гасить его акаунт нельзя: разморозка идёт через
`clients._transition` → `_get_client`, который бросает `ClientNotFound` на
`role is not client`, то есть заморозка была бы НЕОБРАТИМОЙ. Вместо этого —
громкий лог + пометка в аудите, разбирается человек.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models.enums import MembershipRole, UserRole, UserStatus
from app.db.models.user import User
from app.db.repositories import AuditRepository, ClientAccountRepository, UserRepository

logger = structlog.get_logger(__name__)


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
                user_id=None,  # системное действие на старте — актора нет
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
            # Датчик, а не решение: акаунт не трогаем (см. докстринг модуля). Внутри
            # `changed`, чтобы не спамить на каждом рестарте — состояние стабильно.
            notes = "повышение до владельца из OWNER_TELEGRAM_IDS"
            membership = await ClientAccountRepository(session).get_membership(user_id=user.id)
            if membership is not None and membership.role is MembershipRole.account_owner:
                logger.warning(
                    "owner_bootstrap_promoted_account_owner",
                    account_id=str(membership.account_id),
                    before_role=before["role"],
                )
                notes += (
                    f"; у него остался клиентский акаунт {membership.account_id} —"
                    " вероятно, ошибка конфига OWNER_TELEGRAM_IDS"
                )
            await audit.log(
                "owner_bootstrapped",
                user_id=None,  # системное действие на старте — актора нет
                affected_entity=f"user:{user.id}",
                before=before,
                after={"role": user.role, "status": user.status},
                notes=notes,
            )
        result.append(user)

    return result
