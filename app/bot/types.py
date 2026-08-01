"""Типы bot-layer, которые не живут в data-layer."""

from __future__ import annotations

from dataclasses import dataclass

from app.db.models.client_account import ClientAccount, ClientAccountMembership
from app.db.models.enums import UserRole
from app.db.models.user import User


@dataclass(slots=True)
class DevSession:
    role_override: UserRole | None = None
    impersonated_user_id: int | None = None


@dataclass(slots=True)
class EffectiveContext:
    actor_user: User | None
    effective_user: User | None
    effective_role: UserRole | None
    is_dev: bool
    dev_session: DevSession | None = None
    account_context: ClientAccountContext | None = None

    # `account`/`membership` ВЫВОДЯТСЯ, а не хранятся: три поля об одном факте
    # расходились молча (мидлварь писала все три подряд), а рассинхрон пары
    # `(account_id, account)` уже стоил бага — работник видел свой склад вместо
    # складского. Один источник правды — `account_context`.
    @property
    def account(self) -> ClientAccount | None:
        return self.account_context.account if self.account_context else None

    @property
    def membership(self) -> ClientAccountMembership | None:
        return self.account_context.membership if self.account_context else None


@dataclass(frozen=True, slots=True)
class ClientAccountContext:
    """Поточний бізнес-контекст: користувач, акаунт і членство."""

    user: User
    account: ClientAccount
    membership: ClientAccountMembership
    actor_user: User | None = None
