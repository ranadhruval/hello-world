"""NumericGuard — invariant I1, enforced rather than asserted.

I1 says every number in an outbound message is rendered from a typed tool
result, and the model never emits a digit that was not in one. Written down,
that is an instruction a model can quietly disobey. Here it is a property that
is checked: extract every numeric token from the finished message and require
that each traces back to a slot the tool layer produced.

A message that fails is not sent. It falls back to the deterministic template
and logs loudly (invariant I5), because a finance assistant that is
occasionally wrong about a number is worse than no assistant at all.

This runs on *every* composed message, template or model-written. Templates are
not exempt: a template with a hand-typed constant in it is the same defect with
a longer fuse.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Any run of digits, optionally grouped with commas and optionally fractional.
# Indian grouping means 18,42,600 is one token, not three.
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Tokens that are structurally part of the language rather than claims about
# the user's money. Kept deliberately short — every entry is a hole in the
# check, so nothing goes here that could ever be a quantity or a price.
STRUCTURAL = frozenset({"1", "2", "3", "4", "5"})  # reply-option numbering


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    untraceable: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.ok


def _candidates(value: object) -> set[float]:
    """Every float a slot could legitimately render as."""
    out: set[float] = set()
    if isinstance(value, bool) or value is None:
        return out
    if isinstance(value, (int, float)):
        v = float(value)
        out.add(v)
        out.add(abs(v))
        # A slot may render as a percentage of itself (0.347 -> 34.7).
        out.add(abs(v) * 100)
        return out
    if isinstance(value, str):
        for m in _NUMBER.finditer(value):
            try:
                out.add(abs(float(m.group().replace(",", ""))))
            except ValueError:
                continue
    return out


def allowed(slots: dict) -> set[float]:
    """Flatten a slot map into every number it may legitimately produce."""
    out: set[float] = set()
    for v in slots.values():
        if isinstance(v, dict):
            out |= allowed(v)
        elif isinstance(v, (list, tuple)):
            for item in v:
                out |= allowed({"_": item})
        else:
            out |= _candidates(v)
    return out


def _traces(token: str, pool: set[float]) -> bool:
    """Does this rendered token match some slot value?

    Compared at the precision shown. '3.2' traces to -3.1978 because that is
    what one decimal place of it looks like; '3.3' does not.
    """
    raw = token.replace(",", "")
    try:
        shown = float(raw)
    except ValueError:
        return False
    decimals = len(raw.partition(".")[2])
    return any(round(abs(c), decimals) == abs(shown) for c in pool)


def check(text: str, slots: dict) -> GuardResult:
    pool = allowed(slots)
    bad = [
        tok
        for tok in (m.group() for m in _NUMBER.finditer(text))
        if tok not in STRUCTURAL and not _traces(tok, pool)
    ]
    return GuardResult(not bad, tuple(bad))


def guard(text: str, slots: dict, *, fallback: str | None = None, where: str = "") -> str:
    """Return the message, or the fallback when a number cannot be traced.

    Never returns text containing an untraceable figure. If there is no
    fallback the caller gets nothing — silence beats a confident wrong number.
    """
    result = check(text, slots)
    if result.ok:
        return text
    log.error(
        "I1 violation in %s: %s not traceable to any tool result; message withheld",
        where or "composed message",
        ", ".join(result.untraceable),
    )
    return fallback or ""
