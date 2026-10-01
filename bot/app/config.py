"""Настройки: секреты из переменных окружения (.env), всё остальное — из settings.yml / texts.yml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


def _ids(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(";", ",").split(",") if x.strip()]


@dataclass
class Env:
    bot_token: str
    admin_ids: list[int]
    xui_url: str  # http://127.0.0.1:PORT/BASE_PATH/
    xui_token: str
    xui_inbound_ids: list[int]
    sub_base_url: str  # https://host:2096/path/
    db_path: str
    config_dir: Path
    cryptopay_token: str
    cryptopay_testnet: bool
    tz: ZoneInfo
    server_name: str

    @classmethod
    def load(cls) -> "Env":
        e = os.environ
        base = e.get("XUI_BASE_PATH", "/").strip("/")
        xui_url = f"http://127.0.0.1:{e['XUI_PORT']}/" + (base + "/" if base else "")
        sub = e["SUB_BASE_URL"].rstrip("/") + "/"
        return cls(
            bot_token=e["BOT_TOKEN"],
            admin_ids=_ids(e.get("ADMIN_IDS", "")),
            xui_url=xui_url,
            xui_token=e["XUI_API_TOKEN"],
            xui_inbound_ids=_ids(e.get("XUI_INBOUND_IDS", "1")),
            sub_base_url=sub,
            db_path=e.get("DB_PATH", "/data/bot.db"),
            config_dir=Path(e.get("CONFIG_DIR", "/config")),
            cryptopay_token=e.get("CRYPTOPAY_TOKEN", "").strip(),
            cryptopay_testnet=e.get("CRYPTOPAY_TESTNET", "true").lower() in ("1", "true", "yes"),
            tz=ZoneInfo(e.get("TZ", "Europe/Moscow")),
            server_name=e.get("SERVER_NAME", "main"),
        )


@dataclass
class Plan:
    code: str
    title: str
    days: int
    stars: int
    rub: int
    badge: str = ""


@dataclass
class Settings:
    trial_enabled: bool = True
    trial_hours: int = 24
    referral_trial_hours: int = 72
    device_limit: int = 3
    ip_limit_extra: int = 1
    traffic_limit_gb: int = 0
    remind_hours_paid: list[int] = field(default_factory=lambda: [24, 3])
    remind_hours_trial: list[int] = field(default_factory=lambda: [3])
    plans: list[Plan] = field(default_factory=list)
    stars_enabled: bool = True
    crypto_enabled: bool = True
    crypto_assets: str = "USDT,TON"
    test_mode: bool = False
    referral_enabled: bool = True
    referral_bonus_days: int = 7
    unwhitelist_grace_days: int = 3
    support_enabled: bool = True
    apps: dict[str, dict] = field(default_factory=dict)
    texts: dict[str, str] = field(default_factory=dict)

    def plan(self, code: str) -> Plan | None:
        return next((p for p in self.plans if p.code == code), None)

    def t(self, key: str, **kw) -> str:
        s = self.texts.get(key, key)
        return s.format(**kw) if kw else s


def load_settings(config_dir: Path) -> Settings:
    data = yaml.safe_load((config_dir / "settings.yml").read_text("utf-8")) or {}
    texts = yaml.safe_load((config_dir / "texts.yml").read_text("utf-8")) or {}
    trial = data.get("trial", {})
    pay = data.get("payments", {})
    ref = data.get("referral", {})
    rem = data.get("reminders", {})
    return Settings(
        trial_enabled=trial.get("enabled", True),
        trial_hours=int(trial.get("hours", 24)),
        referral_trial_hours=int(ref.get("friend_trial_hours", trial.get("hours", 24))),
        device_limit=int(data.get("device_limit", 3)),
        ip_limit_extra=int(data.get("ip_limit_extra", 1)),
        traffic_limit_gb=int(data.get("traffic_limit_gb", 0)),
        remind_hours_paid=[int(x) for x in rem.get("paid_hours_before", [24, 3])],
        remind_hours_trial=[int(x) for x in rem.get("trial_hours_before", [3])],
        plans=[Plan(code=str(p["code"]), title=p["title"], days=int(p["days"]), stars=int(p["stars"]),
                    rub=int(p.get("rub", 0)), badge=p.get("badge", "")) for p in data.get("plans", [])],
        stars_enabled=pay.get("stars", True),
        crypto_enabled=pay.get("cryptobot", True),
        crypto_assets=pay.get("cryptobot_assets", "USDT,TON"),
        test_mode=pay.get("test_mode", False),
        referral_enabled=ref.get("enabled", True),
        referral_bonus_days=int(ref.get("bonus_days", 7)),
        unwhitelist_grace_days=int(data.get("unwhitelist_grace_days", 3)),
        support_enabled=data.get("support", True),
        apps=data.get("apps", {}),
        texts={k: str(v).strip("\n") for k, v in texts.items()},
    )


class Cfg:
    """Глобальный доступ к настройкам; settings можно перечитать командой /reload."""
    env: Env
    s: Settings

    @classmethod
    def init(cls) -> None:
        cls.env = Env.load()
        cls.s = load_settings(cls.env.config_dir)

    @classmethod
    def reload(cls) -> None:
        cls.s = load_settings(cls.env.config_dir)
