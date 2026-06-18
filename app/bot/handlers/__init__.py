"""Роутеры bot-layer."""

from app.bot.handlers.dev import router as dev_router
from app.bot.handlers.start import router as start_router

__all__ = ["dev_router", "start_router"]
