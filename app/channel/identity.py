"""Canonical WhatsApp identity.

WhatsApp addresses one human in more than one way. The same person can arrive
as a phone JID (``919999999999@s.whatsapp.net``), as a LID
(``123456789@lid``, whose digits are an opaque id and *not* a phone number),
and with a device suffix (``919999999999:47@s.whatsapp.net``) — and the
addressing can change mid-conversation.

Left alone that produces a second `users` row for the same person: a second
book, a second belief map, a second alert budget, a second link token. The bug
does not need two users to appear; it needs one user and one addressing switch.

Two defences, because neither is sufficient alone:

1. **Normalisation** collapses the variants that share digits — device
   suffixes, ``+`` prefixes, separators. Cheap and exact.
2. **Aliasing** handles the variants that do not. A LID and a phone JID for the
   same human carry genuinely different digits, so no amount of string work
   relates them. Hermes resolves this by reading the lid↔phone mapping files
   Baileys writes into its session store; our Baileys (6.7.24) knows about LIDs
   (``isLidUser``) but does not persist those mappings, so that route is closed.

   Instead we anchor on the thing we have and they do not: **the brokerage
   account**. Two addresses that link to the same Groww account are the same
   person, provably, and `Store.bind_account` merges them at link time.

Everything that resolves a user goes through `canonical_wa_id` so that
authorisation and addressing can never drift apart — the failure Hermes calls
out is not "we normalise badly", it is "two lookups disagree".
"""

from __future__ import annotations

import re

# A JID is <user>[:<device>]@<server>. Servers we accept; anything else is a
# group, a broadcast, or something we should not be answering.
DM_SERVERS = frozenset({"s.whatsapp.net", "c.us", "lid"})

_DIGITS = re.compile(r"\D+")
_BARE_PHONE = re.compile(r"^\+?[\d\s().\-]+$")


def split_jid(value: str) -> tuple[str, str]:
    """``('919999999999', 's.whatsapp.net')`` — user part and server."""
    raw = (value or "").strip()
    if "@" not in raw:
        return raw, ""
    user, _, server = raw.partition("@")
    return user, server.lower()


def normalise_wa_id(value: str) -> str:
    """Strip a JID, LID or bare phone down to its bare identifier.

    ``919999999999:47@s.whatsapp.net``, ``+91 99999 99999`` and
    ``919999999999`` all become ``919999999999``. A LID keeps its own digits —
    they are a different identifier, not a malformed phone number, and
    pretending otherwise is how the two get conflated.
    """
    user, _ = split_jid(value)
    user = user.split(":", 1)[0]  # drop the device suffix
    if _BARE_PHONE.fullmatch(user):
        return _DIGITS.sub("", user)
    return user.strip()


def is_lid(value: str) -> bool:
    return split_jid(value)[1] == "lid"


def canonical_wa_id(value: str) -> str:
    """The identifier we key a user on.

    LIDs are namespaced so an opaque id can never collide with a phone number
    that happens to share its digits. That collision is unlikely and silent,
    which is the worst combination — it would hand one person another's book.
    """
    user, server = split_jid(value)
    bare = normalise_wa_id(value)
    if not bare:
        return ""
    return f"lid:{bare}" if server == "lid" else bare


def to_jid(value: str) -> str:
    """Best-effort outbound address when there is no inbound JID to echo.

    Only used by the console REPL and by a proactive send for a user we have
    never had an inbound JID from. Echoing beats rebuilding every time: this
    cannot recover a LID, and says so by returning it in LID form rather than
    inventing a phone JID that would deliver to a different account.
    """
    if not value:
        return ""
    if value.startswith("lid:"):
        return f"{value[4:]}@lid"
    user, server = split_jid(value)
    if server:
        return f"{user.split(':', 1)[0]}@{server}"
    bare = normalise_wa_id(value)
    # Baileys' jidDecode throws on a bare phone, so never hand one to the socket.
    return f"{bare}@s.whatsapp.net" if bare else ""


def is_addressable(value: str) -> bool:
    """False for groups, broadcasts and anything we should not reply into."""
    _, server = split_jid(value)
    return server in DM_SERVERS if server else bool(normalise_wa_id(value))
