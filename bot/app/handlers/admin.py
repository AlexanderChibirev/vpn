"""Админка: статистика, пользователи, белый список, рассылка, промокоды, журнал, сервер, ответы в поддержку."""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, KeyboardButton, KeyboardButtonRequestUsers, Message, ReplyKeyboardMarkup,
                           ReplyKeyboardRemove)

from .. import services
from ..common import IsAdmin, notify_admins, safe_send, show, user_label
from ..config import Cfg
from ..db import now
from ..services import ctx, is_active, sub_url
from ..ui import btn, esc, fmt_bytes, fmt_devices, fmt_dt, fmt_left, kb, qr_file, url_btn

log = logging.getLogger(__name__)
router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

DAY = 86400
ADM = btn("◀️ Админка", "adm")


class WL(StatesGroup):
    target = State()
    note = State()
    term = State()
    date = State()
    devices = State()
    traffic = State()
    confirm = State()


class AdmSt(StatesGroup):
    find = State()
    days = State()
    reply = State()
    broadcast = State()
    promo_code = State()
    promo_value = State()
    promo_uses = State()


# ---------------- меню ----------------

def admin_kb():
    return kb(
        [btn("📊 Статистика", "adm:stats"), btn("🔎 Пользователь", "adm:find")],
        [btn("💚 Белый список", "wl:list:0"), btn("➕ Добавить своего", "wl:add")],
        [btn("🔗 Ссылка без Telegram", "wl:manual")],
        [btn("📣 Рассылка", "adm:bc"), btn("🎟 Промокоды", "adm:promos")],
        [btn("🖥 Сервер", "adm:server"), btn("📜 Журнал", "adm:audit")],
    )


@router.message(Command("admin"))
@router.callback_query(F.data == "adm")
async def adm_menu(ev: Message | CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    mode = " · 🧪 тестовый режим оплаты" if Cfg.s.test_mode else ""
    await show(ev, f"🛠 <b>Админка</b>{mode}\n\nКоманды: /user ID, /refund ID_оплаты, /testmode, /reload",
               admin_kb())
    if isinstance(ev, CallbackQuery):
        await ev.answer()


@router.message(Command("reload"))
async def cmd_reload(m: Message) -> None:
    try:
        Cfg.reload()
        await _apply_overrides()
        await m.answer(f"✅ Настройки и тексты перечитаны. Тарифов: {len(Cfg.s.plans)}.")
    except Exception as e:  # noqa: BLE001
        await m.answer(f"❌ Ошибка в settings.yml / texts.yml:\n<code>{esc(e)}</code>\nСтарые настройки остались.")


async def _apply_overrides() -> None:
    if await ctx.db.kv_get("test_mode") is not None:
        Cfg.s.test_mode = await ctx.db.kv_get("test_mode") == "1"


@router.message(Command("testmode"))
async def cmd_testmode(m: Message) -> None:
    Cfg.s.test_mode = not Cfg.s.test_mode
    await ctx.db.kv_set("test_mode", "1" if Cfg.s.test_mode else "0")
    await ctx.db.audit(m.from_user.id, "testmode", str(Cfg.s.test_mode))
    await m.answer("🧪 Тестовый режим оплаты <b>включён</b>: для админов тарифы стоят 1 ⭐ / 10 ₽. "
                   "Проверьте оплату, затем верните звёзды командой /refund ID." if Cfg.s.test_mode
                   else "Тестовый режим оплаты <b>выключен</b>.")


# ---------------- статистика ----------------

def _day_start(days_ago: int = 0) -> int:
    d = datetime.now(Cfg.env.tz).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return int(d.timestamp())


def _month_start() -> int:
    return int(datetime.now(Cfg.env.tz).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


async def _revenue(since: int) -> str:
    rows = await ctx.db.all("SELECT currency, SUM(amount) s, COUNT(*) c FROM payments WHERE status='paid' AND test=0 "
                            "AND provider!='manual' AND paid_at>=? GROUP BY currency", since)
    if not rows:
        return "0"
    return " + ".join(f"{r['s']:g} {'⭐' if r['currency'] == 'XTR' else '₽'} ({r['c']} шт.)" for r in rows)


@router.callback_query(F.data == "adm:stats")
async def adm_stats(cq: CallbackQuery) -> None:
    db = ctx.db
    t, m = _day_start(), _month_start()
    users = await db.val("SELECT COUNT(*) FROM users")
    new_t = await db.val("SELECT COUNT(*) FROM users WHERE created_at>=?", t)
    new_m = await db.val("SELECT COUNT(*) FROM users WHERE created_at>=?", m)
    act = {r["kind"]: r["c"] for r in await db.all(
        "SELECT kind, COUNT(*) c FROM subs WHERE enabled=1 AND (expires_at IS NULL OR expires_at>?) GROUP BY kind", now())}
    blocked = await db.val("SELECT COUNT(*) FROM users WHERE blocked_bot=1")
    try:
        online = len(await ctx.xui.onlines())
    except Exception:  # noqa: BLE001
        online = "?"
    text = (f"📊 <b>Статистика</b>\n\n"
            f"👥 Пользователей: <b>{users}</b> (сегодня +{new_t}, за месяц +{new_m})\n"
            f"🔕 Заблокировали бота: {blocked}\n\n"
            f"✅ Активных подписок: <b>{sum(act.values())}</b>\n"
            f"   платных: {act.get('paid', 0)} · пробных: {act.get('trial', 0)} · бесплатных: {act.get('free', 0)}\n"
            f"🟢 Онлайн сейчас: {online}\n\n"
            f"💰 Выручка сегодня: {await _revenue(t)}\n"
            f"💰 За месяц: {await _revenue(m)}\n"
            f"💰 За всё время: {await _revenue(0)}")
    await show(cq, text, kb([btn("🔄 Обновить", "adm:stats")], [ADM]))
    await cq.answer()


# ---------------- сервер ----------------

@router.callback_query(F.data == "adm:server")
async def adm_server(cq: CallbackQuery) -> None:
    try:
        st = await ctx.xui.status()
        mem, disk = st.get("mem", {}), st.get("disk", {})
        xr = st.get("xray", {})
        pct = lambda d: f"{100 * d.get('current', 0) / max(d.get('total', 1), 1):.0f}%"  # noqa: E731
        text = (f"🖥 <b>Сервер</b>\n\n"
                f"Xray: <b>{esc(xr.get('state', '?'))}</b> {esc(xr.get('version', ''))}\n"
                f"CPU: {st.get('cpu', 0):.0f}% · RAM: {pct(mem)} · Диск: {pct(disk)}\n"
                f"Аптайм: {st.get('uptime', 0) // 86400} дн. {(st.get('uptime', 0) % 86400) // 3600} ч\n"
                f"Соединений TCP: {st.get('tcpCount', '?')}\n"
                f"Онлайн клиентов: {len(await ctx.xui.onlines())}")
    except Exception as e:  # noqa: BLE001
        text = f"🔴 3x-ui не отвечает: <code>{esc(e)}</code>"
    await show(cq, text, kb([btn("🔄 Обновить", "adm:server")], [ADM]))
    await cq.answer()


# ---------------- журнал ----------------

@router.callback_query(F.data == "adm:audit")
async def adm_audit(cq: CallbackQuery) -> None:
    rows = await ctx.db.all("SELECT * FROM audit ORDER BY id DESC LIMIT 25")
    lines = [f"<code>{fmt_dt(r['ts'])}</code> {esc(r['action'])} {esc(r['details'])[:150]}" for r in rows]
    await show(cq, "📜 <b>Журнал действий</b> (последние 25)\n\n" + ("\n".join(lines) or "пусто"), kb([ADM]))
    await cq.answer()


# ---------------- пользователь ----------------

async def resolve_user(text: str) -> dict | None:
    text = text.strip()
    if text.lstrip("-").isdigit():
        return await ctx.db.user(int(text))
    if text.startswith("@") or text.replace("_", "").isalnum():
        return await ctx.db.user_by_username(text)
    return None


async def user_card(tg_id: int) -> tuple[str, object]:
    u = await ctx.db.user(tg_id)
    sub = await ctx.db.sub_by_tg(tg_id)
    lines = [f"👤 {user_label(u, tg_id)}"]
    if u:
        lines.append(f"С нами с {fmt_dt(u['created_at'], False)} · пробный: {'был' if u['trial_used'] else 'нет'}"
                     + (" · 🚫 <b>БАН</b>" if u["banned"] else "") + (" · бот заблокирован" if u["blocked_bot"] else ""))
        if u["referrer_id"]:
            lines.append(f"Пригласил: <code>{u['referrer_id']}</code>")
    if sub:
        us = await services.usage(sub)
        state = "активна" if is_active(sub) else "не активна"
        lines += ["", f"Подписка: <b>{Cfg.s.t('kind_' + sub['kind'])}</b>, {state}",
                  f"До: {fmt_dt(sub['expires_at']) if sub['expires_at'] else 'бессрочно'}",
                  f"Устройств: {fmt_devices(sub['device_limit'])} · трафик: {fmt_bytes(us['bytes'])}"
                  + (f" из {sub['traffic_gb']} ГБ" if sub["traffic_gb"] else ""),
                  f"Последнее подключение: {fmt_dt(us['last_online'])}",
                  f"Ссылка: <code>{sub_url(sub)}</code>"]
    else:
        lines.append("\nПодписки нет")
    pays = await ctx.db.one("SELECT COUNT(*) c, GROUP_CONCAT(DISTINCT currency) cur FROM payments WHERE tg_id=? AND "
                            "status='paid' AND test=0", tg_id)
    if pays and pays["c"]:
        lines.append(f"Оплат: {pays['c']}")
    rows = [[btn("+1 день", f"u:add:{tg_id}:1"), btn("+7", f"u:add:{tg_id}:7"), btn("+30", f"u:add:{tg_id}:30"),
             btn("+N…", f"u:addn:{tg_id}")]]
    if u:
        rows.append([btn("💚 В белый список", f"u:wl:{tg_id}"),
                     btn("✅ Разбанить" if u["banned"] else "🚫 Забанить", f"u:ban:{tg_id}")])
        rows.append([btn("💬 Написать", f"reply:{tg_id}")])
    rows.append([ADM])
    return "\n".join(lines), kb(*rows)


@router.message(Command("user"))
async def cmd_user(m: Message, command: CommandObject) -> None:
    if not command.args:
        await m.answer("Использование: /user 123456789 или /user @username")
        return
    u = await resolve_user(command.args)
    if not u:
        await m.answer("Не нашёл такого пользователя (он должен хотя бы раз нажать «Старт»).")
        return
    text, markup = await user_card(u["tg_id"])
    await m.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data == "adm:find")
async def adm_find(cq: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdmSt.find)
    await show(cq, "🔎 Отправьте Telegram ID или @username пользователя (или перешлите его сообщение):",
               kb([btn("✖️ Отмена", "adm")]))
    await cq.answer()


@router.message(StateFilter(AdmSt.find))
async def adm_find_input(m: Message, state: FSMContext) -> None:
    tg_id = _forward_id(m)
    u = await ctx.db.user(tg_id) if tg_id else await resolve_user(m.text or "")
    if not u:
        await m.answer("Не нашёл. Попробуйте ещё раз или «Отмена».", reply_markup=kb([btn("✖️ Отмена", "adm")]))
        return
    await state.clear()
    text, markup = await user_card(u["tg_id"])
    await m.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def _give_days(bot: Bot, admin_id: int, tg_id: int, days: int) -> None:
    sub = await services.extend(tg_id, days)
    await ctx.db.run("INSERT INTO payments(tg_id, provider, days, amount, currency, status, created_at, paid_at) "
                     "VALUES (?, 'manual', ?, 0, 'RUB', 'paid', ?, ?)", tg_id, days, now(), now())
    await ctx.db.audit(admin_id, "give_days", f"tg={tg_id} days={days} until={sub['expires_at']}")
    await safe_send(bot, tg_id, f"🎁 Вам начислено <b>+{days} дн.</b> доступа. Подписка активна до "
                                f"{fmt_dt(sub['expires_at'])}.", kb([url_btn("🔌 Подключить", sub_url(sub))]))


@router.callback_query(F.data.startswith("u:add:"))
async def u_add(cq: CallbackQuery, bot: Bot) -> None:
    _, _, tg_id, days = cq.data.split(":")
    try:
        await _give_days(bot, cq.from_user.id, int(tg_id), int(days))
    except Exception as e:  # noqa: BLE001
        await cq.answer(f"Ошибка: {e}", show_alert=True)
        return
    text, markup = await user_card(int(tg_id))
    await show(cq, text, markup)
    await cq.answer(f"+{days} дн. начислено")


@router.callback_query(F.data.startswith("u:addn:"))
async def u_addn(cq: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdmSt.days)
    await state.update_data(tg_id=int(cq.data.split(":")[2]))
    await show(cq, "Сколько дней добавить? Отправьте число:", kb([btn("✖️ Отмена", "adm")]))
    await cq.answer()


@router.message(StateFilter(AdmSt.days))
async def u_addn_input(m: Message, state: FSMContext, bot: Bot) -> None:
    if not (m.text or "").strip().isdigit() or not 0 < int(m.text) <= 3650:
        await m.answer("Нужно число от 1 до 3650.")
        return
    data = await state.get_data()
    await state.clear()
    await _give_days(bot, m.from_user.id, data["tg_id"], int(m.text))
    text, markup = await user_card(data["tg_id"])
    await m.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("u:ban:"))
async def u_ban(cq: CallbackQuery) -> None:
    tg_id = int(cq.data.split(":")[2])
    u = await ctx.db.user(tg_id)
    await services.set_banned(tg_id, not u["banned"])
    await ctx.db.audit(cq.from_user.id, "unban" if u["banned"] else "ban", f"tg={tg_id}")
    text, markup = await user_card(tg_id)
    await show(cq, text, markup)
    await cq.answer("Готово")


# ---------------- ответы в поддержку ----------------

@router.callback_query(F.data.startswith("reply:"))
async def reply_start(cq: CallbackQuery, state: FSMContext) -> None:
    uid = int(cq.data.split(":")[1])
    await state.set_state(AdmSt.reply)
    await state.update_data(uid=uid)
    await cq.message.answer(f"✍️ Напишите ответ для <code>{uid}</code> (текст, фото, файл):",
                            reply_markup=kb([btn("✖️ Отмена", "adm")]))
    await cq.answer()


async def _send_reply(bot: Bot, m: Message, uid: int) -> None:
    try:
        await bot.send_message(uid, Cfg.s.t("support_reply"))
        await bot.copy_message(uid, m.chat.id, m.message_id,
                               reply_markup=kb([btn("💬 Ответить", "support"), btn("🏠 Меню", "home")]))
        await m.answer("✅ Ответ отправлен.")
    except Exception as e:  # noqa: BLE001
        await m.answer(f"❌ Не доставлено: {esc(e)}")


@router.message(StateFilter(AdmSt.reply))
async def reply_input(m: Message, state: FSMContext, bot: Bot) -> None:
    uid = (await state.get_data())["uid"]
    await state.clear()
    await _send_reply(bot, m, uid)


@router.message(StateFilter(None), F.reply_to_message)
async def reply_by_quote(m: Message, bot: Bot) -> None:
    row = await ctx.db.one("SELECT user_id FROM support_map WHERE admin_chat_id=? AND admin_msg_id=?",
                           m.chat.id, m.reply_to_message.message_id)
    if row:
        await _send_reply(bot, m, row["user_id"])
    else:
        await m.answer("Это не обращение в поддержку. Админка: /admin")


# ---------------- белый список ----------------

def _forward_id(m: Message) -> int | None:
    o = m.forward_origin
    if o is not None and getattr(o, "sender_user", None):
        return o.sender_user.id
    if m.users_shared and m.users_shared.users:
        return m.users_shared.users[0].user_id
    return None


TERM_KB = kb([btn("♾ Бессрочно", "wlt:0")],
             [btn("1 месяц", "wlt:30"), btn("3 месяца", "wlt:90"), btn("1 год", "wlt:365")],
             [btn("📅 Ввести дату", "wlt:date")], [btn("✖️ Отмена", "wl:cancel")])
DEV_KB = kb([btn("♾ Без лимита", "wld:0")],
            [btn("1", "wld:1"), btn("2", "wld:2"), btn("3", "wld:3"), btn("5", "wld:5"), btn("10", "wld:10")],
            [btn("✖️ Отмена", "wl:cancel")])
GB_KB = kb([btn("♾ Без лимита", "wlg:0")],
           [btn("50 ГБ", "wlg:50"), btn("100 ГБ", "wlg:100"), btn("300 ГБ", "wlg:300")],
           [btn("✖️ Отмена", "wl:cancel")])


def _terms_text(d: dict) -> str:
    exp = d.get("expires_at")
    return (f"Срок: <b>{'бессрочно' if not exp else 'до ' + fmt_dt(exp, False)}</b>\n"
            f"Устройств: <b>{fmt_devices(d.get('device_limit', 0))}</b>\n"
            f"Трафик: <b>{'без лимита' if not d.get('traffic_gb') else str(d['traffic_gb']) + ' ГБ'}</b>")


@router.callback_query(F.data == "wl:cancel")
async def wl_cancel(cq: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    if data.get("mode") == "edit":
        await _show_entry(cq, data["entry_id"])
    else:
        await show(cq, "Отменено.", admin_kb())
    await cq.answer()


@router.callback_query(F.data == "wl:add")
async def wl_add_start(cq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(WL.target)
    await state.update_data(mode="add")
    await cq.message.answer(
        "➕ <b>Добавить своего</b>\n\nКак указать человека — любой способ:\n"
        "• кнопка «👤 Выбрать контакт» внизу;\n• переслать сюда любое его сообщение;\n"
        "• отправить @username или числовой Telegram ID.\n\n"
        "Если он ещё не запускал бота — запись подождёт, и при первом «Старт» он сразу получит доступ.",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(
            text="👤 Выбрать контакт", request_users=KeyboardButtonRequestUsers(
                request_id=1, user_is_bot=False, max_quantity=1, request_name=True, request_username=True))],
            [KeyboardButton(text="✖️ Отмена")]], resize_keyboard=True, one_time_keyboard=True))
    await cq.answer()


@router.callback_query(F.data.startswith("u:wl:"))
async def wl_from_card(cq: CallbackQuery, state: FSMContext) -> None:
    tg_id = int(cq.data.split(":")[2])
    u = await ctx.db.user(tg_id)
    await state.clear()
    await state.set_state(WL.note)
    await state.update_data(mode="add", tg_id=tg_id, username=u["username"] if u else None)
    await cq.message.answer(f"💚 {user_label(u, tg_id)}\n\nКто это? Короткая заметка (мама, брат, друг Вася):",
                            reply_markup=kb([btn("✖️ Отмена", "wl:cancel")]))
    await cq.answer()


@router.callback_query(F.data == "wl:manual")
async def wl_manual_start(cq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(WL.note)
    await state.update_data(mode="manual")
    await show(cq, "🔗 <b>Ссылка без Telegram</b>\n\nДля кого? Короткая заметка (например: «папа», «тётя Галя»):",
               kb([btn("✖️ Отмена", "wl:cancel")]))
    await cq.answer()


@router.message(StateFilter(WL.target))
async def wl_target(m: Message, state: FSMContext) -> None:
    if m.text == "✖️ Отмена":
        await state.clear()
        await m.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
        return
    tg_id, username = _forward_id(m), None
    if m.users_shared and m.users_shared.users and m.users_shared.users[0].username:
        username = m.users_shared.users[0].username
    if not tg_id and m.forward_origin is not None:
        await m.answer("У этого человека скрыт аккаунт при пересылке. Отправьте его @username или ID.")
        return
    if not tg_id:
        t = (m.text or "").strip()
        if t.lstrip("-").isdigit():
            tg_id = int(t)
        elif t.startswith("@") and len(t) > 3:
            username = t[1:]
            u = await ctx.db.user_by_username(username)
            tg_id = u["tg_id"] if u else None
        else:
            await m.answer("Не понял. Нужен @username, числовой ID или пересланное сообщение.")
            return
    u = await ctx.db.user(tg_id) if tg_id else None
    if u and u["username"]:
        username = u["username"]
    await state.update_data(tg_id=tg_id, username=username)
    await state.set_state(WL.note)
    who = user_label(u, tg_id) if tg_id else f"@{esc(username)}"
    wait = "" if u else "\n<i>Ещё не запускал бота — доступ включится при первом «Старт».</i>"
    await m.answer(f"Человек: {who}{wait}", reply_markup=ReplyKeyboardRemove())
    await m.answer("Кто это? Короткая заметка (мама, брат, друг Вася):", reply_markup=kb([btn("✖️ Отмена", "wl:cancel")]))


@router.message(StateFilter(WL.note), F.text)
async def wl_note(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    note = m.text.strip()[:60]
    if data.get("mode") == "edit":
        await state.clear()
        await services.wl_update(m.from_user.id, data["entry_id"], note=note)
        await _send_entry(m, data["entry_id"])
        return
    await state.update_data(note=note)
    await state.set_state(WL.term)
    await m.answer("На какой срок?", reply_markup=TERM_KB)


@router.callback_query(StateFilter(WL.term), F.data.startswith("wlt:"))
async def wl_term(cq: CallbackQuery, state: FSMContext) -> None:
    v = cq.data[4:]
    if v == "date":
        await state.set_state(WL.date)
        await show(cq, "Отправьте дату окончания в формате ДД.ММ.ГГГГ:", kb([btn("✖️ Отмена", "wl:cancel")]))
        await cq.answer()
        return
    exp = None if v == "0" else _day_start() + (int(v) + 1) * DAY - 1
    await _after_term(cq, state, exp)
    await cq.answer()


@router.message(StateFilter(WL.date), F.text)
async def wl_date(m: Message, state: FSMContext) -> None:
    try:
        d = datetime.strptime(m.text.strip(), "%d.%m.%Y").replace(hour=23, minute=59, tzinfo=Cfg.env.tz)
    except ValueError:
        await m.answer("Не понял дату. Пример: 31.12.2026")
        return
    if d.timestamp() < now():
        await m.answer("Дата уже прошла. Отправьте будущую дату.")
        return
    await _after_term(m, state, int(d.timestamp()))


async def _after_term(ev: Message | CallbackQuery, state: FSMContext, exp: int | None) -> None:
    data = await state.get_data()
    if data.get("mode") == "edit":
        await state.clear()
        await services.wl_update(ev.from_user.id, data["entry_id"], expires_at=exp)
        await _show_entry(ev, data["entry_id"])
        return
    await state.update_data(expires_at=exp)
    await state.set_state(WL.devices)
    await show(ev, "Сколько устройств?", DEV_KB)


@router.callback_query(StateFilter(WL.devices), F.data.startswith("wld:"))
async def wl_devices(cq: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    n = int(cq.data[4:])
    if data.get("mode") == "edit":
        await state.clear()
        await services.wl_update(cq.from_user.id, data["entry_id"], device_limit=n)
        await _show_entry(cq, data["entry_id"])
    else:
        await state.update_data(device_limit=n)
        await state.set_state(WL.traffic)
        await show(cq, "Лимит трафика в месяц?", GB_KB)
    await cq.answer()


@router.callback_query(StateFilter(WL.traffic), F.data.startswith("wlg:"))
async def wl_traffic(cq: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    n = int(cq.data[4:])
    if data.get("mode") == "edit":
        await state.clear()
        await services.wl_update(cq.from_user.id, data["entry_id"], traffic_gb=n)
        await _show_entry(cq, data["entry_id"])
        await cq.answer()
        return
    await state.update_data(traffic_gb=n)
    data = await state.get_data()
    await state.set_state(WL.confirm)
    if data["mode"] == "manual":
        who = "без Telegram — получите ссылку, чтобы отправить её как удобно"
    else:
        u = await ctx.db.user(data["tg_id"]) if data.get("tg_id") else None
        who = user_label(u, data.get("tg_id")) if data.get("tg_id") else f"@{esc(data.get('username'))}"
    await show(cq, f"💚 <b>Проверьте</b>\n\nКто: <b>{esc(data['note'])}</b>\nTelegram: {who}\n{_terms_text(data)}",
               kb([btn("✅ Сохранить", "wl:save")], [btn("✖️ Отмена", "wl:cancel")]))
    await cq.answer()


@router.callback_query(StateFilter(WL.confirm), F.data == "wl:save")
async def wl_save(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    await cq.answer("Сохраняю…")
    terms = dict(note=data["note"], expires_at=data.get("expires_at"), device_limit=data.get("device_limit", 0),
                 traffic_gb=data.get("traffic_gb", 0))
    try:
        if data["mode"] == "manual":
            entry, sub = await services.wl_create_manual(cq.from_user.id, **terms)
            url = sub_url(sub)
            await bot.send_photo(cq.from_user.id, qr_file(url), caption=(
                f"✅ Готово — <b>{esc(entry['note'])}</b>\n\nОтправьте человеку эту ссылку (или QR-код) и инструкцию: "
                f"установить Happ, «+» → «Добавить из буфера» → вставить ссылку.\n\n<code>{url}</code>"),
                reply_markup=kb([btn("💚 Открыть запись", f"wle:{entry['id']}")], [ADM]))
            return
        entry, sub = await services.wl_add(cq.from_user.id, tg_id=data.get("tg_id"), username=data.get("username"),
                                           **terms)
    except Exception as e:  # noqa: BLE001
        log.exception("wl_save")
        await cq.message.answer(f"❌ Не получилось: {esc(e)}")
        return
    if sub and sub["tg_id"]:
        from .user import send_connect, until_text
        try:
            await send_connect(bot, sub["tg_id"], sub, Cfg.s.t("free_ready", until=until_text(sub), url=sub_url(sub)))
            note = "Человек уже в боте — ему отправлена ссылка ✅"
        except Exception:  # noqa: BLE001
            note = "Человек есть в базе, но сообщение не доставлено (возможно, заблокировал бота). Ссылка в карточке."
    else:
        note = "Человек ещё не запускал бота — доступ выдастся при первом «Старт» ⏳"
    await show(cq, f"✅ Добавлено в белый список: <b>{esc(entry['note'])}</b>\n{note}",
               kb([btn("💚 Открыть запись", f"wle:{entry['id']}")], [btn("💚 Весь список", "wl:list:0"), ADM]))


async def _entry_view(entry_id: int) -> tuple[str, object]:
    e = await ctx.db.one("SELECT * FROM whitelist WHERE id=?", entry_id)
    if not e:
        return "Запись не найдена.", kb([ADM])
    sub = await ctx.db.sub(e["sub_pk"]) if e["sub_pk"] else None
    if e["tg_id"]:
        who = user_label(await ctx.db.user(e["tg_id"]), e["tg_id"])
    elif e["username"]:
        who = f"@{esc(e['username'])} — ⏳ ещё не запускал бота"
    else:
        who = "без Telegram (ссылка выдана вручную)"
    lines = [f"💚 <b>{esc(e['note'] or 'без заметки')}</b>" + ("" if e["active"] else " — <i>убран из списка</i>"),
             f"Telegram: {who}", f"Добавлен: {fmt_dt(e['created_at'])}", _terms_text(e)]
    if sub:
        us = await services.usage(sub)
        lines += [f"Потрачено трафика: {fmt_bytes(us['bytes'])}",
                  f"Последнее подключение: {fmt_dt(us['last_online']) if us['last_online'] else 'ещё не было'}",
                  f"Доступ: {'✅ работает' if is_active(sub) else '⛔️ выключен'}"]
    rows = []
    if sub:
        rows.append([btn("🔗 Ссылка и QR", f"wlq:{e['id']}")])
    if e["active"]:
        rows += [[btn("✏️ Срок", f"wlx:term:{e['id']}"), btn("✏️ Устройства", f"wlx:devices:{e['id']}")],
                 [btn("✏️ Трафик", f"wlx:traffic:{e['id']}"), btn("✏️ Заметка", f"wlx:note:{e['id']}")],
                 [btn("🗑 Убрать из списка", f"wlr:{e['id']}")]]
    rows.append([btn("◀️ К списку", "wl:list:0"), ADM])
    return "\n".join(lines), kb(*rows)


async def _show_entry(ev: Message | CallbackQuery, entry_id: int) -> None:
    text, markup = await _entry_view(entry_id)
    await show(ev, text, markup)


async def _send_entry(m: Message, entry_id: int) -> None:
    text, markup = await _entry_view(entry_id)
    await m.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("wle:"))
async def wl_entry(cq: CallbackQuery) -> None:
    await _show_entry(cq, int(cq.data[4:]))
    await cq.answer()


@router.callback_query(F.data.startswith("wl:list:"))
async def wl_list(cq: CallbackQuery) -> None:
    page = int(cq.data.split(":")[2])
    per = 8
    total = await ctx.db.val("SELECT COUNT(*) FROM whitelist WHERE active=1")
    rows = await ctx.db.all("SELECT * FROM whitelist WHERE active=1 ORDER BY id DESC LIMIT ? OFFSET ?", per, page * per)
    try:
        last = await ctx.xui.last_online()
    except Exception:  # noqa: BLE001
        last = {}
    lines = [f"💚 <b>Белый список</b> — {total} чел.\n"]
    buttons = []
    for e in rows:
        sub = await ctx.db.sub(e["sub_pk"]) if e["sub_pk"] else None
        who = f"@{e['username']}" if e["username"] else (str(e["tg_id"]) if e["tg_id"] else "без TG")
        if sub:
            us = await services.usage(sub)
            lo = last.get(sub["email"]) or us["last_online"]
            lo = lo // 1000 if lo and lo > 10 ** 11 else lo
            info = f"{fmt_bytes(us['bytes'])}, был {fmt_dt(lo) if lo else 'ни разу'}"
        else:
            info = "⏳ ждёт первого «Старт»"
        lines.append(f"• <b>{esc(e['note'])}</b> ({esc(who)}) — с {fmt_dt(e['created_at'], False)}; {info}")
        buttons.append([btn(f"{e['note'] or who} · {who}"[:60], f"wle:{e['id']}")])
    nav = []
    if page > 0:
        nav.append(btn("⬅️", f"wl:list:{page - 1}"))
    if (page + 1) * per < total:
        nav.append(btn("➡️", f"wl:list:{page + 1}"))
    buttons += [nav, [btn("➕ Добавить своего", "wl:add"), btn("🔗 Без Telegram", "wl:manual")], [ADM]]
    await show(cq, "\n".join(lines) if rows else "💚 Белый список пуст.", kb(*buttons))
    await cq.answer()


@router.callback_query(F.data.startswith("wlq:"))
async def wl_qr(cq: CallbackQuery, bot: Bot) -> None:
    e = await ctx.db.one("SELECT * FROM whitelist WHERE id=?", int(cq.data[4:]))
    sub = await ctx.db.sub(e["sub_pk"]) if e and e["sub_pk"] else None
    if not sub:
        await cq.answer("Ссылки ещё нет", show_alert=True)
        return
    await bot.send_photo(cq.from_user.id, qr_file(sub_url(sub)),
                         caption=f"💚 {esc(e['note'])}\n\n<code>{sub_url(sub)}</code>",
                         reply_markup=kb([btn("◀️ К записи", f"wle:{e['id']}")]))
    await cq.answer()


@router.callback_query(F.data.startswith("wlx:"))
async def wl_edit(cq: CallbackQuery, state: FSMContext) -> None:
    _, field, eid = cq.data.split(":")
    await state.clear()
    await state.update_data(mode="edit", entry_id=int(eid))
    if field == "term":
        await state.set_state(WL.term)
        await show(cq, "Новый срок (считается от сегодня):", TERM_KB)
    elif field == "devices":
        await state.set_state(WL.devices)
        await show(cq, "Сколько устройств?", DEV_KB)
    elif field == "traffic":
        await state.set_state(WL.traffic)
        await show(cq, "Лимит трафика?", GB_KB)
    else:
        await state.set_state(WL.note)
        await show(cq, "Новая заметка:", kb([btn("✖️ Отмена", "wl:cancel")]))
    await cq.answer()


@router.callback_query(F.data.startswith("wlr:"))
async def wl_remove_ask(cq: CallbackQuery) -> None:
    e = await ctx.db.one("SELECT * FROM whitelist WHERE id=?", int(cq.data[4:]))
    g = Cfg.s.unwhitelist_grace_days
    rows = [[btn("⛔️ Отключить доступ сразу", f"wlrm:disable:{e['id']}")]]
    if e["tg_id"]:
        rows.append([btn(f"💳 На обычные условия ({g} дн. на оплату)", f"wlrm:regular:{e['id']}")])
    rows.append([btn("✖️ Отмена", f"wle:{e['id']}")])
    await show(cq, f"🗑 Убрать <b>{esc(e['note'])}</b> из белого списка.\n\nЧто сделать с доступом?\n"
                   f"• <b>Отключить</b> — VPN перестанет работать сразу, человеку ничего не пишем.\n"
                   f"• <b>Обычные условия</b> — у человека останется {g} дн., бот предложит оплатить подписку.",
               kb(*rows))
    await cq.answer()


@router.callback_query(F.data.startswith("wlrm:"))
async def wl_remove(cq: CallbackQuery, bot: Bot) -> None:
    _, mode, eid = cq.data.split(":")
    sub = await services.wl_remove(cq.from_user.id, int(eid), mode)
    if mode == "regular" and sub and sub["tg_id"]:
        await safe_send(bot, sub["tg_id"],
                        f"ℹ️ Ваш бесплатный доступ завершён. Подключение работает ещё до <b>{fmt_dt(sub['expires_at'])}</b>, "
                        f"дальше — по подписке. Ссылка останется той же.", kb([btn("💳 Тарифы", "buy")]))
    await show(cq, "✅ Убран из белого списка" + (" — доступ отключён." if mode == "disable" else
                                                   " — переведён на обычные условия."),
               kb([btn("💚 Весь список", "wl:list:0"), ADM]))
    await cq.answer()


# ---------------- рассылка ----------------

@router.callback_query(F.data == "adm:bc")
async def bc_start(cq: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdmSt.broadcast)
    n = await ctx.db.val("SELECT COUNT(*) FROM users WHERE banned=0 AND blocked_bot=0")
    await show(cq, f"📣 <b>Рассылка</b> ({n} получателей)\n\nОтправьте сообщение для рассылки — текст, фото, видео. "
                   f"Сначала покажу, как оно будет выглядеть.", kb([btn("✖️ Отмена", "adm")]))
    await cq.answer()


@router.message(StateFilter(AdmSt.broadcast))
async def bc_preview(m: Message, state: FSMContext) -> None:
    await state.update_data(chat_id=m.chat.id, msg_id=m.message_id)
    n = await ctx.db.val("SELECT COUNT(*) FROM users WHERE banned=0 AND blocked_bot=0")
    await m.answer("👆 Так увидят пользователи. Отправить?",
                   reply_markup=kb([btn(f"✅ Отправить {n} пользователям", "bc:go")], [btn("✖️ Отмена", "adm")]))


@router.callback_query(StateFilter(AdmSt.broadcast), F.data == "bc:go")
async def bc_go(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    users = await ctx.db.all("SELECT tg_id FROM users WHERE banned=0 AND blocked_bot=0")
    await show(cq, f"⏳ Рассылка запущена: {len(users)} получателей. Пришлю отчёт.")
    await cq.answer()
    await ctx.db.audit(cq.from_user.id, "broadcast", f"to={len(users)}")
    asyncio.create_task(_broadcast(bot, cq.from_user.id, data["chat_id"], data["msg_id"], [u["tg_id"] for u in users]))


async def _broadcast(bot: Bot, admin_id: int, chat_id: int, msg_id: int, ids: list[int]) -> None:
    ok = fail = 0
    for uid in ids:
        try:
            await bot.copy_message(uid, chat_id, msg_id, reply_markup=kb([btn("🏠 Меню", "home")]))
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            if "blocked" in str(e).lower() or "deactivated" in str(e).lower():
                await ctx.db.run("UPDATE users SET blocked_bot=1 WHERE tg_id=?", uid)
        await asyncio.sleep(0.05)
    await safe_send(bot, admin_id, f"📣 Рассылка завершена: доставлено {ok}, не доставлено {fail}.")


# ---------------- промокоды ----------------

@router.callback_query(F.data == "adm:promos")
async def promos(cq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    rows = await ctx.db.all("SELECT * FROM promos WHERE active=1 ORDER BY created_at DESC LIMIT 30")
    lines = ["🎟 <b>Промокоды</b>\n"]
    btns = []
    for p in rows:
        what = f"+{p['value']} дн." if p["kind"] == "days" else f"−{p['value']}%"
        lim = f"{p['used']}/{p['max_uses']}" if p["max_uses"] else f"{p['used']}/∞"
        lines.append(f"<code>{esc(p['code'])}</code> — {what}, использован {lim}")
        btns.append([btn(f"🗑 {p['code']}", f"pr:del:{p['code']}")])
    if not rows:
        lines.append("Пока нет.")
    btns += [[btn("➕ Создать промокод", "pr:new")], [ADM]]
    await show(cq, "\n".join(lines), kb(*btns))
    await cq.answer()


@router.callback_query(F.data.startswith("pr:del:"))
async def promo_del(cq: CallbackQuery, state: FSMContext) -> None:
    code = cq.data[7:]
    await ctx.db.run("UPDATE promos SET active=0 WHERE code=?", code)
    await ctx.db.audit(cq.from_user.id, "promo_del", code)
    await promos(cq, state)


@router.callback_query(F.data == "pr:new")
async def promo_new(cq: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdmSt.promo_code)
    await show(cq, "Придумайте код (латиница и цифры, например <code>FRIENDS</code>) или отправьте «-» — сгенерирую сам:",
               kb([btn("✖️ Отмена", "adm:promos")]))
    await cq.answer()


@router.message(StateFilter(AdmSt.promo_code), F.text)
async def promo_code(m: Message, state: FSMContext) -> None:
    code = secrets.token_hex(3).upper() if m.text.strip() == "-" else m.text.strip().upper()
    if not code.replace("_", "").isalnum() or len(code) > 32:
        await m.answer("Только латиница, цифры и «_», до 32 символов.")
        return
    if await ctx.db.one("SELECT 1 FROM promos WHERE code=?", code):
        await m.answer("Такой код уже есть. Другой?")
        return
    await state.update_data(code=code)
    await m.answer(f"Код <code>{code}</code>. Что он даёт?",
                   reply_markup=kb([btn("📅 Дни доступа", "pr:kind:days"), btn("💸 Скидку %", "pr:kind:discount")],
                                   [btn("✖️ Отмена", "adm:promos")]))


@router.callback_query(StateFilter(AdmSt.promo_code), F.data.startswith("pr:kind:"))
async def promo_kind(cq: CallbackQuery, state: FSMContext) -> None:
    kind = cq.data[8:]
    await state.update_data(kind=kind)
    await state.set_state(AdmSt.promo_value)
    await show(cq, "Сколько дней?" if kind == "days" else "Какая скидка, % (1–100)?", kb([btn("✖️ Отмена", "adm:promos")]))
    await cq.answer()


@router.message(StateFilter(AdmSt.promo_value), F.text)
async def promo_value(m: Message, state: FSMContext) -> None:
    kind = (await state.get_data())["kind"]
    if not m.text.strip().isdigit() or not 0 < int(m.text) <= (100 if kind == "discount" else 3650):
        await m.answer("Нужно число.")
        return
    await state.update_data(value=int(m.text))
    await state.set_state(AdmSt.promo_uses)
    await m.answer("Сколько раз можно использовать всего? (0 — без ограничения)", reply_markup=kb([btn("✖️ Отмена", "adm:promos")]))


@router.message(StateFilter(AdmSt.promo_uses), F.text)
async def promo_uses(m: Message, state: FSMContext) -> None:
    if not m.text.strip().isdigit():
        await m.answer("Нужно число.")
        return
    d = await state.get_data()
    await state.clear()
    await ctx.db.run("INSERT INTO promos(code, kind, value, max_uses, created_at) VALUES (?,?,?,?,?)",
                     d["code"], d["kind"], d["value"], int(m.text), now())
    await ctx.db.audit(m.from_user.id, "promo_new", f"{d['code']} {d['kind']}={d['value']} uses={m.text}")
    me = await m.bot.me()
    await m.answer(f"✅ Промокод <code>{d['code']}</code> создан.\nПользователь вводит его в боте: «🎟 Промокод».\n"
                   f"Бот: https://t.me/{me.username}", reply_markup=kb([btn("🎟 Промокоды", "adm:promos"), ADM]))


# ---------------- возврат звёзд ----------------

@router.message(Command("refund"))
async def cmd_refund(m: Message, command: CommandObject, bot: Bot) -> None:
    if not command.args or not command.args.strip().isdigit():
        rows = await ctx.db.all("SELECT * FROM payments WHERE provider='stars' AND status='paid' ORDER BY id DESC LIMIT 10")
        lines = [f"#{p['id']} · {int(p['amount'])} ⭐ · {p['tg_id']} · {fmt_dt(p['paid_at'])}{' · ТЕСТ' if p['test'] else ''}"
                 for p in rows]
        await m.answer("Использование: /refund ID_оплаты\n\nПоследние оплаты звёздами:\n" + ("\n".join(lines) or "нет"))
        return
    p = await ctx.db.one("SELECT * FROM payments WHERE id=?", int(command.args))
    if not p or p["provider"] != "stars" or p["status"] != "paid":
        await m.answer("Такой оплаченной звёздами оплаты нет.")
        return
    try:
        await bot.refund_star_payment(user_id=p["tg_id"], telegram_payment_charge_id=p["charge_id"])
    except Exception as e:  # noqa: BLE001
        await m.answer(f"❌ Telegram отказал: {esc(e)}")
        return
    await ctx.db.run("UPDATE payments SET status='refunded' WHERE id=?", p["id"])
    sub = await ctx.db.sub_by_tg(p["tg_id"])
    if sub and sub["expires_at"]:
        new_exp = max(now(), sub["expires_at"] - p["days"] * DAY)
        await ctx.db.run("UPDATE subs SET expires_at=? WHERE id=?", new_exp, sub["id"])
        await services.push(await ctx.db.sub(sub["id"]))
    await ctx.db.audit(m.from_user.id, "refund", f"payment={p['id']} tg={p['tg_id']} stars={p['amount']}")
    await m.answer(f"✅ Возвращено {int(p['amount'])} ⭐, срок подписки уменьшен на {p['days']} дн.")
    await notify_admins(bot, f"↩️ Возврат оплаты #{p['id']} ({int(p['amount'])} ⭐) пользователю <code>{p['tg_id']}</code>")
