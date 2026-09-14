"""Canonical WhatsApp identity (B1).

The bug these pin: WhatsApp can address one human as a phone JID, a LID, or
either with a device suffix, and can switch mid-conversation. Resolving those
to different users gives one person two books, two belief maps and two alert
budgets — silently.
"""

import pytest

from app.channel.identity import (
    canonical_wa_id,
    is_addressable,
    is_lid,
    normalise_wa_id,
    split_jid,
    to_jid,
)
from app.worker import _to_inbound


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("919999999999@s.whatsapp.net", "919999999999"),
        ("919999999999:47@s.whatsapp.net", "919999999999"),  # device suffix
        ("919999999999", "919999999999"),
        ("+91 99999 99999", "919999999999"),  # human formatting
        ("+919999999999", "919999999999"),
        ("919999999999@c.us", "919999999999"),  # legacy server
    ],
)
def test_every_phone_variant_resolves_to_one_identity(raw, expected):
    assert canonical_wa_id(raw) == expected


def test_a_device_suffix_does_not_fork_a_user():
    """The same phone on a second device is the same person."""
    assert canonical_wa_id("919999999999@s.whatsapp.net") == canonical_wa_id(
        "919999999999:47@s.whatsapp.net"
    )


def test_a_lid_is_namespaced_away_from_phone_numbers():
    """A LID's digits are an opaque id, not a phone number. Letting the two
    share a keyspace would silently hand one person another's book."""
    assert canonical_wa_id("919999999999@lid") == "lid:919999999999"
    assert canonical_wa_id("919999999999@lid") != canonical_wa_id("919999999999@s.whatsapp.net")


def test_lid_detection():
    assert is_lid("123456789@lid")
    assert not is_lid("919999999999@s.whatsapp.net")


def test_groups_and_broadcasts_are_not_addressable():
    assert not is_addressable("919999999999@g.us")
    assert not is_addressable("status@broadcast")
    assert is_addressable("919999999999@s.whatsapp.net")
    assert is_addressable("123456789@lid")


def test_split_jid():
    assert split_jid("919999999999:1@s.whatsapp.net") == ("919999999999:1", "s.whatsapp.net")
    assert split_jid("919999999999") == ("919999999999", "")


def test_normalise_keeps_lid_digits_intact():
    """A LID is not a malformed phone number to be cleaned up."""
    assert normalise_wa_id("123456789@lid") == "123456789"


@pytest.mark.parametrize(
    "canonical,jid",
    [
        ("919999999999", "919999999999@s.whatsapp.net"),
        ("lid:123456789", "123456789@lid"),
    ],
)
def test_outbound_address_round_trips(canonical, jid):
    assert to_jid(canonical) == jid


def test_a_canonical_lid_never_rebuilds_as_a_phone_jid():
    """Rebuilding a LID as <digits>@s.whatsapp.net is a syntactically valid
    address for a different account — Baileys sends it happily and returns an
    id, which is exactly how a message goes to the wrong person."""
    assert to_jid("lid:123456789").endswith("@lid")


def test_bare_phone_never_reaches_the_socket_unwrapped():
    """Baileys' jidDecode throws on a bare phone."""
    assert to_jid("919999999999") == "919999999999@s.whatsapp.net"


def test_empty_input_is_empty_not_a_guess():
    assert canonical_wa_id("") == ""
    assert to_jid("") == ""


# ---- the wire ------------------------------------------------------


def test_identity_is_derived_from_the_jid_not_the_stripped_wa_id():
    """The adapter strips the JID to digits for its own logs, which cannot tell
    a LID from a phone number. If the worker trusted that field, a LID user
    would collide with whichever phone number shares those digits."""
    msg = _to_inbound({"wa_id": "123456789", "jid": "123456789@lid", "text": "hi", "ts": "0"})
    assert msg.wa_id == "lid:123456789"


def test_the_stripped_wa_id_is_used_when_there_is_no_jid():
    """Console REPL and messages that predate the jid field."""
    msg = _to_inbound({"wa_id": "919999999999", "text": "hi", "ts": "0"})
    assert msg.wa_id == "919999999999"


def test_device_suffix_is_dropped_on_the_way_in():
    msg = _to_inbound({"jid": "919999999999:32@s.whatsapp.net", "text": "hi", "ts": "0"})
    assert msg.wa_id == "919999999999"
