"""База бота (SQLite). Время везде — unix-секунды UTC."""
from __future__ import annotations

import time
from typing import Any

import aiosqlite

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    created_at INTEGER NOT NULL,
    last_seen INTEGER,
    referrer_id INTEGER,
    ref_rewarded INTEGER NOT NULL DEFAULT 0,
    trial_used INTEGER NOT NULL DEFAULT 0,
    banned INTEGER NOT NULL DEFAULT 0,
    blocked_bot INTEGER NOT NULL DEFAULT 0,
    discount_code TEXT
);
CREATE TABLE IF NOT EXISTS subs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER UNIQUE,
    email TEXT NOT NULL UNIQUE,
    uuid TEXT NOT NULL,
    sub_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,              -- trial | paid | free
    expires_at INTEGER,              -- NULL = бессрочно
    device_limit INTEGER NOT NULL DEFAULT 0,   -- 0 = без лимита
    traffic_gb INTEGER NOT NULL DEFAULT 0,     -- 0 = без лимита
    enabled INTEGER NOT NULL DEFAULT 1,
    note TEXT,
    created_at INTEGER NOT NULL,
    reminded INTEGER NOT NULL DEFAULT 0,       -- сколько напоминаний уже отправлено к текущей дате окончания
    expired_notified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS whitelist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER,
    username TEXT,
    note TEXT,
    expires_at INTEGER,
    device_limit INTEGER NOT NULL DEFAULT 0,
    traffic_gb INTEGER NOT NULL DEFAULT 0,
    sub_pk INTEGER,
    created_at INTEGER NOT NULL,
    created_by INTEGER,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS wl_tg ON whitelist(tg_id);
CREATE INDEX IF NOT EXISTS wl_un ON whitelist(username);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    provider TEXT NOT NULL,          -- stars | cryptobot | manual
    plan_code TEXT,
    days INTEGER NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,          -- XTR | RUB
    status TEXT NOT NULL,            -- pending | paid | refunded | expired
    external_id TEXT,
    charge_id TEXT UNIQUE,
    pay_url TEXT,
    promo_code TEXT,
    test INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    paid_at INTEGER
);
CREATE TABLE IF NOT EXISTS promos (
    code TEXT PRIMARY KEY,
    kind TEXT NOT NULL,              -- days | discount
    value INTEGER NOT NULL,
    max_uses INTEGER NOT NULL DEFAULT 0,
    used INTEGER NOT NULL DEFAULT 0,
    expires_at INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS promo_uses (
    code TEXT NOT NULL,
    tg_id INTEGER NOT NULL,
    used_at INTEGER NOT NULL,
    PRIMARY KEY (code, tg_id)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    admin_id INTEGER,
    action TEXT NOT NULL,
    details TEXT
);
CREATE TABLE IF NOT EXISTS support_map (
    admin_chat_id INTEGER NOT NULL,
    admin_msg_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (admin_chat_id, admin_msg_id)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def now() -> int:
    return int(time.time())


class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def one(self, sql: str, *args: Any) -> dict | None:
        async with self.conn.execute(sql, args) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def all(self, sql: str, *args: Any) -> list[dict]:
        async with self.conn.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def val(self, sql: str, *args: Any) -> Any:
        row = await self.one(sql, *args)
        return next(iter(row.values())) if row else None

    async def run(self, sql: str, *args: Any) -> int:
        cur = await self.conn.execute(sql, args)
        await self.conn.commit()
        return cur.lastrowid

    # --- users
    async def upsert_user(self, tg_id: int, username: str | None, first_name: str | None) -> tuple[dict, bool]:
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO users(tg_id, username, first_name, created_at, last_seen) VALUES (?,?,?,?,?)",
            (tg_id, username, first_name, now(), now()))
        created = cur.rowcount == 1
        if not created:
            await self.conn.execute("UPDATE users SET username=?, first_name=?, last_seen=?, blocked_bot=0 WHERE tg_id=?",
                                    (username, first_name, now(), tg_id))
        await self.conn.commit()
        return await self.one("SELECT * FROM users WHERE tg_id=?", tg_id), created

    async def user(self, tg_id: int) -> dict | None:
        return await self.one("SELECT * FROM users WHERE tg_id=?", tg_id)

    async def user_by_username(self, username: str) -> dict | None:
        return await self.one("SELECT * FROM users WHERE lower(username)=lower(?)", username.lstrip("@"))

    # --- subs
    async def sub_by_tg(self, tg_id: int) -> dict | None:
        return await self.one("SELECT * FROM subs WHERE tg_id=?", tg_id)

    async def sub(self, pk: int) -> dict | None:
        return await self.one("SELECT * FROM subs WHERE id=?", pk)

    # --- audit
    async def audit(self, admin_id: int | None, action: str, details: str = "") -> None:
        await self.run("INSERT INTO audit(ts, admin_id, action, details) VALUES (?,?,?,?)", now(), admin_id, action, details)

    async def kv_get(self, key: str, default: str | None = None) -> str | None:
        v = await self.val("SELECT value FROM kv WHERE key=?", key)
        return default if v is None else v

    async def kv_set(self, key: str, value: str) -> None:
        await self.run("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, value)
