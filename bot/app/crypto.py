"""Crypto Pay API (@CryptoBot). Документация: https://help.send.tg/en/articles/10279948-crypto-pay-api"""
from __future__ import annotations

import httpx

from .config import Cfg


class CryptoPayError(Exception):
    pass


def enabled() -> bool:
    return bool(Cfg.s.crypto_enabled and Cfg.env.cryptopay_token)


async def _call(method: str, params: dict) -> dict:
    host = "https://testnet-pay.crypt.bot" if Cfg.env.cryptopay_testnet else "https://pay.crypt.bot"
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.post(f"{host}/api/{method}", json=params,
                            headers={"Crypto-Pay-API-Token": Cfg.env.cryptopay_token})
    data = r.json()
    if not data.get("ok"):
        raise CryptoPayError(str(data.get("error")))
    return data["result"]


async def create_invoice(amount_rub: int, description: str, payload: str) -> dict:
    """Счёт в рублях; пользователь платит USDT/TON по курсу CryptoBot."""
    return await _call("createInvoice", {
        "currency_type": "fiat", "fiat": "RUB", "amount": str(amount_rub),
        "accepted_assets": Cfg.s.crypto_assets, "description": description[:1024],
        "payload": payload, "expires_in": 3600, "allow_comments": False, "allow_anonymous": False,
    })


async def get_invoices(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    res = await _call("getInvoices", {"invoice_ids": ",".join(ids)})
    return res.get("items", [])


async def get_me() -> dict:
    return await _call("getMe", {})
