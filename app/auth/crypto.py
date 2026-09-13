"""AES-256-GCM envelope for credentials at rest (spec §4.4)."""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12
KEY_BYTES = 32


class Crypto:
    def __init__(self, key_b64: str) -> None:
        key = base64.b64decode(key_b64)
        if len(key) != KEY_BYTES:
            raise ValueError("CRED_KEY must decode to 32 bytes (AES-256)")
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str, aad: bytes | None = None) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, plaintext.encode(), aad)

    def decrypt(self, blob: bytes, aad: bytes | None = None) -> str:
        if len(blob) <= NONCE_BYTES:
            raise ValueError("ciphertext too short")
        nonce, body = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
        return self._aead.decrypt(nonce, body, aad).decode()


def generate_key() -> str:
    return base64.b64encode(os.urandom(KEY_BYTES)).decode()


if __name__ == "__main__":
    print(generate_key())
