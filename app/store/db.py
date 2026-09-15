"""Postgres store.

Plain psycopg rather than the ORM: the queries are few and explicit, and the
schema lives in schema.sql where it can be read in one sitting.

Every method is one of four shapes — `_one`, `_all`, `_exec`, or a `_tx` block
for the handful that need more than one statement in a transaction. psycopg's
connection context manager commits on clean exit and rolls back on exception,
so no method spells out commit or rollback.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.channel.identity import canonical_wa_id
from app.config import settings
from app.obs.incidents import Incident, IncidentState
from app.outbox import Entry as OutboxEntry
from app.watcher.suggestions import Source, State, Suggestion

log = logging.getLogger(__name__)


@dataclass
class Credentials:
    user_id: int
    totp_token_enc: bytes
    totp_secret_enc: bytes
    state: str
    mint_failures: int


def _incident(r: dict) -> Incident:
    return Incident(
        id=r["id"],
        job_name=r["job_name"],
        signature=r["signature"],
        state=IncidentState(r["state"]),
        occurrences=r["occurrences"],
        last_seen=r["last_seen"],
        alerted_at=r["alerted_at"],
    )


class Store:
    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = (dsn or settings().database_url).replace(
            "postgresql+psycopg://", "postgresql://"
        )

    @contextmanager
    def _tx(self) -> Iterator[psycopg.Cursor]:
        """One transaction. Commits on clean exit, rolls back on exception."""
        with psycopg.connect(self._dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            yield cur

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._tx() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._tx() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def _exec(self, sql: str, params: tuple = ()) -> dict | None:
        """A write. Returns the RETURNING row when there is one."""
        with self._tx() as cur:
            cur.execute(sql, params)
            return cur.fetchone() if cur.description else None

    # ---- users -----------------------------------------------------

    async def user_id_for(self, wa_id: str, create: bool = True) -> int | None:
        """Resolve a chat address to a user, through the alias table.

        Every caller goes through here so authorisation and addressing cannot
        disagree — two lookups that resolve a person differently is the actual
        failure, not imperfect normalisation (app/channel/identity.py).
        """
        key = canonical_wa_id(wa_id)
        if not key:
            return None
        with self._tx() as cur:
            cur.execute("SELECT user_id FROM wa_aliases WHERE wa_id = %s", (key,))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE wa_aliases SET last_seen = now() WHERE wa_id = %s", (key,))
                return row["user_id"]

            # Pre-alias rows: users created before this table existed.
            cur.execute("SELECT id FROM users WHERE wa_id = %s", (key,))
            row = cur.fetchone()
            if row:
                cur.execute(
                    "INSERT INTO wa_aliases (wa_id, user_id, source) VALUES (%s, %s, 'inbound') "
                    "ON CONFLICT (wa_id) DO NOTHING",
                    (key, row["id"]),
                )
                return row["id"]

            if not create:
                return None
            cur.execute("INSERT INTO users (wa_id) VALUES (%s) RETURNING id", (key,))
            user_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO wa_aliases (wa_id, user_id, source) VALUES (%s, %s, 'inbound')",
                (key, user_id),
            )
            return user_id

    async def wa_id_for(self, user_id: int) -> str | None:
        """The address to reach this user on — the most recently seen alias.

        A user who switched from a phone JID to a LID must be messaged at the
        LID; the address they first arrived on may no longer deliver.
        """
        row = self._one(
            "SELECT wa_id FROM wa_aliases WHERE user_id = %s ORDER BY last_seen DESC LIMIT 1",
            (user_id,),
        ) or self._one("SELECT wa_id FROM users WHERE id = %s", (user_id,))
        return row["wa_id"] if row else None

    async def bind_account(self, user_id: int, fingerprint: str) -> int:
        """Attach a brokerage-account fingerprint, merging on collision.

        The merge is the point. A LID and a phone JID for one human share no
        digits, so nothing about the addresses relates them — but if both link
        to the same brokerage account they are provably the same person. When
        that happens we repoint the newer row's aliases at the original user
        and delete the duplicate, which is safe precisely because a just-linked
        row has no history yet.

        Returns the surviving user id, which may not be the one passed in.
        """
        with self._tx() as cur:
            cur.execute(
                "SELECT id FROM users WHERE account_fingerprint = %s AND id <> %s",
                (fingerprint, user_id),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "UPDATE users SET account_fingerprint = %s WHERE id = %s",
                    (fingerprint, user_id),
                )
                return user_id

            keep = row["id"]
            cur.execute(
                "UPDATE wa_aliases SET user_id = %s, source = 'merge' WHERE user_id = %s",
                (keep, user_id),
            )
            # The duplicate carries no book of its own; ON DELETE CASCADE clears
            # whatever little it accumulated before the link completed.
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            log.info("merged user %s into %s on account fingerprint", user_id, keep)
            return keep

    async def aliases_for(self, user_id: int) -> list[str]:
        rows = self._all(
            "SELECT wa_id FROM wa_aliases WHERE user_id = %s ORDER BY last_seen DESC",
            (user_id,),
        )
        return [r["wa_id"] for r in rows]

    # ---- preferences ------------------------------------------------

    async def get_prefs(self, user_id: int) -> dict:
        with self._tx() as cur:
            cur.execute("SELECT * FROM prefs WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if row:
                return dict(row)
            cur.execute(
                "INSERT INTO prefs (user_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING *",
                (user_id,),
            )
            return dict(cur.fetchone() or {"user_id": user_id})

    async def set_prefs(self, user_id: int, **fields) -> None:
        if not fields:
            return
        await self.get_prefs(user_id)  # ensure the row exists
        assignments = ", ".join(f"{k} = %s" for k in fields)
        self._exec(
            f"UPDATE prefs SET {assignments} WHERE user_id = %s",
            (*fields.values(), user_id),
        )

    async def briefs_enabled(self, user_id: int) -> bool:
        """False when paused or inside a snooze — checked before every brief."""
        p = await self.get_prefs(user_id)
        if p.get("briefs_paused"):
            return False
        until = p.get("muted_until")
        return not (until and until > datetime.now(until.tzinfo))

    # ---- scheduler slots --------------------------------------------

    async def job_last_run(self, job_name: str) -> date | None:
        row = self._one("SELECT last_run_on FROM job_runs WHERE job_name = %s", (job_name,))
        return row["last_run_on"] if row else None

    async def claim_slot(self, job_name: str, instant: datetime) -> bool:
        """Claim a scheduled instant. False when someone already has it.

        The unique key on (job_name, scheduled_instant) is what makes a
        recurring job at-most-once across a crash — not the application logic
        above it, which can be raced.
        """
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO job_slots (job_name, scheduled_instant) VALUES (%s, %s) "
                "ON CONFLICT (job_name, scheduled_instant) DO NOTHING RETURNING job_name",
                (job_name, instant),
            )
            claimed = cur.fetchone() is not None
            if claimed:
                cur.execute(
                    "INSERT INTO job_runs (job_name, last_run_on) VALUES (%s, %s) "
                    "ON CONFLICT (job_name) DO UPDATE SET last_run_on = EXCLUDED.last_run_on, "
                    "last_run_at = now()",
                    (job_name, instant.date()),
                )
            return claimed

    async def finish_slot(
        self, job_name: str, instant: datetime, ok: bool, error: str | None = None
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                "UPDATE job_slots SET ok = %s, error = %s "
                "WHERE job_name = %s AND scheduled_instant = %s",
                (ok, error, job_name, instant),
            )
            cur.execute(
                "UPDATE job_runs SET ok = %s, error = %s WHERE job_name = %s",
                (ok, error, job_name),
            )

    # ---- notepad ----------------------------------------------------

    async def notepad_get(self, job_name: str, key: str) -> str | None:
        row = self._one(
            "SELECT value FROM job_notepad WHERE job_name = %s AND key = %s", (job_name, key)
        )
        return row["value"] if row else None

    async def notepad_all(self, job_name: str) -> dict[str, str]:
        rows = self._all("SELECT key, value FROM job_notepad WHERE job_name = %s", (job_name,))
        return {r["key"]: r["value"] for r in rows}

    async def notepad_set(self, job_name: str, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO job_notepad (job_name, key, value) VALUES (%s, %s, %s) "
            "ON CONFLICT (job_name, key) DO UPDATE SET value = EXCLUDED.value, "
            "updated_at = now()",
            (job_name, key, value),
        )

    # ---- incidents --------------------------------------------------

    async def get_incident(self, incident_id: str) -> Incident | None:
        r = self._one("SELECT * FROM incidents WHERE id = %s", (incident_id,))
        return _incident(r) if r else None

    async def upsert_incident(
        self,
        incident_id: str,
        *,
        job_name: str,
        signature: str,
        sample: str,
        now: datetime,
        alerted: bool,
    ) -> Incident:
        state = IncidentState.ALERTED if alerted else IncidentState.DETECTED
        r = self._exec(
            "INSERT INTO incidents (id, job_name, signature, sample, state, alerted_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET occurrences = incidents.occurrences + 1, "
            "  last_seen = now(), state = %s, closed_at = NULL, "
            "  alerted_at = CASE WHEN %s THEN %s ELSE incidents.alerted_at END "
            "RETURNING *",
            (
                incident_id,
                job_name,
                signature,
                sample,
                state,
                now if alerted else None,
                state,
                alerted,
                now,
            ),
        )
        return _incident(r)

    async def close_incident(self, incident_id: str) -> None:
        self._exec(
            "UPDATE incidents SET state = 'closed', closed_at = now() WHERE id = %s",
            (incident_id,),
        )

    async def suppressions_today(self, user_id: int) -> int:
        """How many signals were held back today — the digest's honesty line."""
        return self._one(
            "SELECT count(*) AS n FROM suppressions WHERE user_id = %s "
            "AND created_at >= date_trunc('day', now())",
            (user_id,),
        )["n"]

    async def record_suppression(
        self,
        user_id: int,
        *,
        rule_id: str,
        entity: str,
        fingerprint: str,
        route: str,
        score: float,
        reason: str,
        score_trace: list | None = None,
    ) -> None:
        self._exec(
            "INSERT INTO suppressions (user_id, rule_id, entity, fingerprint, route, "
            "score, score_trace, reason) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (user_id, rule_id, entity, fingerprint, route, score, Jsonb(score_trace or []), reason),
        )

    # ---- watch suggestions ------------------------------------------

    async def suggestions_for(self, user_id: int) -> list[Suggestion]:
        rows = self._all(
            "SELECT dedup_key, source, reason, spec, state FROM watch_suggestions "
            "WHERE user_id = %s ORDER BY created_at DESC",
            (user_id,),
        )
        return [
            Suggestion(
                dedup_key=r["dedup_key"],
                source=Source(r["source"]),
                reason=r["reason"],
                spec=r["spec"] or {},
                state=State(r["state"]),
            )
            for r in rows
        ]

    async def get_suggestion(self, user_id: int, dedup_key: str) -> Suggestion | None:
        return next(
            (s for s in await self.suggestions_for(user_id) if s.dedup_key == dedup_key), None
        )

    async def add_suggestion(self, user_id: int, suggestion: Suggestion) -> None:
        self._exec(
            "INSERT INTO watch_suggestions (user_id, dedup_key, source, reason, spec) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id, dedup_key) DO NOTHING",
            (
                user_id,
                suggestion.dedup_key,
                str(suggestion.source),
                suggestion.reason,
                Jsonb(suggestion.spec),
            ),
        )

    async def decide_suggestion(self, user_id: int, dedup_key: str, state: State) -> None:
        self._exec(
            "UPDATE watch_suggestions SET state = %s, decided_at = now() "
            "WHERE user_id = %s AND dedup_key = %s",
            (str(state), user_id, dedup_key),
        )

    async def add_watch(
        self,
        user_id: int,
        *,
        trading_symbol: str,
        condition: str,
        value: float,
        exchange: str = "NSE",
        segment: str = "CASH",
    ) -> int:
        """The one watch path. A suggestion that created its own parallel
        construct would drift from the real thing within a month."""
        return self._exec(
            "INSERT INTO watches (user_id, exchange, segment, trading_symbol, "
            "condition, value) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (user_id, exchange, segment, trading_symbol, condition, value),
        )["id"]

    # ---- outbox -----------------------------------------------------

    async def enqueue_outbox(
        self,
        user_id: int,
        *,
        rule_id: str,
        fingerprint: str,
        idempotency_key: str,
        body: str,
        route: str,
        payload: dict,
        score: float | None = None,
        score_trace: list | None = None,
        state: str = "pending",
    ) -> int | None:
        """Queue an alert, superseding any unsent row for the same condition.

        Returns None when the idempotency key already exists — a retried
        evaluation must not produce a second message. The UNIQUE constraint is
        the real guarantee; this is just the polite path to it.
        """
        with self._tx() as cur:
            # Insert FIRST. Superseding before the idempotency check meant a
            # re-run marked the only pending row superseded and then inserted
            # nothing, silently dropping the message — the exact failure the
            # outbox exists to prevent.
            cur.execute(
                "INSERT INTO outbox (user_id, idempotency_key, rule_id, fingerprint, "
                "payload, body, route, score, score_trace, state) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
                (
                    user_id,
                    idempotency_key,
                    rule_id,
                    fingerprint,
                    Jsonb(payload),
                    body,
                    route,
                    score,
                    Jsonb(score_trace or []),
                    state,
                ),
            )
            row = cur.fetchone()
            if row is None:
                return None

            # Only now retire older unsent rows for the same condition: a newer
            # trigger replaces the earlier one rather than queueing behind it.
            cur.execute(
                "UPDATE outbox SET state = 'superseded' WHERE user_id = %s "
                "AND fingerprint = %s AND state = 'pending' AND id <> %s",
                (user_id, fingerprint, row["id"]),
            )
            return row["id"]

    async def claim_outbox(self, limit: int = 10) -> list[OutboxEntry]:
        """Take pending rows that are due, marking them claimed in one statement.

        SKIP LOCKED so a second drainer cannot pick up the same row; the
        attempts bump is what makes the retry ladder advance.
        """
        rows = self._all(
            "UPDATE outbox SET attempts = attempts + 1 WHERE id IN ("
            "  SELECT id FROM outbox WHERE state = 'pending' AND send_after <= now() "
            "  ORDER BY send_after LIMIT %s FOR UPDATE SKIP LOCKED"
            ") RETURNING id, user_id, rule_id, fingerprint, idempotency_key, "
            "body, route, payload, attempts",
            (limit,),
        )
        return [
            OutboxEntry(
                id=r["id"],
                user_id=r["user_id"],
                rule_id=r["rule_id"],
                fingerprint=r["fingerprint"],
                idempotency_key=r["idempotency_key"],
                body=r["body"],
                route=r["route"],
                payload=r["payload"] or {},
                # attempts was just incremented; the ladder indexes on the
                # count of attempts already made.
                attempts=r["attempts"] - 1,
            )
            for r in rows
        ]

    async def defer_outbox(self, outbox_id: int, send_after: datetime) -> None:
        self._exec("UPDATE outbox SET send_after = %s WHERE id = %s", (send_after, outbox_id))

    async def finish_outbox(
        self,
        outbox_id: int,
        state: str,
        channel_msg_id: str | None = None,
        error: str | None = None,
    ) -> None:
        self._exec(
            "UPDATE outbox SET state = %s, channel_msg_id = %s, last_error = %s, "
            "sent_at = CASE WHEN %s = 'sent' THEN now() ELSE sent_at END WHERE id = %s",
            (state, channel_msg_id, error, state, outbox_id),
        )

    async def interrupts_today(self, user_id: int) -> int:
        return self._one(
            "SELECT count(*) AS n FROM outbox WHERE user_id = %s AND route = 'interrupt' "
            "AND state = 'sent' AND sent_at >= date_trunc('day', now())",
            (user_id,),
        )["n"]

    async def all_linked_users(self) -> list[int]:
        """Users a background job may act for."""
        rows = self._all("SELECT user_id FROM credentials WHERE state <> 'dead' ORDER BY user_id")
        return [r["user_id"] for r in rows]

    async def is_linked(self, user_id: int) -> bool:
        return (
            self._one(
                "SELECT 1 FROM credentials WHERE user_id = %s AND state <> 'dead'", (user_id,)
            )
            is not None
        )

    # ---- credentials (CredentialStore protocol) --------------------

    async def get_credentials(self, user_id: int) -> Credentials:
        row = self._one(
            "SELECT user_id, totp_token_enc, totp_secret_enc, state, mint_failures "
            "FROM credentials WHERE user_id = %s",
            (user_id,),
        )
        if row is None:
            raise LookupError(f"no credentials for user {user_id}")
        return Credentials(**row)

    async def put_credentials(self, user_id: int, token_enc: bytes, secret_enc: bytes) -> None:
        self._exec(
            """INSERT INTO credentials (user_id, totp_token_enc, totp_secret_enc)
               VALUES (%s, %s, %s)
               ON CONFLICT (user_id) DO UPDATE SET
                 totp_token_enc = EXCLUDED.totp_token_enc,
                 totp_secret_enc = EXCLUDED.totp_secret_enc,
                 state = 'ok', mint_failures = 0""",
            (user_id, token_enc, secret_enc),
        )

    async def delete_user_data(self, user_id: int) -> None:
        """DELETE /me must actually remove everything (spec §24.3)."""
        self._exec("DELETE FROM users WHERE id = %s", (user_id,))

    async def set_credential_state(self, user_id: int, state: str) -> None:
        failures = "mint_failures + 1" if state in {"failing", "dead"} else "0"
        self._exec(
            f"UPDATE credentials SET state = %s, mint_failures = {failures} WHERE user_id = %s",
            (state, user_id),
        )

    async def log_token_mint(self, user_id: int, ok: bool, error: str | None = None) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO token_mints (user_id, ok, error) VALUES (%s, %s, %s)",
                (user_id, ok, error),
            )
            if ok:
                cur.execute(
                    "UPDATE credentials SET last_mint_at = now() WHERE user_id = %s", (user_id,)
                )

    async def mints_in_last_24h(self) -> int:
        return self._one(
            "SELECT count(*) AS n FROM token_mints WHERE minted_at > now() - interval '24 hours'"
        )["n"]

    # ---- link flow -------------------------------------------------

    def new_link_token(self, wa_id: str, wa_id_enc: bytes | None = None) -> str:
        """Signed, single-use, 10-minute TTL (spec §4.1).

        `wa_id_enc` is the identity the link is for, encrypted by the caller;
        the link page recovers it so the user never has to know or retype
        the address WhatsApp delivered them under. Expiry is computed by
        Postgres so the clock is the same one that later checks it.
        """
        cfg = settings()
        nonce = secrets.token_hex(8)
        payload = f"{wa_id}{nonce}{int(time.time())}"
        token = hmac.new(cfg.link_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[
            :32
        ]
        self._exec(
            "INSERT INTO link_requests (token, wa_id_hash, wa_id_enc, expires_at) "
            "VALUES (%s, %s, %s, now() + make_interval(secs => %s))",
            (token, hashlib.sha256(wa_id.encode()).hexdigest(), wa_id_enc, cfg.link_ttl_seconds),
        )
        return token

    def link_request_wa_id(self, token: str) -> bytes | None:
        """The encrypted identity behind a live token; None if expired or used."""
        row = self._one(
            "SELECT wa_id_enc FROM link_requests "
            "WHERE token = %s AND used = false AND expires_at > now()",
            (token,),
        )
        return row["wa_id_enc"] if row else None

    def consume_link_token(self, token: str) -> bool:
        """Burn a token. Returns False if it was expired or already used."""
        hit = self._exec(
            "UPDATE link_requests SET used = true "
            "WHERE token = %s AND used = false AND expires_at > now() RETURNING token",
            (token,),
        )
        return hit is not None

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
        return self._exec(
            """INSERT INTO messages (user_id, direction, channel_msg_id, text, intent, latency_ms)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (user_id, direction, channel_msg_id, text, intent, latency_ms),
        )["id"]

    async def log_trace(
        self, message_id: int, path: str, intent: str, tool_calls=None, error: str | None = None
    ) -> None:
        self._exec(
            """INSERT INTO traces (message_id, path, intent, tool_calls, error)
               VALUES (%s, %s, %s, %s, %s)""",
            (message_id, path, intent, json.dumps(tool_calls or []), error),
        )
