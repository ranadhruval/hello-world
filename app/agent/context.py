"""What R2D2 is told about the person asking.

Symbols and the shape of the book, never a credential and never a figure R2D2
could echo back as its own. The distinction matters: anything sent as context
is something the model can repeat, and a number it repeats looks identical to a
number it retrieved. Holdings values stay out; the symbol list is what lets it
resolve "my gold position" without guessing.
"""

from __future__ import annotations

from typing import Any

MAX_SYMBOLS = 40


def build_context(
    *,
    holdings: list[str] | None = None,
    positions: list[str] | None = None,
    groww_user_id: str | None = None,
    recent: list[dict] | None = None,
) -> dict[str, Any]:
    ctx: dict[str, Any] = {}
    if groww_user_id:
        # Only present when /response needs it. GR-2 does not store this: it is
        # read at request time from the already-authenticated Groww client and
        # held in memory. See docs/INTEGRATE_R2D2.md.
        ctx["user_id"] = groww_user_id
    if holdings:
        ctx["holdings"] = sorted({s.upper() for s in holdings})[:MAX_SYMBOLS]
    if positions:
        ctx["positions"] = sorted({s.upper() for s in positions})[:MAX_SYMBOLS]
    if recent:
        ctx["history"] = [
            {
                "role": "user" if m.get("direction") == "in" else "assistant",
                "content": (m.get("text") or "")[:400],
            }
            for m in recent
            if m.get("text")
        ]
    return ctx
