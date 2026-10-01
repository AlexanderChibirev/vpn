"""Фоновые задачи: напоминания и окончание подписок, проверка крипто-оплат, мониторинг 3x-ui."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from . import crypto
from .common import notify_admins, safe_send
from .config import Cfg
from .db import now
from .services import ctx, push, sub_url
from .ui import btn, fmt_dt, fmt_left, kb, url_btn

log = logging.getLogger(__name__)


async def _loop(name: str, interval: int, fn, *args) -> None:
    await asyncio.sleep(5)
    while True:
        try:
            await fn(*args)
        except Exception:  # noqa: BLE001
            log.exception("job %s", name)
        await asyncio.sleep(interval)


def start(bot: Bot) -> list[asyncio.Task]:
    return [
        asyncio.create_task(_loop("expiry", 120, check_expiry, bot)),
        asyncio.create_task(_loop("crypto", 20, check_crypto, bot)),
        asyncio.create_task(_loop("health", 60, check_health, bot)),
    ]


async def check_expiry(bot: Bot) -> None:
    db, t = ctx.db, now()
    renew = kb([btn("💳 Продлить", "buy")], [btn("🏠 Меню", "home")])
    # окончание доступа
    for sub in await db.all("SELECT * FROM subs WHERE enabled=1 AND expires_at IS NOT NULL AND expires_at<=? "
                            "AND expired_notified=0", t):
        await db.run("UPDATE subs SET expired_notified=1 WHERE id=?", sub["id"])
        await push(sub)  # 3x-ui сама выключает истёкших, но синхронизируем на всякий случай
        if sub["kind"] == "free":
            await db.run("UPDATE subs SET kind='paid' WHERE id=?", sub["id"])
            await db.run("UPDATE whitelist SET active=0 WHERE sub_pk=? AND active=1", sub["id"])
            await db.audit(None, "wl_expired", f"sub={sub['id']} {sub['note']}")
            if sub["tg_id"]:
                await safe_send(bot, sub["tg_id"], Cfg.s.t("free_expired"), renew)
            await notify_admins(bot, f"💚 Закончился бесплатный доступ: {sub['note'] or sub['email']}")
        elif sub["tg_id"]:
            await safe_send(bot, sub["tg_id"], Cfg.s.t("expired"), renew)
    # напоминания (бесплатным не шлём)
    for sub in await db.all("SELECT * FROM subs WHERE enabled=1 AND tg_id IS NOT NULL AND kind IN ('trial','paid') "
                            "AND expires_at>?", t):
        hours = sorted(Cfg.s.remind_hours_trial if sub["kind"] == "trial" else Cfg.s.remind_hours_paid, reverse=True)
        due = [i for i, h in enumerate(hours) if sub["expires_at"] - t <= h * 3600]
        if not due or due[-1] < sub["reminded"]:
            continue
        stage = due[-1] + 1
        if stage <= sub["reminded"]:
            continue
        await db.run("UPDATE subs SET reminded=? WHERE id=?", stage, sub["id"])
        u = await db.user(sub["tg_id"])
        if not u or u["banned"]:
            continue
        key = "remind_trial" if sub["kind"] == "trial" else "remind"
        await safe_send(bot, sub["tg_id"], Cfg.s.t(key, left=fmt_left(sub["expires_at"]), date=fmt_dt(sub["expires_at"])),
                        kb([btn("💳 Продлить", "buy")], [url_btn("🔌 Подключить", sub_url(sub))]))


async def check_crypto(bot: Bot) -> None:
    if not crypto.enabled():
        return
    db, t = ctx.db, now()
    await db.run("UPDATE payments SET status='expired' WHERE provider='cryptobot' AND status='pending' AND created_at<?",
                 t - 4200)
    pend = await db.all("SELECT * FROM payments WHERE provider='cryptobot' AND status='pending' AND external_id IS NOT NULL")
    if not pend:
        return
    by_ext = {p["external_id"]: p for p in pend}
    for inv in await crypto.get_invoices(list(by_ext)):
        p = by_ext.get(str(inv.get("invoice_id")))
        if not p:
            continue
        if inv.get("status") == "paid":
            from .handlers.user import finish_payment
            await finish_payment(bot, p["id"], f"crypto:{p['external_id']}")
        elif inv.get("status") == "expired":
            await db.run("UPDATE payments SET status='expired' WHERE id=?", p["id"])


_health = {"fails": 0, "down": False}


async def check_health(bot: Bot) -> None:
    problem = None
    try:
        st = await ctx.xui.status()
        state = (st.get("xray") or {}).get("state")
        if state and state != "running":
            problem = f"Xray в состоянии «{state}» — VPN не работает"
        disk = st.get("disk") or {}
        if disk.get("total") and disk.get("current", 0) / disk["total"] > 0.9:
            if not await ctx.db.kv_get("disk_warned"):
                await ctx.db.kv_set("disk_warned", "1")
                await notify_admins(bot, "⚠️ Диск сервера заполнен больше чем на 90%")
    except Exception as e:  # noqa: BLE001
        problem = f"3x-ui не отвечает ({e})"
    if problem:
        _health["fails"] += 1
        if _health["fails"] == 3 and not _health["down"]:
            _health["down"] = True
            await notify_admins(bot, f"🔴 <b>Сбой:</b> {problem}\nПроверка: <code>vpn status</code> на сервере.")
    else:
        if _health["down"]:
            await notify_admins(bot, "🟢 3x-ui и Xray снова работают.")
        _health.update(fails=0, down=False)
