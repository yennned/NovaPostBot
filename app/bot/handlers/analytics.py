"""Хендлер «📈 Аналітика» (только владелец/dev; Фаза 6).

Сводка по периоду + финотчёт (fee, опоздавшие ТТН) + поддержка по менеджерах.
Период переключается inline-кнопками; можно выбрать конкретный день (быстрые
кнопки последних дней или ручной ввод).
"""

from __future__ import annotations

from datetime import date

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.reports import build_period_kb
from app.bot.states import AnalyticsState
from app.bot.texts import reports as texts
from app.bot.types import EffectiveContext
from app.db.models.enums import UserRole
from app.services import reports
from app.services.exceptions import ClientServiceError
from app.services.reports import PERIODS
from app.utils.dates import USER_DATE_HINT, parse_user_date

router = Router(name="analytics")

ANALYTICS_BUTTON = "📈 Аналітика"
_STALE = "Кнопка застаріла, відкрийте розділ заново."


def _is_owner(ctx: EffectiveContext) -> bool:
    return ctx.effective_role is UserRole.owner or ctx.is_dev


async def _show(
    target: Message,
    session: AsyncSession,
    ctx: EffectiveContext,
    *,
    period: str,
    day: date | None = None,
    edit: bool,
) -> None:
    actor = ctx.effective_user
    report = await reports.period_report(session, actor=actor, period=period, day=day)
    fin = await reports.financial_report(session, actor=actor, period=period, day=day)
    stats = await reports.manager_support_stats(session, actor=actor, period=period, day=day)
    text = texts.analytics_text(report, fin, stats)
    markup = build_period_kb("an", period, selected_day=day)
    if edit:
        await target.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=markup, parse_mode="HTML")


@router.message(F.text == ANALYTICS_BUTTON)
async def open_analytics(
    message: Message, effective_context: EffectiveContext, db_session: AsyncSession
) -> None:
    if not _is_owner(effective_context) or effective_context.effective_user is None:
        raise SkipHandler()
    try:
        await _show(message, db_session, effective_context, period="today", edit=False)
    except ClientServiceError as exc:
        await message.answer(str(exc))


@router.callback_query(F.data == "home:analytics")
async def open_analytics_home(
    callback: CallbackQuery, effective_context: EffectiveContext, db_session: AsyncSession
) -> None:
    if (
        callback.message is None
        or not _is_owner(effective_context)
        or effective_context.effective_user is None
    ):
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        await _show(callback.message, db_session, effective_context, period="today", edit=True)
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("an:p:"))
async def cb_period(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None or not _is_owner(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    period = callback.data.split(":")[2]
    if period not in PERIODS or effective_context.effective_user is None:
        await callback.answer(_STALE, show_alert=True)
        return
    await state.clear()
    try:
        await _show(callback.message, db_session, effective_context, period=period, edit=True)
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("an:day:"))
async def cb_day(
    callback: CallbackQuery,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if callback.message is None or not _is_owner(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    try:
        day = date.fromisoformat(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer(_STALE, show_alert=True)
        return
    if effective_context.effective_user is None:
        await callback.answer(_STALE, show_alert=True)
        return
    await state.clear()
    try:
        await _show(
            callback.message, db_session, effective_context, period="day", day=day, edit=True
        )
    except ClientServiceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data == "an:pick")
async def cb_pick(
    callback: CallbackQuery, effective_context: EffectiveContext, state: FSMContext
) -> None:
    if callback.message is None or not _is_owner(effective_context):
        await callback.answer(_STALE, show_alert=True)
        return
    await state.set_state(AnalyticsState.waiting_for_date)
    await callback.message.answer(f"Введіть дату у форматі {USER_DATE_HINT}.")
    await callback.answer()


@router.message(AnalyticsState.waiting_for_date, F.text, ~F.text.startswith("/"))
async def receive_date(
    message: Message,
    effective_context: EffectiveContext,
    db_session: AsyncSession,
    state: FSMContext,
) -> None:
    if not _is_owner(effective_context) or effective_context.effective_user is None:
        await state.clear()
        raise SkipHandler()
    day = parse_user_date(message.text)
    if day is None:
        await message.answer(f"❌ Невірна дата. Використайте {USER_DATE_HINT}.")
        return
    await state.clear()
    try:
        await _show(message, db_session, effective_context, period="day", day=day, edit=False)
    except ClientServiceError as exc:
        await message.answer(str(exc))
