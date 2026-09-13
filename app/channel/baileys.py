"""Baileys bridge client (spec §3.1).

The Node process in adapter/ owns the WhatsApp session and exposes a small
HTTP surface; this class is the Python side of it. Migrating to the Cloud API
means writing one sibling class, not touching anything upstream.
"""

from __future__ import annotations

import base64

import httpx

from app.channel.base import OutboundMessage


class BaileysChannel:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def send(self, msg: OutboundMessage) -> str:
        payload: dict[str, object] = {
            "wa_id": msg.wa_id,
            "jid": msg.jid,
            "kind": msg.kind,
            "text": msg.text,
            "reply_to": msg.reply_to,
            "idempotency_key": msg.idempotency_key,
        }
        if msg.buttons:
            payload["buttons"] = msg.buttons
        if msg.list_rows:
            payload["list_rows"] = [{"id": i, "label": lbl} for i, lbl in msg.list_rows]
        if msg.image_bytes:
            payload["image_b64"] = base64.b64encode(msg.image_bytes).decode()

        resp = await self._client.post("/send", json=payload)
        resp.raise_for_status()
        msg_id = resp.json().get("channel_msg_id")
        if not msg_id:
            # Spec §18.2: a send is not delivered because the call returned.
            raise RuntimeError("adapter accepted the send but returned no channel_msg_id")
        return msg_id

    async def typing(self, wa_id: str, on: bool) -> None:
        await self._client.post("/typing", json={"wa_id": wa_id, "on": on})

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/health")
            return resp.status_code == 200 and resp.json().get("connected") is True
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
