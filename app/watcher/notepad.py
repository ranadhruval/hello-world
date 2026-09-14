"""Per-job durable key/value carried across wake-ups (B2).

Cursors and watermarks: the `since` token for the signal engine, the last
instrument-master refresh, the high-water mark of a backfill. Small, boring,
and the difference between a restart resuming and a restart re-reading a week.

The caps are the transferable part, not the storage. Hermes caps its notepad
because the contents are injected into a prompt each run; ours are not, but the
same discipline applies for a different reason — a value that grows every tick
is a job that works for a month and then stops, and the failure surfaces far
from its cause. Oversized writes raise and leave the store untouched.
"""

from __future__ import annotations

MAX_KEY_CHARS = 128
MAX_VALUE_BYTES = 16 * 1024
MAX_JOB_TOTAL_BYTES = 64 * 1024


class NotepadFull(ValueError):
    """A write would take the job over its byte budget."""


def check_write(key: str, value: str, existing: dict[str, str]) -> None:
    """Validate a write against the caps. Raises, or returns None.

    Pure so the budget arithmetic is testable without a database — and the
    arithmetic is the whole point, since an off-by-one here shows up as a job
    that silently stops updating its cursor.
    """
    if not key or len(key) > MAX_KEY_CHARS:
        raise ValueError(f"key must be 1..{MAX_KEY_CHARS} chars, got {len(key)}")
    size = len(value.encode())
    if size > MAX_VALUE_BYTES:
        raise NotepadFull(f"value is {size} bytes, cap is {MAX_VALUE_BYTES}")
    total = (
        sum(len(k.encode()) + len(v.encode()) for k, v in existing.items() if k != key)
        + len(key.encode())
        + size
    )
    if total > MAX_JOB_TOTAL_BYTES:
        raise NotepadFull(f"job total would be {total} bytes, cap is {MAX_JOB_TOTAL_BYTES}")


class Notepad:
    """Thin typed accessor over the job_notepad table."""

    def __init__(self, store, job_name: str) -> None:
        self._store = store
        self._job = job_name

    async def get(self, key: str, default: str | None = None) -> str | None:
        return await self._store.notepad_get(self._job, key) or default

    async def set(self, key: str, value: str) -> None:
        existing = await self._store.notepad_all(self._job)
        check_write(key, value, existing)
        await self._store.notepad_set(self._job, key, value)

    async def all(self) -> dict[str, str]:
        return await self._store.notepad_all(self._job)
