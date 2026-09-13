"""Console channel — lets the whole pipeline run without a WhatsApp session.

Used by `python -m app.repl` and by the eval harness.
"""

from __future__ import annotations

import itertools
import time

from app.channel.base import InboundMessage, OutboundMessage


class ConsoleChannel:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []
        self._ids = itertools.count(1)

    async def send(self, msg: OutboundMessage) -> str:
        self.sent.append(msg)
        msg_id = f"console-{next(self._ids)}"
        print(msg.text)
        if msg.buttons:
            print("   " + "  ".join(f"[{b}]" for b in msg.buttons))
        for row_id, label in msg.list_rows:
            print(f"   · {label}  ({row_id})")
        if msg.image_bytes:
            print(f"   <image {len(msg.image_bytes)} bytes>")
        return msg_id

    async def typing(self, wa_id: str, on: bool) -> None:
        return None

    async def health(self) -> bool:
        return True


def inbound(
    text: str,
    wa_id: str = "919999999999",
    msg_id: str | None = None,
    jid: str | None = None,
) -> InboundMessage:
    return InboundMessage(
        channel_msg_id=msg_id or f"in-{time.time_ns()}",
        wa_id=wa_id,
        text=text,
        ts=int(time.time() * 1000),
        jid=jid,
    )
