"""Роутеры bot-layer."""

from app.bot.handlers.clients_manage import router as clients_router
from app.bot.handlers.dev import router as dev_router
from app.bot.handlers.start import router as start_router

__all__ = ["clients_router", "dev_router", "start_router"]
