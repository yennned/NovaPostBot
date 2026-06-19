"""Middleware для сервисов и effective context."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.services import (
    BotServices,
    DevService,
    InMemoryDevState,
    RepositoryAuditLog,
    RepositoryUserStore,
    StartService,
    build_effective_context,
)
from app.db.repositories import AuditRepository, UserRepository

if TYPE_CHECKING:
    from app.novaposhta.cache import NPReferenceCache
    from app.novaposhta.client import NovaPoshtaClient


class ServicesMiddleware(BaseMiddleware):
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        *,
        dev_ids: frozenset[int],
        dev_state: InMemoryDevState,
        np_client: NovaPoshtaClient | None = None,
        np_cache: NPReferenceCache | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.dev_ids = dev_ids
        self.dev_state = dev_state
        self.np_client = np_client
        self.np_cache = np_cache

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.sessionmaker() as session:
            services = BotServices(
                user_store=RepositoryUserStore(UserRepository(session)),
                audit_log=RepositoryAuditLog(AuditRepository(session)),
                dev_ids=self.dev_ids,
                dev_state=self.dev_state,
            )
            data["db_session"] = session
            data["services"] = services
            data["np_client"] = self.np_client
            data["np_cache"] = self.np_cache
            data["start_service"] = StartService(services.user_store)
            data["dev_service"] = DevService(services)
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result


class EffectiveContextMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        services: BotServices = data["services"]
        dev_service: DevService = data["dev_service"]

        actor_user = None
        impersonated_user = None
        dev_session = None
        is_dev = False

        if user is not None:
            actor_user = await services.user_store.get_by_telegram_id(user.id)
            is_dev = dev_service.is_dev(user.id)
            if is_dev:
                dev_session = dev_service.get_session(user.id)
                if dev_session.impersonated_user_id is not None:
                    impersonated_user = await services.user_store.get_by_telegram_id(
                        dev_session.impersonated_user_id
                    )

        data["effective_context"] = build_effective_context(
            actor_user=actor_user,
            impersonated_user=impersonated_user,
            is_dev=is_dev,
            dev_session=dev_session,
        )
        return await handler(event, data)
