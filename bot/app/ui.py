"""Форматирование и клавиатуры."""
from __future__ import annotations

import html
import io
from datetime import datetime

import qrcode
from aiogram.types import BufferedInputFile, InlineKeyboardButton as B, InlineKeyboardMarkup

from .config import Cfg
from .db import now


def esc(s: object) -> str:
    return html.escape(str(s or ""))


def fmt_dt(ts: int | None, with_time: bool = True) -> str:
    if not ts:
        return "—"
    d = datetime.fromtimestamp(ts, Cfg.env.tz)
    return d.strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def fmt_left(ts: int) -> str:
    sec = max(0, ts - now())
    days, hours = sec // 86400, (sec % 86400) // 3600
    if days >= 1:
        return f"{days} {plural(days, 'день', 'дня', 'дней')}" + (f" {hours} ч" if days < 3 and hours else "")
    if hours >= 1:
        return f"{hours} ч {(sec % 3600) // 60} мин"
    return f"{max(1, sec // 60)} мин"


def fmt_hours(h: int) -> str:
    if h % 24 == 0:
        d = h // 24
        return f"{d} {plural(d, 'день', 'дня', 'дней')}" if d != 1 else "1 день"
    return f"{h} {plural(h, 'час', 'часа', 'часов')}"


def fmt_bytes(b: int | None) -> str:
    if b is None:
        return "нет данных"
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if b < 1024 or unit == "ТБ":
            return f"{b:.0f} {unit}" if unit in ("Б", "КБ") else f"{b:.2f} {unit}".replace(".", ",")
        b /= 1024
    return ""


def fmt_devices(n: int) -> str:
    return "без лимита" if not n else str(n)


def qr_file(url: str) -> BufferedInputFile:
    img = qrcode.make(url, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return BufferedInputFile(buf.getvalue(), filename="qr.png")


def kb(*rows: list[B]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def btn(text: str, data: str) -> B:
    return B(text=text, callback_data=data)


def url_btn(text: str, url: str) -> B:
    return B(text=text, url=url)


HOME = btn("🏠 Меню", "home")


def platform_rows() -> list[list[B]]:
    return [[btn("🍎 iPhone", "ins:ios"), btn("🤖 Android", "ins:android")],
            [btn("💻 Windows", "ins:windows"), btn("🍏 Mac", "ins:macos")]]


def connect_kb(url: str) -> InlineKeyboardMarkup:
    return kb([url_btn("🔌 Подключить", url)], *platform_rows(), [HOME])
