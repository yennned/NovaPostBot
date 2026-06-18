"""Dev-only команды: /as, /as_user, kill-switch."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.filters import IsDevFilter
from app.bot.services import BotServices, DevService
from app.bot.texts import dev_help_text
from app.db.models.enums import UserRole

router = Router(name="dev")
router.message.filter(IsDevFilter())


def _parse_role(value: str) -> UserRole | None:
    try:
        return UserRole(value)
    except ValueError:
        return None


@router.message(Command("as"))
async def as_role(message: Message, dev_service: DevService) -> None:
    if message.from_user is None:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 1:
        await message.answer(dev_help_text())
        return

    argument = parts[1].strip().lower()
    if argument == "off":
        await dev_service.clear_context(message.from_user.id)
        await message.answer("Dev-контекст очищено.")
        return

    role = _parse_role(argument)
    if role is None:
        await message.answer("Доступні ролі: client, manager, owner, off.")
        return

    await dev_service.set_role(message.from_user.id, role)
    await message.answer(f"Увімкнено режим `{role.value}`.")


@router.message(Command("as_user"))
async def as_user(message: Message, dev_service: DevService, services: BotServices) -> None:
    if message.from_user is None:
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 1:
        await message.answer("Вкажіть Telegram ID або телефон після `/as_user`.")
        return

    raw = parts[1].strip()
    target = None
    if raw.isdigit():
        target = await services.user_store.get_by_telegram_id(int(raw))
    if target is None:
        target = await services.user_store.get_by_phone(raw)
    if target is None:
        await message.answer("Користувача не знайдено.")
        return

    await dev_service.impersonate(message.from_user.id, target)
    await message.answer(
        f"Увімкнено impersonation для `{target.full_name}` ({target.telegram_id})."
    )


@router.message(Command("kill_switch"))
async def kill_switch(message: Message, dev_service: DevService) -> None:
    if message.from_user is None:
        return

    parts = (message.text or "").split(maxsplit=1)
    action = parts[1].strip().lower() if len(parts) > 1 else ""

    try:
        if action == "confirm":
            stop = await dev_service.confirm_kill_switch(message.from_user.id)
            await message.answer(
                "Kill-switch активовано. "
                f"Скасувати можна до {stop.cancel_until.isoformat(timespec='minutes')}."
            )
            return
        if action == "cancel":
            await dev_service.cancel_kill_switch(message.from_user.id)
            await message.answer("Kill-switch скасовано.")
            return

        request = await dev_service.request_kill_switch(message.from_user.id)
        await message.answer(
            "Запит на повну зупинку створено. "
            f"Другий dev має підтвердити до {request.expires_at.isoformat(timespec='minutes')}."
        )
    except ValueError as exc:
        await message.answer(str(exc))
