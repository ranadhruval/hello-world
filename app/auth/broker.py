"""TOTP token broker (spec §4.3).

Why TOTP and not the API-key flow: the API Key & Secret flow requires a human
to click approve on the Groww Cloud API Keys page every day. An agent that
wakes at 06:00 to watch a market cannot depend on that. This single decision
is what makes Phase 2 possible at all (invariant I4).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import pyotp
from growwapi import GrowwAPI

from app.config import IST, settings

log = logging.getLogger(__name__)


class CredentialRow(Protocol):
    totp_token_enc: bytes
    totp_secret_enc: bytes
    state: str
    mint_failures: int


class CredentialStore(Protocol):
    async def get_credentials(self, user_id: int) -> CredentialRow: ...
    async def log_token_mint(self, user_id: int, ok: bool, error: str | None = None) -> None: ...
    async def mints_in_last_24h(self) -> int: ...
    async def set_credential_state(self, user_id: int, state: str) -> None: ...


class MintBudgetExhausted(RuntimeError):
    """Groww caps /v1/token/api/access at 150 per 24h across the account."""


class CredentialDead(RuntimeError):
    """The user revoked the TOTP token. Stop retrying, ask them to re-link."""


def next_0605_ist(now: datetime | None = None) -> float:
    """Epoch seconds of the next 06:05 IST.

    Access tokens expire daily around 06:00 IST. Expiring our cache just after
    that boundary means the first request of the morning mints, rather than
    every request between 06:00 and whenever the 6h TTL happens to lapse.
    """
    now = now or datetime.now(IST)
    target = now.replace(hour=6, minute=5, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


@dataclass
class _Cached:
    client: GrowwAPI
    expires_at: float


class TokenBroker:
    """One GrowwAPI client per user, refreshed lazily and on schedule."""

    def __init__(self, store: CredentialStore, crypto) -> None:
        self._store = store
        self._crypto = crypto
        self._clients: dict[int, _Cached] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def client(self, user_id: int) -> GrowwAPI:
        cached = self._clients.get(user_id)
        if cached and time.time() < cached.expires_at:
            return cached.client

        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            # Re-check: a concurrent caller may have minted while we waited.
            cached = self._clients.get(user_id)
            if cached and time.time() < cached.expires_at:
                return cached.client
            return await self._mint(user_id)

    async def invalidate(self, user_id: int) -> None:
        """Drop the cached client so the next call re-mints.

        Called on a 401 from any downstream request (spec §4.6).
        """
        self._clients.pop(user_id, None)

    async def _mint(self, user_id: int) -> GrowwAPI:
        row = await self._store.get_credentials(user_id)
        if row.state == "dead":
            raise CredentialDead(f"credentials for user {user_id} are revoked")

        cfg = settings()
        if await self._store.mints_in_last_24h() >= cfg.max_mints_per_day:
            raise MintBudgetExhausted(
                f"{cfg.max_mints_per_day} token mints in 24h — refusing to mint, "
                "serve cache or fail loudly"
            )

        totp_token = self._crypto.decrypt(row.totp_token_enc)
        totp_secret = self._crypto.decrypt(row.totp_secret_enc)

        try:
            code = pyotp.TOTP(totp_secret).now()
            token = await asyncio.to_thread(
                GrowwAPI.get_access_token, api_key=totp_token, totp=code
            )
        except Exception as exc:
            await self._store.log_token_mint(user_id, ok=False, error=type(exc).__name__)
            if row.mint_failures + 1 >= 3:
                await self._store.set_credential_state(user_id, "dead")
            else:
                await self._store.set_credential_state(user_id, "failing")
            raise

        client = GrowwAPI(access_token(token))
        expires_at = min(time.time() + cfg.token_ttl_seconds, next_0605_ist())
        self._clients[user_id] = _Cached(client, expires_at)
        await self._store.log_token_mint(user_id, ok=True)
        await self._store.set_credential_state(user_id, "ok")
        log.info("minted access token for user=%s ttl=%ss", user_id, int(expires_at - time.time()))
        return client


def access_token(token) -> str:
    """growwapi annotates get_access_token as -> dict but returns response['token'].

    The annotation is wrong as of growwapi 1.5.0. Accept either shape so a
    future release that actually returns the envelope does not break auth.
    """
    if isinstance(token, str):
        return token
    if isinstance(token, dict):
        for key in ("token", "access_token", "accessToken"):
            if key in token:
                return token[key]
    raise TypeError(f"unexpected access-token shape: {type(token).__name__}")
