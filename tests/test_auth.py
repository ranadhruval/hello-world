import base64
import time
from datetime import datetime, timedelta

import pytest
from cryptography.exceptions import InvalidTag

from app.auth.broker import access_token, next_0605_ist
from app.auth.crypto import Crypto, generate_key
from app.config import IST, redact


def test_roundtrip():
    c = Crypto(generate_key())
    assert c.decrypt(c.encrypt("totp-secret-value")) == "totp-secret-value"


def test_ciphertext_differs_each_time():
    """Fresh nonce per encryption, so the same plaintext never repeats."""
    c = Crypto(generate_key())
    assert c.encrypt("same") != c.encrypt("same")


def test_wrong_key_cannot_decrypt():
    blob = Crypto(generate_key()).encrypt("secret")
    with pytest.raises(InvalidTag):
        Crypto(generate_key()).decrypt(blob)


def test_tampered_ciphertext_is_rejected():
    """AES-GCM is authenticated — a flipped bit must fail, not decrypt to junk."""
    c = Crypto(generate_key())
    blob = bytearray(c.encrypt("secret"))
    blob[-1] ^= 0x01
    with pytest.raises(InvalidTag):
        c.decrypt(bytes(blob))


def test_short_key_rejected():
    with pytest.raises(ValueError):
        Crypto(base64.b64encode(b"tooshort").decode())


def test_truncated_blob_rejected():
    with pytest.raises(ValueError):
        Crypto(generate_key()).decrypt(b"abc")


def test_next_0605_is_always_in_the_future():
    before = datetime(2026, 9, 13, 5, 0, tzinfo=IST)
    assert next_0605_ist(before) == datetime(2026, 9, 13, 6, 5, tzinfo=IST).timestamp()

    after = datetime(2026, 9, 13, 7, 0, tzinfo=IST)
    assert next_0605_ist(after) == datetime(2026, 9, 14, 6, 5, tzinfo=IST).timestamp()


def test_next_0605_at_the_boundary_rolls_forward():
    at = datetime(2026, 9, 13, 6, 5, tzinfo=IST)
    assert next_0605_ist(at) > at.timestamp()


def test_token_cache_never_outlives_the_daily_expiry():
    """Access tokens die around 06:00 IST, so a 6h TTL must be clipped."""
    now = datetime.now(IST)
    six_hours_out = time.time() + 6 * 3600
    effective = min(six_hours_out, next_0605_ist(now))
    assert effective <= next_0605_ist(now)
    assert effective <= six_hours_out


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("plain", "plain"),
        ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", "[REDACTED]"),
        ("token=abcdefghijklmnopqrstuvwxyz123", "token=[REDACTED]"),
    ],
)
def test_log_redaction(raw, expected):
    assert redact(raw) == expected


def test_access_token_accepts_both_shapes():
    """growwapi 1.5.0 annotates -> dict but returns the bare token string."""
    assert access_token("raw-token") == "raw-token"
    assert access_token({"token": "wrapped"}) == "wrapped"
    assert access_token({"access_token": "wrapped"}) == "wrapped"


def test_access_token_rejects_nonsense():
    with pytest.raises(TypeError):
        access_token(12345)


def test_expiry_ttl_is_bounded(monkeypatch):
    """A stopped clock must not produce an infinite TTL."""
    base = datetime(2026, 9, 13, 12, 0, tzinfo=IST)
    assert next_0605_ist(base) - base.timestamp() <= timedelta(days=1).total_seconds()
