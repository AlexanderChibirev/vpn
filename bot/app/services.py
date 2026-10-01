"""Бизнес-логика: подписки, белый список, оплаты, промокоды. Хендлеры только вызывают эти функции."""
from __future__ import annotations

import logging
import secrets
import uuid as uuidlib
from dataclasses import dataclass

from .config import Cfg
from .db import DB, now
from .xui import Xui

log = logging.getLogger(__name__)
DAY = 86400


@dataclass
class Ctx:
    db: DB = None  # type: ignore[assignment]
    xui: Xui = None  # type: ignore[assignment]


ctx = Ctx()  # заполняется в main: ctx.db / ctx.xui


def sub_url(sub: dict) -> str:
    return Cfg.env.sub_base_url + sub["sub_id"]


def ip_limit(device_limit: int) -> int:
    return device_limit + Cfg.s.ip_limit_extra if device_limit > 0 else 0


def is_active(sub: dict | None) -> bool:
    return bool(sub and sub["enabled"] and (sub["expires_at"] is None or sub["expires_at"] > now()))


async def push(sub: dict) -> None:
    """Синхронизирует подписку из базы бота в 3x-ui."""
    banned = False
    if sub["tg_id"]:
        u = await ctx.db.user(sub["tg_id"])
        banned = bool(u and u["banned"])
    await ctx.xui.upsert_client(
        email=sub["email"], uuid=sub["uuid"], sub_id=sub["sub_id"],
        expiry_ts=sub["expires_at"], limit_ip=ip_limit(sub["device_limit"]), traffic_gb=sub["traffic_gb"],
        tg_id=sub["tg_id"], comment=(sub["note"] or ""), enable=bool(sub["enabled"]) and not banned,
    )


async def _comment_for(tg_id: int | None) -> str:
    if not tg_id:
        return ""
    u = await ctx.db.user(tg_id)
    if not u:
        return str(tg_id)
    return f"@{u['username']}" if u["username"] else (u["first_name"] or str(tg_id))


async def ensure_sub(tg_id: int | None, *, kind: str, expires_at: int | None, device_limit: int,
                     traffic_gb: int, note: str | None = None, email: str | None = None) -> dict:
    """Создаёт или обновляет подписку. Ссылка (sub_id) при обновлении не меняется."""
    sub = await ctx.db.sub_by_tg(tg_id) if tg_id else None
    note = note if note is not None else (sub["note"] if sub else await _comment_for(tg_id))
    if sub:
        await ctx.db.run(
            "UPDATE subs SET kind=?, expires_at=?, device_limit=?, traffic_gb=?, enabled=1, note=?, reminded=0, "
            "expired_notified=0 WHERE id=?", kind, expires_at, device_limit, traffic_gb, note, sub["id"])
        pk = sub["id"]
    else:
        email = email or (f"tg{tg_id}" if tg_id else f"m{secrets.token_hex(4)}")
        pk = await ctx.db.run(
            "INSERT INTO subs(tg_id, email, uuid, sub_id, kind, expires_at, device_limit, traffic_gb, note, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            tg_id, email, str(uuidlib.uuid4()), secrets.token_urlsafe(12).replace("-", "x").replace("_", "y"),
            kind, expires_at, device_limit, traffic_gb, note, now())
    sub = await ctx.db.sub(pk)
    await push(sub)
    return sub


async def start_trial(tg_id: int, hours: int) -> dict:
    await ctx.db.run("UPDATE users SET trial_used=1 WHERE tg_id=?", tg_id)
    return await ensure_sub(tg_id, kind="trial", expires_at=now() + hours * 3600,
                            device_limit=Cfg.s.device_limit, traffic_gb=Cfg.s.traffic_limit_gb)


async def extend(tg_id: int, days: int, *, kind: str = "paid") -> dict:
    sub = await ctx.db.sub_by_tg(tg_id)
    if sub and sub["kind"] == "free" and sub["expires_at"] is None:
        return sub  # бессрочный бесплатный — продлевать нечего
    base = max(now(), (sub["expires_at"] or 0) if sub and sub["enabled"] else 0)
    if sub and sub["kind"] == "free":
        kind = "free"
    devices = sub["device_limit"] if sub and sub["kind"] == "free" else Cfg.s.device_limit
    traffic = sub["traffic_gb"] if sub and sub["kind"] == "free" else Cfg.s.traffic_limit_gb
    return await ensure_sub(tg_id, kind=kind, expires_at=base + days * DAY, device_limit=devices, traffic_gb=traffic)


async def disable(sub: dict) -> None:
    await ctx.db.run("UPDATE subs SET enabled=0 WHERE id=?", sub["id"])
    await push(await ctx.db.sub(sub["id"]))


async def set_banned(tg_id: int, banned: bool) -> None:
    await ctx.db.run("UPDATE users SET banned=? WHERE tg_id=?", int(banned), tg_id)
    sub = await ctx.db.sub_by_tg(tg_id)
    if sub:
        await push(sub)


async def usage(sub: dict) -> dict:
    """Трафик и последнее подключение из 3x-ui. Если панель недоступна — пустые значения."""
    try:
        t = await ctx.xui.traffic(sub["email"])
    except Exception as e:  # noqa: BLE001
        log.warning("traffic %s: %s", sub["email"], e)
        return {"bytes": None, "last_online": None}
    lo = int(t.get("lastOnline") or 0) // 1000
    return {"bytes": int(t.get("up") or 0) + int(t.get("down") or 0), "last_online": lo or None}


# ---------------- белый список ----------------

async def wl_find_for_user(tg_id: int, username: str | None) -> dict | None:
    row = await ctx.db.one("SELECT * FROM whitelist WHERE active=1 AND tg_id=? ORDER BY id DESC LIMIT 1", tg_id)
    if not row and username:
        row = await ctx.db.one(
            "SELECT * FROM whitelist WHERE active=1 AND tg_id IS NULL AND lower(username)=lower(?) ORDER BY id DESC LIMIT 1",
            username)
    return row


async def wl_apply(entry: dict, tg_id: int | None) -> dict:
    """Выдаёт бесплатный доступ по записи белого списка."""
    sub = await ensure_sub(tg_id, kind="free", expires_at=entry["expires_at"], device_limit=entry["device_limit"],
                           traffic_gb=entry["traffic_gb"], note=entry["note"] or None)
    await ctx.db.run("UPDATE whitelist SET tg_id=?, sub_pk=? WHERE id=?", tg_id, sub["id"], entry["id"])
    return sub


async def wl_add(admin_id: int, *, tg_id: int | None, username: str | None, note: str, expires_at: int | None,
                 device_limit: int, traffic_gb: int) -> tuple[dict, dict | None]:
    """Добавляет в белый список. Возвращает (запись, подписку или None, если человек ещё не запускал бота)."""
    username = username.lstrip("@") if username else None
    if not tg_id and username:
        u = await ctx.db.user_by_username(username)
        tg_id = u["tg_id"] if u else None
    if tg_id:  # одна активная запись на человека
        await ctx.db.run("UPDATE whitelist SET active=0 WHERE tg_id=? AND active=1", tg_id)
    pk = await ctx.db.run(
        "INSERT INTO whitelist(tg_id, username, note, expires_at, device_limit, traffic_gb, created_at, created_by) "
        "VALUES (?,?,?,?,?,?,?,?)", tg_id, username, note, expires_at, device_limit, traffic_gb, now(), admin_id)
    entry = await ctx.db.one("SELECT * FROM whitelist WHERE id=?", pk)
    sub = None
    if tg_id and await ctx.db.user(tg_id):
        sub = await wl_apply(entry, tg_id)
    await ctx.db.audit(admin_id, "wl_add", f"#{pk} tg={tg_id} @{username} note={note!r} exp={expires_at} "
                                           f"dev={device_limit} gb={traffic_gb}")
    return entry, sub


async def wl_create_manual(admin_id: int, *, note: str, expires_at: int | None, device_limit: int,
                           traffic_gb: int) -> tuple[dict, dict]:
    pk = await ctx.db.run(
        "INSERT INTO whitelist(note, expires_at, device_limit, traffic_gb, created_at, created_by) VALUES (?,?,?,?,?,?)",
        note, expires_at, device_limit, traffic_gb, now(), admin_id)
    entry = await ctx.db.one("SELECT * FROM whitelist WHERE id=?", pk)
    sub = await wl_apply(entry, None)
    await ctx.db.audit(admin_id, "wl_manual", f"#{pk} note={note!r} email={sub['email']}")
    return entry, sub


async def wl_update(admin_id: int, entry_id: int, **fields) -> dict:
    allowed = {k: v for k, v in fields.items() if k in ("note", "expires_at", "device_limit", "traffic_gb")}
    sets = ", ".join(f"{k}=?" for k in allowed)
    await ctx.db.run(f"UPDATE whitelist SET {sets} WHERE id=?", *allowed.values(), entry_id)
    entry = await ctx.db.one("SELECT * FROM whitelist WHERE id=?", entry_id)
    if entry["sub_pk"]:
        sub = await ctx.db.sub(entry["sub_pk"])
        await ctx.db.run("UPDATE subs SET kind='free', expires_at=?, device_limit=?, traffic_gb=?, note=?, enabled=1, "
                         "reminded=0, expired_notified=0 WHERE id=?", entry["expires_at"], entry["device_limit"],
                         entry["traffic_gb"], entry["note"], sub["id"])
        await push(await ctx.db.sub(sub["id"]))
    await ctx.db.audit(admin_id, "wl_update", f"#{entry_id} {allowed}")
    return entry


async def wl_remove(admin_id: int, entry_id: int, mode: str) -> dict | None:
    """mode: 'disable' — выключить доступ; 'regular' — перевести на обычные условия с запасом в N дней."""
    entry = await ctx.db.one("SELECT * FROM whitelist WHERE id=?", entry_id)
    await ctx.db.run("UPDATE whitelist SET active=0 WHERE id=?", entry_id)
    sub = await ctx.db.sub(entry["sub_pk"]) if entry and entry["sub_pk"] else None
    if sub:
        if mode == "regular" and sub["tg_id"]:
            await ctx.db.run("UPDATE subs SET kind='paid', expires_at=?, device_limit=?, traffic_gb=?, enabled=1, "
                             "reminded=0, expired_notified=0 WHERE id=?",
                             now() + Cfg.s.unwhitelist_grace_days * DAY, Cfg.s.device_limit, Cfg.s.traffic_limit_gb,
                             sub["id"])
        else:
            await ctx.db.run("UPDATE subs SET kind='paid', enabled=0, expires_at=? WHERE id=?", now(), sub["id"])
        sub = await ctx.db.sub(sub["id"])
        await push(sub)
    await ctx.db.audit(admin_id, "wl_remove", f"#{entry_id} mode={mode}")
    return sub


# ---------------- оплаты ----------------

async def price_for(tg_id: int, plan) -> tuple[int, int, str | None]:
    """Цена (stars, rub) с учётом скидочного промокода и тестового режима."""
    u = await ctx.db.user(tg_id)
    stars, rub, code = plan.stars, plan.rub, None
    if u and u["discount_code"]:
        p = await ctx.db.one("SELECT * FROM promos WHERE code=? AND active=1", u["discount_code"])
        if p and p["kind"] == "discount":
            k = (100 - p["value"]) / 100
            stars, rub, code = max(1, round(stars * k)), max(1, round(rub * k)), p["code"]
    if Cfg.s.test_mode and tg_id in Cfg.env.admin_ids:
        stars, rub = 1, 10
    return stars, rub, code


async def complete_payment(payment_id: int, charge_id: str | None = None) -> tuple[dict, dict] | None:
    """Идемпотентно: помечает оплату, продлевает подписку, начисляет реферальный бонус. None — если уже обработана."""
    p = await ctx.db.one("SELECT * FROM payments WHERE id=?", payment_id)
    if not p or p["status"] == "paid":
        return None
    await ctx.db.run("UPDATE payments SET status='paid', paid_at=?, charge_id=COALESCE(?, charge_id) WHERE id=?",
                     now(), charge_id, payment_id)
    sub = await extend(p["tg_id"], p["days"])
    if p["promo_code"]:
        await ctx.db.run("UPDATE users SET discount_code=NULL WHERE tg_id=?", p["tg_id"])
        await ctx.db.run("INSERT OR IGNORE INTO promo_uses(code, tg_id, used_at) VALUES (?,?,?)",
                         p["promo_code"], p["tg_id"], now())
    return await ctx.db.one("SELECT * FROM payments WHERE id=?", payment_id), sub


async def referral_reward(tg_id: int) -> int | None:
    """После первой оплаты друга — бонус пригласившему. Возвращает tg_id пригласившего."""
    if not Cfg.s.referral_enabled:
        return None
    u = await ctx.db.user(tg_id)
    if not u or not u["referrer_id"] or u["ref_rewarded"]:
        return None
    await ctx.db.run("UPDATE users SET ref_rewarded=1 WHERE tg_id=?", tg_id)
    ref = await ctx.db.user(u["referrer_id"])
    if not ref or ref["banned"]:
        return None
    await extend(ref["tg_id"], Cfg.s.referral_bonus_days)
    return ref["tg_id"]


# ---------------- промокоды ----------------

async def apply_promo(tg_id: int, code: str) -> tuple[str, int | None]:
    """Возвращает (результат, значение): ok_days / ok_discount / not_found / used / exhausted."""
    p = await ctx.db.one("SELECT * FROM promos WHERE upper(code)=upper(?) AND active=1", code.strip())
    if not p or (p["expires_at"] and p["expires_at"] < now()):
        return "not_found", None
    if await ctx.db.one("SELECT 1 FROM promo_uses WHERE code=? AND tg_id=?", p["code"], tg_id):
        return "used", None
    if p["max_uses"] and p["used"] >= p["max_uses"]:
        return "exhausted", None
    await ctx.db.run("UPDATE promos SET used=used+1 WHERE code=?", p["code"])
    if p["kind"] == "days":
        await ctx.db.run("INSERT INTO promo_uses(code, tg_id, used_at) VALUES (?,?,?)", p["code"], tg_id, now())
        await extend(tg_id, p["value"])
        return "ok_days", p["value"]
    await ctx.db.run("UPDATE users SET discount_code=? WHERE tg_id=?", p["code"], tg_id)
    return "ok_discount", p["value"]
