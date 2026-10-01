"""Точка входа: python -m app"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import StateFilter
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, CallbackQuery, Message

from . import scheduler, services
from .common import notify_admins
from .config import Cfg
from .db import DB
from .handlers import admin, user
from .xui import Xui

log = logging.getLogger("bot")

fallback = Router(name="fallback")


@fallback.message(StateFilter(None), F.chat.type == "private")
async def any_message(m: Message) -> None:
    u, _ = await services.ctx.db.upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    text, markup = await user.home_view(u)
    await m.answer(text, reply_markup=markup, disable_web_page_preview=True)


@fallback.callback_query()
async def stale_button(cq: CallbackQuery) -> None:
    await cq.answer("Кнопка устарела — откройте /menu", show_alert=False)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    Cfg.init()
    db = DB(Cfg.env.db_path)
    await db.open()
    xui = Xui(Cfg.env.xui_url, Cfg.env.xui_token, Cfg.env.xui_inbound_ids)
    services.ctx.db, services.ctx.xui = db, xui
    await admin._apply_overrides()

    bot = Bot(Cfg.env.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_routers(admin.router, user.router, fallback)

    cmds = [BotCommand(command="start", description="Главное меню"),
            BotCommand(command="profile", description="Мой профиль и ссылка"),
            BotCommand(command="buy", description="Тарифы и продление"),
            BotCommand(command="help", description="Как подключить"),
            BotCommand(command="support", description="Написать в поддержку")]
    await bot.set_my_commands(cmds)
    for aid in Cfg.env.admin_ids:
        try:
            await bot.set_my_commands(cmds + [BotCommand(command="admin", description="Админка")],
                                      scope=BotCommandScopeChat(chat_id=aid))
        except Exception:  # noqa: BLE001
            pass  # админ ещё не открывал бота
    try:
        await bot.set_my_short_description("Быстрое защищённое подключение за минуту. Первый день — бесплатно.")
        await bot.set_my_description("Нажмите «Старт» — бот сразу выдаст доступ на пробный день, ссылку и QR-код "
                                     "для подключения. Работает на iPhone, Android, Windows и Mac.")
    except Exception:  # noqa: BLE001
        pass

    tasks = scheduler.start(bot)
    await notify_admins(bot, "✅ Бот запущен")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for t in tasks:
            t.cancel()
        await xui.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
