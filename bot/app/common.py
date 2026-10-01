"""Общие помощники для хендлеров."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject

from .config import Cfg

log = logging.getLogger(__name__)


class IsAdmin(BaseFilter):
    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and user.id in Cfg.env.admin_ids)


async def notify_admins(bot: Bot, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    for aid in Cfg.env.admin_ids:
        try:
            await bot.send_message(aid, text, reply_markup=kb, disable_web_page_preview=True)
        except Exception as e:  # noqa: BLE001
            log.warning("notify admin %s: %s", aid, e)


async def show(target: Message | CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Показывает экран: редактирует текущее сообщение (кнопки) или шлёт новое."""
    if isinstance(target, CallbackQuery):
        msg = target.message
        try:
            if msg and msg.text is not None:
                await msg.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
                return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
        if msg:
            await msg.answer(text, reply_markup=kb, disable_web_page_preview=True)
        return
    await target.answer(text, reply_markup=kb, disable_web_page_preview=True)


async def safe_send(bot: Bot, chat_id: int, text: str, kb: InlineKeyboardMarkup | None = None) -> bool:
    try:
        await bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)
        return True
    except TelegramForbiddenError:
        from .services import ctx
        await ctx.db.run("UPDATE users SET blocked_bot=1 WHERE tg_id=?", chat_id)
    except Exception as e:  # noqa: BLE001
        log.warning("send %s: %s", chat_id, e)
    return False


def user_label(u: dict | None, tg_id: int | None = None) -> str:
    from .ui import esc
    if not u:
        return f"<code>{tg_id}</code>" if tg_id else "—"
    name = esc(u.get("first_name") or "")
    un = f" @{esc(u['username'])}" if u.get("username") else ""
    return f'<a href="tg://user?id={u["tg_id"]}">{name or u["tg_id"]}</a>{un} (<code>{u["tg_id"]}</code>)'
