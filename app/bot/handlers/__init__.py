"""Роутеры bot-layer."""

from app.bot.handlers.client_cabinet import router as client_cabinet_router
from app.bot.handlers.clients_manage import router as clients_router
from app.bot.handlers.dev import router as dev_router
from app.bot.handlers.start import router as start_router
from app.bot.handlers.ttn import router as ttn_router

__all__ = [
    "client_cabinet_router",
    "clients_router",
    "dev_router",
    "start_router",
    "ttn_router",
]
