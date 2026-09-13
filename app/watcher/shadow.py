"""Shadow mode: run the whole pipeline, send nothing (plan §7).

An alerting product gets exactly one chance. The spec's kill criterion is that
the moment alerts become background noise the product is dead and no amount of
additional rules revives it — so the first week runs complete but silent, and
the gate is tuned against real sessions before anything reaches a phone.

Output is a local file by choice: a week of unfiltered "here is what I would
have said" is precisely the noise we are trying to avoid putting on WhatsApp.

The log is JSONL and append-only so a day can be re-read by `scripts/replay.py`
after the gate changes, and the two runs compared.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from app.compose.alerts import Alert
from app.config import IST
from app.watcher.gate import Decision, Route
from app.watcher.rules import Trigger

SHADOW_DIR = Path("data/shadow")

# Spec §19: the volume the gate is aiming at. Outside this band the gate is
# miscalibrated, and the report says so rather than leaving it to be noticed.
TARGET_PER_DAY = (2, 5)

_ORDER = [Route.INTERRUPT, Route.BATCH, Route.DIGEST, Route.SILENT]
_HEADING = {
    Route.INTERRUPT: "WOULD HAVE INTERRUPTED",
    Route.BATCH: "WOULD HAVE BATCHED",
    Route.DIGEST: "HELD FOR THE EVENING DIGEST",
    Route.SILENT: "SILENT (ledger only)",
}


@dataclass(frozen=True)
class ShadowEntry:
    at: datetime
    rule_id: str
    entity: str
    route: Route
    score: float
    reason: str
    body: str
    trace: tuple[tuple[str, float], ...] = ()

    def to_json(self) -> dict:
        return {
            "at": self.at.isoformat(),
            "rule_id": self.rule_id,
            "entity": self.entity,
            "route": str(self.route),
            "score": round(self.score, 4),
            "reason": self.reason,
            "body": self.body,
            "trace": [[n, round(v, 4)] for n, v in self.trace],
        }

    @classmethod
    def from_json(cls, d: dict) -> ShadowEntry:
        return cls(
            at=datetime.fromisoformat(d["at"]),
            rule_id=d["rule_id"],
            entity=d["entity"],
            route=Route(d["route"]),
            score=float(d["score"]),
            reason=d.get("reason", ""),
            body=d.get("body", ""),
            trace=tuple((n, v) for n, v in d.get("trace", [])),
        )


class ShadowLog:
    def __init__(self, directory: Path | str = SHADOW_DIR) -> None:
        self.dir = Path(directory)

    def path_for(self, day: date) -> Path:
        return self.dir / f"{day:%Y-%m-%d}.jsonl"

    def record(
        self,
        trigger: Trigger,
        decision: Decision,
        alert: Alert | None = None,
        at: datetime | None = None,
    ) -> ShadowEntry:
        # When it happened, not when the process got round to it. A replayed or
        # backfilled day must land in the right file and sort correctly.
        now = at or datetime.now(IST)
        entry = ShadowEntry(
            at=now,
            rule_id=trigger.rule_id,
            entity=trigger.entity,
            route=decision.route,
            score=decision.score,
            reason=decision.reason,
            body=alert.body if alert else "",
            trace=decision.trace,
        )
        path = self.path_for(now.date())
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(entry.to_json(), ensure_ascii=False) + "\n")
        return entry

    def entries(self, day: date) -> list[ShadowEntry]:
        path = self.path_for(day)
        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines():
            if line.strip():
                out.append(ShadowEntry.from_json(json.loads(line)))
        return out

    def report(self, day: date) -> str:
        return render_report(day, self.entries(day))

    def write_report(self, day: date) -> Path:
        path = self.dir / f"{day:%Y-%m-%d}-report.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.report(day))
        return path


def render_report(day: date, entries: list[ShadowEntry]) -> str:
    """The evening read. Optimised for deciding whether to loosen or tighten."""
    by_route: dict[Route, list[ShadowEntry]] = {r: [] for r in _ORDER}
    for e in entries:
        by_route.setdefault(e.route, []).append(e)

    would_reach = len(by_route[Route.INTERRUPT]) + len(by_route[Route.BATCH])
    lines = [
        f"Shadow report — {day:%a %d %b %Y}",
        "=" * 52,
        f"interrupt {len(by_route[Route.INTERRUPT])} · batch {len(by_route[Route.BATCH])} · "
        f"digest {len(by_route[Route.DIGEST])} · silent {len(by_route[Route.SILENT])}",
        "",
    ]

    for route in _ORDER:
        rows = by_route[route]
        if not rows:
            continue
        lines.append(f"{_HEADING[route]} ({len(rows)})")
        lines.append("-" * 52)
        for e in sorted(rows, key=lambda x: x.at):
            tail = f"  [{e.reason}]" if e.reason else ""
            lines.append(f"  {e.at:%H:%M}  {e.rule_id}  {e.entity}  {e.score:.2f}{tail}")
            if route in (Route.INTERRUPT, Route.BATCH) and e.body:
                lines.extend("        " + b for b in e.body.splitlines())
                lines.append("        · " + " → ".join(f"{n} {v:.2f}" for n, v in e.trace))
        lines.append("")

    lo, hi = TARGET_PER_DAY
    verdict = (
        "within target"
        if lo <= would_reach <= hi
        else ("too quiet — loosen" if would_reach < lo else "too loud — tighten")
    )
    lines += [
        "=" * 52,
        f"Would have reached you: {would_reach}   (target {lo}–{hi}/day — {verdict})",
    ]
    if by_route[Route.SILENT]:
        lines.append(
            "Silence was a decision, not a failure — every suppressed row is above, "
            "with the reason."
        )
    return "\n".join(lines)
