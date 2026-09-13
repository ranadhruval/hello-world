"""Channel abstraction (spec §3.2).

Every channel sits behind this interface from commit #1 so the Baileys →
Cloud API migration is one new class, not a refactor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

Kind = Literal["text", "image", "buttons", "list"]

MAX_BUTTONS = 3
MAX_LIST_ROWS = 10
MAX_CHARS = 4096


@dataclass(frozen=True)
class InboundMessage:
    channel_msg_id: str
    wa_id: str  # E.164 without '+'
    text: str | None
    ts: int  # epoch ms
    # The channel's own address for this chat. Carried verbatim so replies
    # echo it instead of rebuilding one — WhatsApp also uses <id>@lid, whose
    # digits are an opaque id rather than a phone number.
    jid: str | None = None
    media_url: str | None = None
    media_mime: str | None = None
    quoted_id: str | None = None


@dataclass
class OutboundMessage:
    wa_id: str
    kind: Kind
    text: str
    jid: str | None = None
    image_bytes: bytes | None = None
    buttons: list[str] = field(default_factory=list)
    list_rows: list[tuple[str, str]] = field(default_factory=list)
    reply_to: str | None = None
    # Keyed on (user_id, rule_id, dedupe_key, date) in Phase 2 so a retried
    # alert delivery cannot double-send.
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if len(self.buttons) > MAX_BUTTONS:
            raise ValueError(f"WhatsApp allows at most {MAX_BUTTONS} buttons")
        if len(self.list_rows) > MAX_LIST_ROWS:
            raise ValueError(f"WhatsApp allows at most {MAX_LIST_ROWS} list rows")
        if len(self.text) > MAX_CHARS:
            raise ValueError(f"message exceeds the {MAX_CHARS}-char WhatsApp limit")


@runtime_checkable
class Channel(Protocol):
    async def send(self, msg: OutboundMessage) -> str:
        """Deliver and return the channel's message id.

        Must only return once the channel has acknowledged with an id —
        a bare `return` is not delivery confirmation (spec §18.2).
        """
        ...

    async def typing(self, wa_id: str, on: bool) -> None: ...

    async def health(self) -> bool: ...
