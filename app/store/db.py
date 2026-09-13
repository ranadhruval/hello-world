"""Postgres store.

Plain psycopg rather than the ORM: the queries are few and explicit, and the
schema lives in schema.sql where it can be read in one sitting.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row

from app.config import settings

log = logging.getLogger(__name__)


@dataclass
class Credentials:
    user_id: int
    totp_token_enc: bytes
    totp_secret_enc: bytes
    state: str
    mint_failures: int


class Store:
    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = (dsn or settings().database_url).replace("postgresql+psycopg://", "postgresql://")

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    # ---- users -----------------------------------------------------

    async def user_id_for(self, wa_id: str, create: bool = True) -> int | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE wa_id = %s", (wa_id,))
            row = cur.fetchone()
            if row:
                return row["id"]
            if not create:
                return None
            cur.execute("INSERT INTO users (wa_id) VALUES (%s) RETURNING id", (wa_id,))
            conn.commit()
            return cur.fetchone()["id"]

    async def wa_id_for(self, user_id: int) -> str | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT wa_id FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return row["wa_id"] if row else None

    async def is_linked(self, user_id: int) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM credentials WHERE user_id = %s AND state <> 'dead'", (user_id,)
            )
            return cur.fetchone() is not None

    # ---- credentials (CredentialStore protocol) --------------------

    async def get_credentials(self, user_id: int) -> Credentials:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, totp_token_enc, totp_secret_enc, state, mint_failures "
                "FROM credentials WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise LookupError(f"no credentials for user {user_id}")
        return Credentials(**row)

    async def put_credentials(self, user_id: int, token_enc: bytes, secret_enc: bytes) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO credentials (user_id, totp_token_enc, totp_secret_enc)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET
                     totp_token_enc = EXCLUDED.totp_token_enc,
                     totp_secret_enc = EXCLUDED.totp_secret_enc,
                     state = 'ok', mint_failures = 0""",
                (user_id, token_enc, secret_enc),
            )
            conn.commit()

    async def delete_user_data(self, user_id: int) -> None:
        """DELETE /me must actually remove everything (spec §24.3)."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            conn.commit()

    async def set_credential_state(self, user_id: int, state: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            failures = "mint_failures + 1" if state in {"failing", "dead"} else "0"
            cur.execute(
                f"UPDATE credentials SET state = %s, mint_failures = {failures} WHERE user_id = %s",
                (state, user_id),
            )
            conn.commit()

    async def log_token_mint(self, user_id: int, ok: bool, error: str | None = None) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO token_mints (user_id, ok, error) VALUES (%s, %s, %s)",
                (user_id, ok, error),
            )
            if ok:
                cur.execute(
                    "UPDATE credentials SET last_mint_at = now() WHERE user_id = %s", (user_id,)
                )
            conn.commit()

    async def mints_in_last_24h(self) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM token_mints WHERE minted_at > now() - interval '24 hours'"
            )
            return cur.fetchone()["n"]

    # ---- link flow -------------------------------------------------

    def new_link_token(self, wa_id: str) -> str:
        """Signed, single-use, 10-minute TTL (spec §4.1)."""
        cfg = settings()
        nonce = secrets.token_hex(8)
        payload = f"{wa_id}{nonce}{int(time.time())}"
        token = hmac.new(cfg.link_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO link_requests (token, wa_id_hash, expires_at) VALUES (%s, %s, %s)",
                (
                    token,
                    hashlib.sha256(wa_id.encode()).hexdigest(),
                    datetime.now() + timedelta(seconds=cfg.link_ttl_seconds),
                ),
            )
            conn.commit()
        return token

    def consume_link_token(self, token: str, wa_id: str) -> bool:
        """Verify and burn. Returns False if expired, used, or for another chat."""
        wa_hash = hashlib.sha256(wa_id.encode()).hexdigest()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE link_requests SET used = true
                   WHERE token = %s AND wa_id_hash = %s AND used = false AND expires_at > now()
                   RETURNING token""",
                (token, wa_hash),
            )
            hit = cur.fetchone() is not None
            conn.commit()
        return hit

    def link_request_wa_hash(self, token: str) -> str | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT wa_id_hash FROM link_requests "
                "WHERE token = %s AND used = false AND expires_at > now()",
                (token,),
            )
            row = cur.fetchone()
            return row["wa_id_hash"] if row else None

    # ---- traces ----------------------------------------------------

    async def log_message(
        self,
        user_id: int | None,
        direction: str,
        text: str | None,
        channel_msg_id: str | None = None,
        intent: str | None = None,
        latency_ms: int | None = None,
    ) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO messages (user_id, direction, channel_msg_id, text, intent, latency_ms)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (user_id, direction, channel_msg_id, text, intent, latency_ms),
            )
            conn.commit()
            return cur.fetchone()["id"]

    async def log_trace(
        self, message_id: int, path: str, intent: str, tool_calls=None, error: str | None = None
    ) -> None:
        import json

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO traces (message_id, path, intent, tool_calls, error)
                   VALUES (%s, %s, %s, %s, %s)""",
                (message_id, path, intent, json.dumps(tool_calls or []), error),
            )
            conn.commit()
