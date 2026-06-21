"""Роутеры bot-layer."""

from app.bot.handlers.client_cabinet import router as client_cabinet_router
from app.bot.handlers.clients_manage import router as clients_router
from app.bot.handlers.dev import router as dev_router
from app.bot.handlers.duty import router as duty_router
from app.bot.handlers.errors import router as errors_router
from app.bot.handlers.manager_shipments import router as manager_shipments_router
from app.bot.handlers.staff import router as staff_router
from app.bot.handlers.start import router as start_router
from app.bot.handlers.support import router as support_router
from app.bot.handlers.ttn import router as ttn_router

__all__ = [
    "client_cabinet_router",
    "clients_router",
    "dev_router",
    "duty_router",
    "errors_router",
    "manager_shipments_router",
    "staff_router",
    "start_router",
    "support_router",
    "ttn_router",
]
