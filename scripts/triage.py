#!/usr/bin/env python3
"""Where did the message stop? Walks the live path hop by hop.

    python scripts/triage.py          # or: make triage

`doctor` checks prerequisites before you start anything. This checks the
running system: a text goes phone -> adapter -> redis -> worker -> adapter ->
phone, and a silent bot means one of those hops is down. Read-only, and it
names the fix for the first hop that is broken.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def say(mark: str, name: str, detail: str = "") -> None:
    print(f"  {mark}  {name}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


ok = lambda n, d="": say(f"{GREEN}ok  {RESET}", n, d)  # noqa: E731
warn = lambda n, d="": say(f"{YELLOW}warn{RESET}", n, d)  # noqa: E731
bad = lambda n, d="": say(f"{RED}FAIL{RESET}", n, d)  # noqa: E731


def get_json(url: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
            return json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def main() -> int:
    from app.config import settings

    cfg = settings()
    problems: list[str] = []

    # ---- hop 1: the adapter owns the WhatsApp session ----------------
    print("\nadapter (terminal 3)")
    health = get_json(f"{cfg.baileys_url}/health")
    if health is None:
        bad("process", f"nothing answering at {cfg.baileys_url}")
        problems.append("Adapter is down. In adapter/: npm start   (exactly ONE terminal)")
    elif health.get("connected"):
        # Our own JID says which addressing scheme this account is on, which
        # decides how a reply has to be addressed.
        ok("connected to WhatsApp", f"as {health.get('jid') or 'unknown'}")
    else:
        bad("connected to WhatsApp", "listening, but the WhatsApp socket is down")
        problems.append(
            "Adapter is not paired. In adapter/: rm -rf session && npm start, then scan the QR"
        )

    # ---- hop 2: inbound lands on the redis stream --------------------
    print("\nredis (the inbound queue)")
    try:
        import redis as redis_sync

        r = redis_sync.Redis.from_url(cfg.redis_url, socket_timeout=2)
        depth = r.xlen("inbound")
        ok("reachable", cfg.redis_url)
        ok("inbound stream", f"{depth} message(s) ever queued")
        # A LID chat can only be delivered to via the phone address behind it,
        # which the adapter learns from inbound messages. Without it a send is
        # accepted and silently never arrives.
        lids = {k.decode(): v.decode() for k, v in r.hgetall("lid_pn").items()}
        if lids:
            for lid, pn in list(lids.items())[:4]:
                ok("lid -> phone", f"{lid} -> {pn}")
        else:
            warn("lid -> phone map", "empty — replies to a @lid chat may not arrive")
    except Exception as exc:  # noqa: BLE001
        bad("reachable", f"{type(exc).__name__}: {exc}")
        problems.append("Redis is down: docker compose up -d redis")
        r, depth = None, 0

    # ---- hop 3: the worker consumes it -------------------------------
    print("\nworker (terminal 2)")
    if r is not None and depth:
        groups = {g["name"].decode(): g for g in r.xinfo_groups("inbound")}
        g = groups.get("workers")
        last_id = r.xrevrange("inbound", count=1)[0][0].decode()
        if g is None:
            bad("consumer group", "'workers' does not exist — the worker has never run")
            problems.append("Worker never started: make worker")
        elif g["last-delivered-id"].decode() == last_id:
            ok("consumer group", f"caught up at {last_id}")
        else:
            bad(
                "consumer group",
                f"stuck at {g['last-delivered-id'].decode()}, stream is at {last_id}",
            )
            problems.append("Worker is not reading. Restart terminal 2: make worker")
        if g is not None and g["pending"]:
            warn("unacked", f"{g['pending']} message(s) claimed but never finished")
    elif r is not None:
        warn("consumer group", "stream is empty — text the bot, then re-run this")

    # ---- hop 4: is anyone actually linked? ---------------------------
    print("\nthe account")
    try:
        import psycopg
        from psycopg.rows import dict_row

        dsn = cfg.database_url.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(dsn, connect_timeout=3, row_factory=dict_row) as conn:
            rows = conn.execute(
                "SELECT u.id, u.wa_id, (c.user_id IS NOT NULL) AS linked "
                "FROM users u LEFT JOIN credentials c ON c.user_id = u.id ORDER BY u.id"
            ).fetchall()
            aliases = conn.execute(
                "SELECT user_id, wa_id FROM wa_aliases ORDER BY user_id"
            ).fetchall()
            recent = conn.execute(
                "SELECT direction, text, intent, created_at FROM messages "
                "ORDER BY created_at DESC LIMIT 6"
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        bad("postgres", f"{type(exc).__name__}: {exc}")
        problems.append("Postgres is down: docker compose up -d postgres && make migrate")
        rows, aliases, recent = [], [], []

    linked = [r_ for r_ in rows if r_["linked"]]
    if not rows:
        warn("users", "nobody has ever texted the bot")
    elif not linked:
        bad("credentials", f"{len(rows)} user(s), none linked")
        problems.append("Nobody is linked. Text 'link' and complete the page.")
    else:
        for r_ in linked:
            alias = [a["wa_id"] for a in aliases if a["user_id"] == r_["id"]]
            ok(
                f"user {r_['id']} linked",
                f"as {r_['wa_id']}" + (f" (+{len(alias)} alias)" if alias else ""),
            )
        unlinked = [r_["wa_id"] for r_ in rows if not r_["linked"]]
        if unlinked:
            # The classic LID split: the page stored credentials under one
            # identity while messages arrive under another, so every text gets
            # the onboarding line instead of an answer.
            warn("unlinked identities", ", ".join(unlinked[:4]))
            problems.append(
                "Some identities have no credentials. If the bot keeps sending you the "
                "link, text 'unlink', then 'link', and complete the page again."
            )

    print("\nlast messages")
    if not recent:
        warn("none", "the worker has never answered anything")
    for m in reversed(recent):
        arrow = "->" if m["direction"] == "in" else "<-"
        body = (m["text"] or "").splitlines()[0][:54]
        say(f"{DIM}    {RESET}", f"{arrow} {body}", m["intent"] or "")

    print()
    if problems:
        print(f"{RED}fix this first{RESET}\n")
        print(f"  {problems[0]}\n")
        for p in problems[1:]:
            print(f"  {DIM}then: {p}{RESET}")
        print()
        return 1
    print(f"{GREEN}every hop is up{RESET} — if a text still goes unanswered, the worker")
    print("terminal will have the traceback. Paste it.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
