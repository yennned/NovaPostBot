"""repair audit_logs.account_id: actor's account -> subject's account.

`AuditRepository.log` заполнял `account_id` из членства **актора**, а не субъекта
действия. Правило было инвертировано: staff-действия о клиенте
(`client_approved` и т.п.) получали NULL, потому что у менеджера-актора членства
нет, а дежурство менеджера, наоборот, могло получить клиентский аккаунт.

Эта миграция чинит уже накопленные строки. Код правится в том же PR: неявный
догруз убран, `account_id` проставляют вызывающие явно.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Снять аккаунт актора там, где аккаунта-субъекта нет вовсе: дежурство,
    #    персонал платформы, bootstrap, dev-god-mode. Аккаунт в этих строках мог
    #    появиться только из членства актора.
    op.execute(
        r"""
        update audit_logs
        set account_id = null
        where account_id is not null
          and (
            action in ('duty_started', 'duty_ended', 'owner_bootstrapped', 'permission_changed')
            or action like 'dev\_%'
            or action like 'manager\_%'
          )
        """
    )

    # 2. Восстановить субъект там, где `affected_entity` его называет. Регексп —
    #    защита от неожиданного формата: приведение мусора к uuid уронило бы
    #    миграцию.
    #
    # 2a. `user:<uuid>` — членство даёт аккаунт этого пользователя. Список действий
    #     обязателен: `manager_promoted` и т.п. тоже указывают на `user:<uuid>`, но
    #     аккаунта-субъекта у них нет (см. шаг 1). Членство уникально по `user_id`,
    #     поэтому join детерминирован. Субъект без членства (напр. менеджер,
    #     когда-то подтверждённый как клиент) остаётся NULL — аккаунт не выдумываем.
    op.execute(
        r"""
        update audit_logs a
        set account_id = m.account_id
        from client_account_memberships m
        where a.account_id is null
          and a.action in (
            'client_approved', 'client_blocked', 'client_archived',
            'client_restored', 'client_profile_updated'
          )
          and a.affected_entity ~ '^user:[0-9a-fA-F-]{36}$'
          and m.user_id = substring(a.affected_entity from 6)::uuid
        """
    )

    # 2b. `shipment:<uuid>` — у отправления `account_id` NOT NULL, то есть субъект
    #     восстанавливается точно. Список действий не нужен: отправление по своей
    #     природе принадлежит аккаунту.
    op.execute(
        r"""
        update audit_logs a
        set account_id = s.account_id
        from shipments s
        where a.account_id is null
          and a.affected_entity ~ '^shipment:[0-9a-fA-F-]{36}$'
          and s.id = substring(a.affected_entity from 10)::uuid
        """
    )

    # 2c. `sender_profile:<uuid>` — то же самое: `sender_profiles.account_id` NOT NULL.
    op.execute(
        r"""
        update audit_logs a
        set account_id = p.account_id
        from sender_profiles p
        where a.account_id is null
          and a.affected_entity ~ '^sender_profile:[0-9a-fA-F-]{36}$'
          and p.id = substring(a.affected_entity from 16)::uuid
        """
    )


def downgrade() -> None:
    # Ремонт данных не откатывается: прежние значения были заведомо неверными
    # (аккаунт актора вместо аккаунта субъекта), и восстанавливать их не из чего —
    # старое правило не было функцией от сохранённых полей.
    pass
