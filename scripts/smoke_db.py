#!/usr/bin/env python3
"""Exercise the Store against a real Postgres.

The unit suite uses hand-written fakes, which is right for policy but cannot
catch anything about the SQL itself. This script found a live bug the first
time it ran — Postgres returns `timestamptz` while the incident code defaulted
to a naive `datetime.now()`, and subtracting them raises — so it earns its keep.

Run it against a throwaway database, never a real one: it writes rows.

    make smoke-db                       # uses DATABASE_URL
    python scripts/smoke_db.py --dsn postgresql://groww@/growwdesk?host=/tmp&port=55432

A hand-rolled throwaway cluster must be UTF-8 (`initdb -E UTF8`): under
SQL_ASCII psycopg returns text columns as bytes and the enum checks fail
for a reason that has nothing to do with the code. Docker's image is UTF-8.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import IST, settings  # noqa: E402
from app.obs.incidents import Incidents  # noqa: E402
from app.outbox import OutboxState, idempotency_key  # noqa: E402
from app.store.db import Store  # noqa: E402
from app.watcher.suggestions import Suggestions, from_repeated_questions  # noqa: E402

GREEN, RED, RESET = "\033[32m", "\033[31m", "\033[0m"
_failures = 0


def ok(label: str, cond: bool) -> None:
    global _failures
    if not cond:
        _failures += 1
    mark = f"{GREEN}PASS{RESET}" if cond else f"{RED}FAIL{RESET}"
    print(f"  {mark}  {label}")


async def run(dsn: str) -> int:
    st = Store(dsn)
    stamp = datetime.datetime.now(IST).strftime("%H%M%S%f")
    phone = f"9199{stamp[:8]}"
    lid_digits = f"77{stamp[:9]}"

    print("\nidentity")
    u1 = await st.user_id_for(f"{phone}@s.whatsapp.net")
    ok(
        "a device suffix resolves to the same user",
        u1 == await st.user_id_for(f"{phone}:47@s.whatsapp.net"),
    )
    lid = await st.user_id_for(f"{lid_digits}@lid")
    ok("a LID is a separate user until proven otherwise", lid != u1)

    await st.bind_account(u1, f"fp-{stamp}")
    ok(
        "linking the same brokerage account merges the rows",
        await st.bind_account(lid, f"fp-{stamp}") == u1,
    )
    ok(
        "both addresses now point at one user",
        set(await st.aliases_for(u1)) == {phone, f"lid:{lid_digits}"},
    )
    ok(
        "a proactive send addresses the freshest alias",
        await st.wa_id_for(u1) == f"lid:{lid_digits}",
    )
    ok(
        "the merged address no longer mints a second user",
        await st.user_id_for(f"{lid_digits}@lid") == u1,
    )

    print("\noutbox")
    key = idempotency_key(u1, "margin.band_up", "tight", datetime.date(2026, 9, 15))
    first = await st.enqueue_outbox(
        u1,
        rule_id="margin.band_up",
        fingerprint=f"fp{stamp}",
        idempotency_key=key,
        body="margin tight",
        route="interrupt",
        payload={"band": "tight"},
        score=0.77,
        score_trace=[["severity", 0.79]],
    )
    ok("an alert queues", first is not None)
    ok(
        "the same idempotency key cannot double-send",
        await st.enqueue_outbox(
            u1,
            rule_id="margin.band_up",
            fingerprint=f"fp{stamp}",
            idempotency_key=key,
            body="dup",
            route="interrupt",
            payload={},
        )
        is None,
    )
    newer = await st.enqueue_outbox(
        u1,
        rule_id="margin.band_up",
        fingerprint=f"fp{stamp}",
        idempotency_key=key + ":v2",
        body="margin stressed",
        route="interrupt",
        payload={},
    )
    claimed = await st.claim_outbox()
    ok("a superseded row is never delivered", [c.id for c in claimed] == [newer])
    ok("attempts counts attempts already made", claimed[0].attempts == 0)

    # Regression: supersession used to run before the idempotency check, so a
    # re-run retired the only pending row and then inserted nothing — the
    # message vanished with no error anywhere. Silent, and therefore the worst
    # kind. A duplicate enqueue must leave the original deliverable.
    key2 = idempotency_key(u1, "brief.pre_market", "daily", datetime.date(2026, 9, 16))
    fp2 = f"brief|{stamp}"
    live = await st.enqueue_outbox(
        u1,
        rule_id="brief.pre_market",
        fingerprint=fp2,
        idempotency_key=key2,
        body="good morning",
        route="interrupt",
        payload={},
    )
    dup = await st.enqueue_outbox(
        u1,
        rule_id="brief.pre_market",
        fingerprint=fp2,
        idempotency_key=key2,
        body="good morning",
        route="interrupt",
        payload={},
    )
    ok("a duplicate enqueue is refused", dup is None)
    ok("and does not cancel the original", live in [c.id for c in await st.claim_outbox(limit=50)])
    # Leave nothing pending: a row left behind here is claimed by the NEXT run
    # of this script and fails its supersession check for no real reason.
    await st.finish_outbox(live, OutboxState.SENT, channel_msg_id="wamid.smoke")
    await st.finish_outbox(newer, OutboxState.SENT, channel_msg_id="wamid.1")
    ok("a sent interrupt spends the day's budget", await st.interrupts_today(u1) >= 1)

    print("\nscheduler")
    slot = datetime.datetime(2026, 9, 15, 9, 5, tzinfo=IST)
    job = f"brief-{stamp}"
    ok("a slot claims once", await st.claim_slot(job, slot) is True)
    ok("and cannot be claimed twice", await st.claim_slot(job, slot) is False)
    await st.finish_slot(job, slot, ok=True)
    ok("the last-run date is durable", await st.job_last_run(job) == slot.date())

    print("\nlink tokens")
    tok = st.new_link_token("lid:4242", b"enc")
    ok("a live token carries its identity", st.link_request(tok)["wa_id_enc"] == b"enc")
    ok("it burns once", st.consume_link_token(tok))
    ok("and not twice", not st.consume_link_token(tok))
    ok("a burnt token is gone", st.link_request(tok) is None)
    stale = st.new_link_token("lid:4242")
    ok(
        "a token from an older build is live but identity-less",
        st.link_request(stale) is not None and st.link_request(stale)["wa_id_enc"] is None,
    )

    print("\nnotepad")
    await st.notepad_set(job, "since", "cursor-1")
    await st.notepad_set(job, "since", "cursor-2")
    ok("a cursor upserts", await st.notepad_get(job, "since") == "cursor-2")

    print("\nincidents")
    inc, now = Incidents(st), datetime.datetime.now(IST)
    i1, paged = await inc.record(job, "redis timeout after 3 tries", now=now)
    i2, again = await inc.record(job, "redis timeout after 91 tries", now=now)
    ok("the same fault with different ids is one incident", i1.id == i2.id)
    ok("it pages once", paged and not again)
    ok("occurrences accumulate", i2.occurrences == 2)
    await inc.close(job, "redis timeout after 3 tries")
    _, repaged = await inc.record(job, "redis timeout after 5 tries", now=now)
    ok("a closed fault that recurs pages again", repaged)

    print("\nsuggestions")
    sug = Suggestions(st)
    cand = from_repeated_questions("TITAN", 4)
    ok("a proposal is offered", await sug.offer(u1, cand) is True)
    ok("and never offered twice", await sug.offer(u1, cand) is False)
    ok("accepting creates a real watch", await sug.accept(u1, cand.dedup_key) is True)
    ok("and clears the pending list", await sug.pending(u1) == [])

    print()
    if _failures:
        print(f"{RED}{_failures} failed{RESET}")
    else:
        print(f"{GREEN}all checks passed{RESET}")
    return 1 if _failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default="")
    args = ap.parse_args()
    dsn = (args.dsn or settings().database_url).replace("postgresql+psycopg://", "postgresql://")
    print(f"smoke: {dsn.rsplit('@', 1)[-1]}")
    return asyncio.run(run(dsn))


if __name__ == "__main__":
    raise SystemExit(main())
