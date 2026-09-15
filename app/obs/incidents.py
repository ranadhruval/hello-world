"""Our own failures, grouped so they page once (B8).

Three detectors are planned for "alerts stopped, chat works" — a heartbeat the
worker checks, pending outbox rows aging, and a zero-alert day. Without
grouping, each of them pages on every tick: a broken feed at 09:15 produces
several hundred identical messages by 15:30, and the operator learns to ignore
them, which is the same failure mode the interruption gate exists to prevent.
We should not solve it for the user and not for ourselves.

An incident is keyed on `(job, error signature)`. The signature is the error
text with the parts that vary between occurrences stripped out, so the same
fault always resolves to the same incident id and an acknowledged one stays
quiet until the error genuinely changes.

Lifecycle: detected -> alerted -> closed. `alerted` means a page actually
reached someone; a closed incident stays closed until a new signature mints a
new id.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.config import IST, to_ist

# Parts of an error that differ between occurrences of the same fault. Stripped
# before hashing, or every retry looks like a brand-new incident and the dedup
# does nothing.
_VARIABLE = (
    (re.compile(r"0x[0-9a-fA-F]+"), "<addr>"),  # object addresses
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][\d:.]+"), "<ts>"),  # timestamps
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
            r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<uuid>",
    ),
    (re.compile(r"\b\d+\b"), "<n>"),  # ids, ports, counts
    (re.compile(r"\s+"), " "),
)

# How long an alerted incident stays quiet before it is worth mentioning again.
# Long, because the point is one page per fault, not one page per hour.
REPAGE_AFTER = timedelta(hours=6)


class IncidentState(StrEnum):
    DETECTED = "detected"
    ALERTED = "alerted"
    CLOSED = "closed"


def signature(error: str) -> str:
    """Normalise an error to what is stable about it across occurrences."""
    out = (error or "").strip()
    for pattern, replacement in _VARIABLE:
        out = pattern.sub(replacement, out)
    return out.strip()[:500]


def incident_id(job_name: str, error: str) -> str:
    """Same job, same fault -> same id, across restarts and across processes."""
    digest = hashlib.sha256(f"{job_name}|{signature(error)}".encode()).hexdigest()
    return f"inc_{digest[:16]}"


@dataclass(frozen=True)
class Incident:
    id: str
    job_name: str
    signature: str
    state: IncidentState
    occurrences: int
    last_seen: datetime
    alerted_at: datetime | None = None


def should_page(incident: Incident | None, now: datetime) -> bool:
    """Whether this occurrence is worth telling a human about.

    A brand-new fault pages. A fault we already paged about does not, until it
    has been quiet long enough that a repeat is news again. A closed incident
    that recurs pages, because closing it was a claim that it was fixed.
    """
    if incident is None:
        return True
    if incident.state is IncidentState.DETECTED:
        return True
    if incident.state is IncidentState.CLOSED:
        return True
    alerted_at = to_ist(incident.alerted_at)
    if alerted_at is None:
        return True
    return to_ist(now) - alerted_at >= REPAGE_AFTER


class Incidents:
    """Typed accessor over the incidents table."""

    def __init__(self, store) -> None:
        self._store = store

    async def record(
        self, job_name: str, error: str, *, now: datetime | None = None
    ) -> tuple[Incident, bool]:
        """Log an occurrence. Returns the incident and whether to page."""
        now = to_ist(now) or datetime.now(IST)
        iid = incident_id(job_name, error)
        existing = await self._store.get_incident(iid)
        page = should_page(existing, now)
        incident = await self._store.upsert_incident(
            iid,
            job_name=job_name,
            signature=signature(error),
            sample=(error or "")[:1000],
            now=now,
            alerted=page,
        )
        return incident, page

    async def close(self, job_name: str, error: str) -> None:
        """Mark a fault resolved. It stays closed until the signature changes."""
        await self._store.close_incident(incident_id(job_name, error))
