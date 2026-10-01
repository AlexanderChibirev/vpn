"""Сценарии пользователя: старт, пробный период, профиль, инструкции, тарифы, оплата, рефералы, промокоды, поддержка."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from .. import crypto, services
from ..common import notify_admins, show, user_label
from ..config import Cfg
from ..db import now
from ..services import ctx, is_active, sub_url
from ..ui import (HOME, btn, connect_kb, esc, fmt_bytes, fmt_devices, fmt_dt, fmt_hours, fmt_left, kb,
                  platform_rows, qr_file, url_btn)

log = logging.getLogger(__name__)
router = Router(name="user")


class UserSt(StatesGroup):
    support = State()
    promo = State()


def trial_hours(u: dict) -> int:
    return Cfg.s.referral_trial_hours if u.get("referrer_id") and Cfg.s.referral_enabled else Cfg.s.trial_hours


def can_trial(u: dict, sub: dict | None) -> bool:
    return Cfg.s.trial_enabled and not u["trial_used"] and not sub


def until_text(sub: dict) -> str:
    return f" до {fmt_dt(sub['expires_at'])}" if sub["expires_at"] else " — бессрочно"


# ---------------- главное меню ----------------

async def home_view(u: dict) -> tuple[str, object]:
    s = Cfg.s
    sub = await ctx.db.sub_by_tg(u["tg_id"])
    name = esc(u.get("first_name") or "друг")
    support = btn("💬 Поддержка", "support") if s.support_enabled else None
    if u["banned"]:
        return s.t("home_banned"), kb([support])
    if sub and sub["kind"] == "free" and is_active(sub):
        return s.t("home_free", until=until_text(sub)), kb(
            [url_btn("🔌 Подключить", sub_url(sub))],
            [btn("👤 Мой профиль", "profile"), btn("📱 Как подключить", "ins")],
            [support])
    if sub and is_active(sub):
        return s.t("home_active", date=fmt_dt(sub["expires_at"]), left=fmt_left(sub["expires_at"])), kb(
            [url_btn("🔌 Подключить", sub_url(sub))],
            [btn("👤 Мой профиль", "profile"), btn("💳 Продлить", "buy")],
            [btn("📱 Как подключить", "ins"), btn("🎁 Пригласить друга", "ref")] if s.referral_enabled
            else [btn("📱 Как подключить", "ins")],
            [btn("🎟 Промокод", "promo"), support] if support else [btn("🎟 Промокод", "promo")])
    if sub:
        return s.t("home_expired", date=fmt_dt(sub["expires_at"])), kb(
            [btn("💳 Продлить подписку", "buy")],
            [btn("👤 Мой профиль", "profile"), btn("🎟 Промокод", "promo")],
            [support])
    if can_trial(u, sub):
        th = trial_hours(u)
        return s.t("welcome_new", name=name, trial=fmt_hours(th), devices=fmt_devices(s.device_limit)), kb(
            [btn(f"🎁 Попробовать бесплатно — {fmt_hours(th)}", "trial")],
            [btn("💳 Тарифы", "buy"), btn("📱 Как подключить", "ins")],
            [support])
    return s.t("welcome_no_trial", name=name, devices=fmt_devices(s.device_limit)) + "\n\n" + s.t("home_trial_used"), kb(
        [btn("💳 Тарифы", "buy")], [btn("🎟 Промокод", "promo"), support] if support else [btn("🎟 Промокод", "promo")])


async def send_connect(bot: Bot, chat_id: int, sub: dict, text: str) -> None:
    url = sub_url(sub)
    await bot.send_photo(chat_id, qr_file(url), caption=text, reply_markup=connect_kb(url))


@router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    fu = m.from_user
    u, is_new = await ctx.db.upsert_user(fu.id, fu.username, fu.first_name)
    if is_new:
        arg = (command.args or "").strip()
        if arg.startswith("r") and arg[1:].isdigit() and int(arg[1:]) != fu.id and await ctx.db.user(int(arg[1:])):
            await ctx.db.run("UPDATE users SET referrer_id=? WHERE tg_id=?", int(arg[1:]), fu.id)
            u["referrer_id"] = int(arg[1:])
        ref = f"\nпо приглашению {user_label(await ctx.db.user(u['referrer_id']))}" if u.get("referrer_id") else ""
        await notify_admins(bot, f"🆕 Новый пользователь: {user_label(u)}{ref}")

    entry = await services.wl_find_for_user(fu.id, fu.username)
    sub = await ctx.db.sub_by_tg(fu.id)
    if entry and (not entry["sub_pk"] or not sub or sub["id"] != entry["sub_pk"]):
        try:
            sub = await services.wl_apply(entry, fu.id)
        except Exception:
            log.exception("wl_apply")
            await m.answer(Cfg.s.t("error"))
            return
        await send_connect(bot, fu.id, sub, Cfg.s.t("free_ready", until=until_text(sub), url=sub_url(sub)))
        await notify_admins(bot, f"💚 Из белого списка подключился: {user_label(u)}\n«{esc(entry['note'])}»")
        return
    text, markup = await home_view(u)
    await m.answer(text, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data == "home")
async def cb_home(cq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    u = await ctx.db.user(cq.from_user.id)
    if not u:
        u, _ = await ctx.db.upsert_user(cq.from_user.id, cq.from_user.username, cq.from_user.first_name)
    text, markup = await home_view(u)
    await show(cq, text, markup)
    await cq.answer()


@router.message(Command("menu"))
async def cmd_menu(m: Message, state: FSMContext) -> None:
    await state.clear()
    u, _ = await ctx.db.upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    text, markup = await home_view(u)
    await m.answer(text, reply_markup=markup, disable_web_page_preview=True)


# ---------------- пробный период ----------------

@router.callback_query(F.data == "trial")
async def cb_trial(cq: CallbackQuery, bot: Bot) -> None:
    u = await ctx.db.user(cq.from_user.id)
    sub = await ctx.db.sub_by_tg(cq.from_user.id)
    if not u or u["banned"] or not can_trial(u, sub):
        await cq.answer("Пробный период уже использован", show_alert=True)
        return
    await cq.answer("Создаю доступ…")
    hours = trial_hours(u)
    try:
        sub = await services.start_trial(u["tg_id"], hours)
    except Exception:
        log.exception("trial")
        await ctx.db.run("UPDATE users SET trial_used=0 WHERE tg_id=?", u["tg_id"])
        await cq.message.answer(Cfg.s.t("error"))
        await notify_admins(bot, f"⚠️ Не удалось выдать пробный период {user_label(u)} — проверьте 3x-ui")
        return
    try:
        await cq.message.delete()
    except Exception:  # noqa: BLE001
        pass
    await send_connect(bot, u["tg_id"], sub, Cfg.s.t("trial_ready", trial=fmt_hours(hours), url=sub_url(sub)))
    await notify_admins(bot, f"🎁 Пробный период ({fmt_hours(hours)}): {user_label(u)}")


# ---------------- профиль ----------------

@router.message(Command("profile"))
@router.callback_query(F.data == "profile")
async def profile(ev: Message | CallbackQuery) -> None:
    tg_id = ev.from_user.id
    sub = await ctx.db.sub_by_tg(tg_id)
    u = await ctx.db.user(tg_id)
    if not sub or not u:
        text, markup = await home_view(u or (await ctx.db.upsert_user(tg_id, ev.from_user.username, ev.from_user.first_name))[0])
        await show(ev, text, markup)
        if isinstance(ev, CallbackQuery):
            await ev.answer()
        return
    s = Cfg.s
    us = await services.usage(sub)
    if sub["kind"] == "free" and is_active(sub):
        status = "💚 <b>Доступ бесплатный</b>" + until_text(sub)
    elif is_active(sub):
        status = f"✅ Активна до <b>{fmt_dt(sub['expires_at'])}</b> (осталось {fmt_left(sub['expires_at'])})"
    else:
        status = f"⛔️ Закончилась {fmt_dt(sub['expires_at'])}"
    traffic = fmt_bytes(us["bytes"]) + (f" из {sub['traffic_gb']} ГБ" if sub["traffic_gb"] else "")
    text = s.t("profile", status=status, kind=s.t("kind_" + sub["kind"]), devices=fmt_devices(sub["device_limit"]),
               traffic=traffic, last=fmt_dt(us["last_online"]) if us["last_online"] else "ещё не было",
               url=sub_url(sub))
    rows = [[url_btn("🔌 Подключить", sub_url(sub))]]
    if sub["kind"] == "free":
        rows.append([btn("📷 QR-код", "qr"), btn("📱 Как подключить", "ins")])
    else:
        rows.append([btn("📷 QR-код", "qr"), btn("💳 Продлить", "buy")])
        rows.append([btn("📱 Как подключить", "ins")])
    rows.append([HOME])
    await show(ev, text, kb(*rows))
    if isinstance(ev, CallbackQuery):
        await ev.answer()


@router.callback_query(F.data == "qr")
async def cb_qr(cq: CallbackQuery, bot: Bot) -> None:
    sub = await ctx.db.sub_by_tg(cq.from_user.id)
    if not sub:
        await cq.answer("Подписки пока нет", show_alert=True)
        return
    await cq.answer()
    url = sub_url(sub)
    await bot.send_photo(cq.from_user.id, qr_file(url),
                         caption=f"Отсканируйте QR-код в приложении (кнопка «+» → «QR-код»)\n\n<code>{url}</code>",
                         reply_markup=kb([HOME]))


# ---------------- инструкции ----------------

@router.message(Command("help"))
@router.callback_query(F.data == "ins")
async def ins_menu(ev: Message | CallbackQuery) -> None:
    await show(ev, Cfg.s.t("instructions_menu"), kb(*platform_rows(), [HOME]))
    if isinstance(ev, CallbackQuery):
        await ev.answer()


@router.callback_query(F.data.startswith("ins:"))
async def ins_platform(cq: CallbackQuery) -> None:
    plat = cq.data.split(":", 1)[1]
    app = Cfg.s.apps.get(plat, {})
    sub = await ctx.db.sub_by_tg(cq.from_user.id)
    text = Cfg.s.t(f"instr_{plat}", app=esc(app.get("name", "")), alt=esc(app.get("alt_name", "")),
                   extra=esc(app.get("extra_name", "")))
    rows = [[url_btn(f"⬇️ {app['name']}", app["url"])]] if app.get("url") else []
    if app.get("extra_url"):
        rows.append([url_btn(f"⬇️ {app['extra_name']}", app["extra_url"])])
    if app.get("alt_url"):
        rows.append([url_btn(f"⬇️ {app['alt_name']}", app["alt_url"])])
    if sub and is_active(sub):
        rows.append([url_btn("🔌 Подключить", sub_url(sub))])
    rows.append([btn("◀️ Другое устройство", "ins"), HOME])
    await show(cq, text, kb(*rows))
    await cq.answer()


# ---------------- тарифы и оплата ----------------

@router.message(Command("buy"))
@router.callback_query(F.data == "buy")
async def tariffs(ev: Message | CallbackQuery) -> None:
    tg_id = ev.from_user.id
    s = Cfg.s
    sub = await ctx.db.sub_by_tg(tg_id)
    u = await ctx.db.user(tg_id)
    if sub and sub["kind"] == "free" and is_active(sub):
        await show(ev, "💚 У вас бесплатный доступ — платить ничего не нужно.", kb([HOME]))
        if isinstance(ev, CallbackQuery):
            await ev.answer()
        return
    if is_active(sub):
        status = f"Сейчас активна до <b>{fmt_dt(sub['expires_at'])}</b> — новый срок добавится к текущему."
    else:
        status = "Сейчас подписки нет."
    discount = ""
    if u and u["discount_code"]:
        p = await ctx.db.one("SELECT value FROM promos WHERE code=?", u["discount_code"])
        if p:
            discount = f"🎟 Применена скидка {p['value']}% по промокоду.\n"
    if s.test_mode and tg_id in Cfg.env.admin_ids:
        discount += "🧪 <b>Тестовый режим:</b> для админов все тарифы стоят 1 ⭐ / 10 ₽.\n"
    rows = []
    for p in s.plans:
        stars, rub, _ = await services.price_for(tg_id, p)
        price = " · ".join(x for x in (f"{stars} ⭐" if s.stars_enabled else "",
                                        f"{rub} ₽" if crypto.enabled() else "") if x)
        rows.append([btn(f"{p.title} — {price}" + (f"  {p.badge}" if p.badge else ""), f"plan:{p.code}")])
    rows.append([btn("🎟 Промокод", "promo"), HOME])
    traffic = f", трафик {s.traffic_limit_gb} ГБ" if s.traffic_limit_gb else ", без лимита трафика"
    await show(ev, s.t("tariffs", status=status, devices=fmt_devices(s.device_limit), traffic=traffic,
                       discount=discount), kb(*rows))
    if isinstance(ev, CallbackQuery):
        await ev.answer()


@router.callback_query(F.data.startswith("plan:"))
async def choose_method(cq: CallbackQuery) -> None:
    plan = Cfg.s.plan(cq.data.split(":", 1)[1])
    if not plan:
        await cq.answer("Тариф больше недоступен", show_alert=True)
        return
    stars, rub, _ = await services.price_for(cq.from_user.id, plan)
    rows = []
    if Cfg.s.stars_enabled:
        rows.append([btn(f"⭐ Telegram Stars — {stars} ⭐", f"pay:stars:{plan.code}")])
    if crypto.enabled():
        rows.append([btn(f"💎 Криптовалюта (USDT, TON) — {rub} ₽", f"pay:crypto:{plan.code}")])
    rows.append([btn("◀️ Назад", "buy"), HOME])
    await show(cq, Cfg.s.t("choose_method", plan=esc(plan.title), days=plan.days), kb(*rows))
    await cq.answer()


async def _new_payment(tg_id: int, plan, provider: str, amount: float, currency: str, promo: str | None) -> int:
    test = int(Cfg.s.test_mode and tg_id in Cfg.env.admin_ids)
    return await ctx.db.run(
        "INSERT INTO payments(tg_id, provider, plan_code, days, amount, currency, status, promo_code, test, created_at) "
        "VALUES (?,?,?,?,?,?,'pending',?,?,?)", tg_id, provider, plan.code, plan.days, amount, currency, promo, test, now())


@router.callback_query(F.data.startswith("pay:stars:"))
async def pay_stars(cq: CallbackQuery, bot: Bot) -> None:
    plan = Cfg.s.plan(cq.data.split(":")[2])
    u = await ctx.db.user(cq.from_user.id)
    if not plan or not u or u["banned"]:
        await cq.answer("Недоступно", show_alert=True)
        return
    stars, _, promo = await services.price_for(u["tg_id"], plan)
    pid = await _new_payment(u["tg_id"], plan, "stars", stars, "XTR", promo)
    await bot.send_invoice(
        chat_id=u["tg_id"], title=f"Подписка: {plan.title}",
        description=f"Доступ к сервису на {plan.days} дн., до {fmt_devices(Cfg.s.device_limit)} устройств. "
                    f"Ссылка-подписка не меняется.",
        payload=f"pay:{pid}", currency="XTR", prices=[LabeledPrice(label=plan.title, amount=stars)],
    )
    await cq.answer()


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery) -> None:
    ok = False
    if q.invoice_payload.startswith("pay:"):
        p = await ctx.db.one("SELECT * FROM payments WHERE id=?", int(q.invoice_payload[4:]))
        u = await ctx.db.user(q.from_user.id)
        ok = bool(p and p["status"] == "pending" and p["tg_id"] == q.from_user.id and int(p["amount"]) == q.total_amount
                  and u and not u["banned"])
    await q.answer(ok=ok, error_message=None if ok else "Счёт устарел — откройте тарифы и создайте новый.")


@router.message(F.successful_payment)
async def successful_payment(m: Message, bot: Bot) -> None:
    sp = m.successful_payment
    pid = int(sp.invoice_payload[4:])
    await finish_payment(bot, pid, sp.telegram_payment_charge_id)


async def finish_payment(bot: Bot, pid: int, charge_id: str | None) -> None:
    try:
        res = await services.complete_payment(pid, charge_id)
    except Exception:
        log.exception("complete_payment %s", pid)
        p = await ctx.db.one("SELECT * FROM payments WHERE id=?", pid)
        await notify_admins(bot, f"🚨 Оплата #{pid} получена, но продлить подписку не удалось — проверьте 3x-ui и "
                                 f"продлите вручную (/user {p['tg_id'] if p else '?'}).")
        if p:
            await bot.send_message(p["tg_id"], "Оплата получена ✅ Активируем подписку — это может занять пару минут. "
                                               "Если что-то пойдёт не так, мы напишем.")
        return
    if not res:
        return
    p, sub = res
    await bot.send_message(p["tg_id"], Cfg.s.t("paid_ok", date=fmt_dt(sub["expires_at"])),
                           reply_markup=kb([url_btn("🔌 Подключить", sub_url(sub))], [btn("👤 Мой профиль", "profile")]))
    u = await ctx.db.user(p["tg_id"])
    amount = f"{int(p['amount'])} ⭐" if p["currency"] == "XTR" else f"{p['amount']:g} ₽"
    await notify_admins(bot, f"💰 Оплата{' (ТЕСТ)' if p['test'] else ''}: {amount} · {p['days']} дн. · "
                             f"{'Stars' if p['provider'] == 'stars' else 'CryptoBot'}\n{user_label(u)}\n"
                             f"Подписка до {fmt_dt(sub['expires_at'])}")
    ref_id = await services.referral_reward(p["tg_id"])
    if ref_id:
        try:
            await bot.send_message(ref_id, Cfg.s.t("ref_friend_paid", n=Cfg.s.referral_bonus_days))
        except Exception:  # noqa: BLE001
            pass


@router.callback_query(F.data.startswith("pay:crypto:"))
async def pay_crypto(cq: CallbackQuery) -> None:
    plan = Cfg.s.plan(cq.data.split(":")[2])
    u = await ctx.db.user(cq.from_user.id)
    if not plan or not u or u["banned"] or not crypto.enabled():
        await cq.answer("Недоступно", show_alert=True)
        return
    await cq.answer("Создаю счёт…")
    _, rub, promo = await services.price_for(u["tg_id"], plan)
    pid = await _new_payment(u["tg_id"], plan, "cryptobot", rub, "RUB", promo)
    try:
        inv = await crypto.create_invoice(rub, f"Подписка: {plan.title} ({plan.days} дн.)", f"pay:{pid}")
    except Exception:
        log.exception("cryptobot invoice")
        await ctx.db.run("UPDATE payments SET status='expired' WHERE id=?", pid)
        await cq.message.answer("Не получилось создать счёт в CryptoBot. Попробуйте позже или оплатите звёздами.")
        return
    url = inv.get("bot_invoice_url") or inv.get("mini_app_invoice_url")
    await ctx.db.run("UPDATE payments SET external_id=?, pay_url=? WHERE id=?", str(inv["invoice_id"]), url, pid)
    await show(cq, f"💎 <b>Счёт на {rub} ₽</b> ({plan.title})\n\nОплатите в @CryptoBot в USDT или TON — сумма "
                   f"пересчитается по курсу. Счёт действует 1 час.\nПосле оплаты подписка продлится автоматически "
                   f"в течение минуты.",
               kb([url_btn("💎 Оплатить", url)], [btn("🔄 Я оплатил — проверить", f"chk:{pid}")], [HOME]))


@router.callback_query(F.data.startswith("chk:"))
async def check_crypto(cq: CallbackQuery, bot: Bot) -> None:
    pid = int(cq.data[4:])
    p = await ctx.db.one("SELECT * FROM payments WHERE id=? AND tg_id=?", pid, cq.from_user.id)
    if not p:
        await cq.answer()
        return
    if p["status"] == "paid":
        await cq.answer("Оплата уже зачислена ✅", show_alert=True)
        return
    try:
        items = await crypto.get_invoices([p["external_id"]])
    except Exception:  # noqa: BLE001
        items = []
    if items and items[0].get("status") == "paid":
        await cq.answer("Оплата найдена ✅")
        await finish_payment(bot, pid, f"crypto:{p['external_id']}")
    else:
        await cq.answer("Оплата пока не поступила. Если вы уже оплатили — подождите минуту.", show_alert=True)


# ---------------- рефералы ----------------

@router.callback_query(F.data == "ref")
async def cb_ref(cq: CallbackQuery, bot: Bot) -> None:
    me = await bot.me()
    s = Cfg.s
    cnt = await ctx.db.val("SELECT COUNT(*) FROM users WHERE referrer_id=?", cq.from_user.id)
    paid = await ctx.db.val("SELECT COUNT(*) FROM users WHERE referrer_id=? AND ref_rewarded=1", cq.from_user.id)
    link = f"https://t.me/{me.username}?start=r{cq.from_user.id}"
    await show(cq, s.t("referral", friend=fmt_hours(s.referral_trial_hours), trial=fmt_hours(s.trial_hours),
                       bonus=s.referral_bonus_days, link=link, count=cnt, paid=paid),
               kb([url_btn("📤 Поделиться", f"https://t.me/share/url?url={link}")], [HOME]))
    await cq.answer()


# ---------------- промокоды ----------------

@router.callback_query(F.data == "promo")
async def cb_promo(cq: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserSt.promo)
    await show(cq, Cfg.s.t("promo_ask"), kb([btn("✖️ Отмена", "home")]))
    await cq.answer()


@router.message(StateFilter(UserSt.promo), F.text)
async def promo_entered(m: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    u = await ctx.db.user(m.from_user.id)
    if not u or u["banned"]:
        return
    try:
        res, val = await services.apply_promo(m.from_user.id, m.text)
    except Exception:
        log.exception("promo")
        await m.answer(Cfg.s.t("error"), reply_markup=kb([HOME]))
        return
    if res == "ok_days":
        sub = await ctx.db.sub_by_tg(m.from_user.id)
        await m.answer(Cfg.s.t("promo_ok_days", n=val) + f"\nДоступ до {fmt_dt(sub['expires_at'])}.",
                       reply_markup=kb([url_btn("🔌 Подключить", sub_url(sub))], [HOME]))
        await notify_admins(bot, f"🎟 Промокод {esc(m.text.strip().upper())} (+{val} дн.): {user_label(u)}")
    elif res == "ok_discount":
        await m.answer(Cfg.s.t("promo_ok_discount", n=val), reply_markup=kb([btn("💳 К тарифам", "buy")], [HOME]))
    else:
        await m.answer(Cfg.s.t("promo_" + res), reply_markup=kb([btn("🎟 Ещё раз", "promo"), HOME]))


# ---------------- поддержка ----------------

@router.message(Command("support"))
@router.callback_query(F.data == "support")
async def support_start(ev: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserSt.support)
    await show(ev, Cfg.s.t("support_ask"), kb([btn("✖️ Отмена", "home")]))
    if isinstance(ev, CallbackQuery):
        await ev.answer()


@router.message(StateFilter(UserSt.support))
async def support_msg(m: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    u = await ctx.db.user(m.from_user.id)
    sub = await ctx.db.sub_by_tg(m.from_user.id)
    st = "нет подписки"
    if sub:
        st = f"{Cfg.s.t('kind_' + sub['kind'])}, " + ("активна" if is_active(sub) else "истекла") + \
             (f" до {fmt_dt(sub['expires_at'])}" if sub["expires_at"] else "")
    header = f"📩 <b>Поддержка</b> от {user_label(u)}\n{st}\n\n<i>Ответьте на это сообщение (reply) или нажмите кнопку.</i>"
    for aid in Cfg.env.admin_ids:
        try:
            h = await bot.send_message(aid, header, reply_markup=kb([btn("↩️ Ответить", f"reply:{m.from_user.id}")]))
            c = await bot.copy_message(aid, m.chat.id, m.message_id, reply_to_message_id=h.message_id)
            for mid in (h.message_id, c.message_id):
                await ctx.db.run("INSERT OR REPLACE INTO support_map(admin_chat_id, admin_msg_id, user_id) VALUES (?,?,?)",
                                 aid, mid, m.from_user.id)
        except Exception as e:  # noqa: BLE001
            log.warning("support forward to %s: %s", aid, e)
    await m.answer(Cfg.s.t("support_sent"), reply_markup=kb([HOME]))
