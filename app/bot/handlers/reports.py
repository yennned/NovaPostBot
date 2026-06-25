"""Хендлер «📊 Звіти» (менеджер с правом `can_view_reports`, владелец; Фаза 6).

Сводка по периоду + разбивка по клиентам. Период переключается inline-кнопками.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.reports import build_period_kb
from app.bot.texts import reports as texts
from app.bot.types import EffectiveContext
from app.db.models.enums import UserRole
from app.services import reports
from app.services.exceptions import ClientServiceError
from app.services.reports import PERIODS

router = Router(name="reports")

REPORTS_BUTTON = "📊 Звіти"
_STALE = "Кнопка застаріла, відкрийте розділ заново."


def _can_reports(ctx: EffectiveContext) -> bool:
    return ctx.effective_role is UserRole.manager or ctx.is_dev


async def _show(
    target: Message, session: AsyncSession, ctx: EffectiveContext, *, period: str, edit: bool
) -> None:
    report = await reports.period_report(session, actor=ctx.effective_user, period=period)
    text, markup = texts.period_report_text(report), build_period_kb("rep", period)
    if edit:
        await target.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=markup, parse_mode="HTML")


@router.message(F.text == REPORTS_BUTTON)
async def open_reports(
    message: Message, effective_context: EffectiveContext, db_session: AsyncSession
) -> None:
    if not _can_reports(effective_context) or effective_context.effective_user is None:
        raise SkipHandler()
    try:
        await _show(message, db_session, effective_context, period="today", edit=False)
    except ClientServiceError as exc:
        await message.answer(str(exc))


@router.callback_query(F.data == "home:reports")
async def open_reports_home(
    callback: CallbackQuery, effective_context: EffectiveContext, db_session: AsyncSession
) -> None:
    if callback.message is None or not _can_reports(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        await _show(callback.message, db_session, effective_context, period="today", edit=True)
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("rep:p:"))
async def cb_period(
    callback: CallbackQuery, effective_context: EffectiveContext, db_session: AsyncSession
) -> None:
    if callback.message is None or not _can_reports(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    period = callback.data.split(":")[2]
    if period not in PERIODS or effective_context.effective_user is None:
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        await _show(callback.message, db_session, effective_context, period=period, edit=True)
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()
