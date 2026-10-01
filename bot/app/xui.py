"""Клиент API 3x-ui v3 (Bearer-токен, эндпоинты /panel/api/clients/*)."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

GB = 1024 ** 3


class XuiError(Exception):
    pass


class Xui:
    def __init__(self, base_url: str, token: str, inbound_ids: list[int]):
        self.inbound_ids = inbound_ids
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/panel/api",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=httpx.Timeout(15.0, connect=5.0),
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def _req(self, method: str, path: str, json: dict | None = None) -> object:
        try:
            r = await self.http.request(method, path, json=json)
        except httpx.HTTPError as e:
            raise XuiError(f"3x-ui недоступна: {e.__class__.__name__}") from e
        if r.status_code == 404 and method == "GET":
            return None
        if r.status_code >= 400:
            raise XuiError(f"3x-ui HTTP {r.status_code} на {path}")
        data = r.json()
        if not data.get("success"):
            msg = data.get("msg", "")
            if method == "GET" and ("not found" in msg.lower() or "record not found" in msg.lower()):
                return None
            raise XuiError(f"3x-ui: {msg or 'ошибка'} ({path})")
        return data.get("obj")

    @staticmethod
    def _payload(email: str, uuid: str, sub_id: str, expiry_ts: int | None, limit_ip: int,
                 traffic_gb: int, tg_id: int | None, comment: str, enable: bool) -> dict:
        return {
            "id": uuid, "email": email, "subId": sub_id, "flow": "xtls-rprx-vision",
            "expiryTime": (expiry_ts or 0) * 1000, "limitIp": max(limit_ip, 0), "limitHwid": 0,
            "totalGB": max(traffic_gb, 0) * GB, "tgId": tg_id or 0, "comment": comment[:100], "enable": enable,
        }

    async def add_client(self, **kw) -> None:
        await self._req("POST", "/clients/add", {"client": self._payload(**kw), "inboundIds": self.inbound_ids})

    async def update_client(self, **kw) -> None:
        await self._req("POST", f"/clients/update/{kw['email']}", self._payload(**kw))

    async def upsert_client(self, **kw) -> None:
        if await self.get_client(kw["email"]):
            await self.update_client(**kw)
        else:
            await self.add_client(**kw)

    async def get_client(self, email: str) -> dict | None:
        obj = await self._req("GET", f"/clients/get/{email}")
        return obj.get("client") if isinstance(obj, dict) else None

    async def delete_client(self, email: str) -> None:
        await self._req("POST", f"/clients/del/{email}", {})

    async def traffic(self, email: str) -> dict:
        """{'up','down','lastOnline'(ms),'enable','expiryTime'}"""
        obj = await self._req("GET", f"/clients/traffic/{email}")
        return obj if isinstance(obj, dict) else {}

    async def last_online(self) -> dict[str, int]:
        obj = await self._req("POST", "/clients/lastOnline", {})
        return obj if isinstance(obj, dict) else {}

    async def onlines(self) -> list[str]:
        obj = await self._req("POST", "/clients/onlines", {})
        return obj if isinstance(obj, list) else []

    async def status(self) -> dict:
        obj = await self._req("GET", "/server/status")
        return obj if isinstance(obj, dict) else {}
