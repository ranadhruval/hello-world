#!/usr/bin/env python3
"""Replay recorded signals through the current gate.

Alerting systems cannot be tuned live: a threshold change takes a week of
market to evaluate, and by then you have changed three other things. Because
the shadow log is append-only and carries the full score trace, a recorded day
can be pushed through a modified gate in seconds and the two runs compared.

This is to Phase 2 what eval/run.py is to Phase 1, and it is built first rather
than last for the same reason.

Usage:
    python scripts/replay.py                    # today, current gate
    python scripts/replay.py --day 2026-09-15
    python scripts/replay.py --day 2026-09-15 --diff   # vs what was recorded
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.watcher.gate import Route  # noqa: E402
from app.watcher.shadow import ShadowLog, render_report  # noqa: E402

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", default=date.today().isoformat())
    ap.add_argument("--dir", default="data/shadow")
    ap.add_argument("--diff", action="store_true", help="compare routes against the recording")
    args = ap.parse_args()

    day = date.fromisoformat(args.day)
    log = ShadowLog(args.dir)
    entries = log.entries(day)

    if not entries:
        print(f"No shadow log for {day}. Nothing to replay.")
        print(f"{DIM}Expected {log.path_for(day)}{RESET}")
        return 1

    print(render_report(day, entries))

    if args.diff:
        # Re-scoring needs the book and gate state as they were, which the log
        # does not carry — it records decisions, not inputs. Until the watcher
        # persists signals (schema_phase2.sql `signals`), a diff can only
        # compare recordings to each other, so say that plainly rather than
        # printing a comparison that quietly means nothing.
        print()
        print(f"{RED}--diff needs recorded signal inputs, not just decisions.{RESET}")
        print(f"{DIM}Replay from the `signals` table once the watcher is persisting.{RESET}")
        return 2

    counts = Counter(e.route for e in entries)
    reach = counts[Route.INTERRUPT] + counts[Route.BATCH]
    lo, hi = 2, 5
    ok = lo <= reach <= hi
    print()
    print(f"{GREEN if ok else RED}{reach} would have reached the user{RESET}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
