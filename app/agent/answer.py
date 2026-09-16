"""Decide whether R2D2's answer is fit to send.

The transport in `r2d2.py` knows how to get prose. This knows what GR-2 is
willing to put in front of a user, and it is the reason an external answering
engine can be plugged in at all without giving up the guarantees the rest of
the system makes.

Three checks, in order, each of which withholds rather than edits:

  I1  every figure traces to a tool result (app/compose/guard.py). R2D2's tool
      results are what its figures trace to, which is why the transport carries
      them and why an answer arriving without them can only be sent if it
      contains no numbers at all.
  Compliance: no advice-shaped phrasing (app/compose/voice.py). R2D2 writes for
      a different surface and may well say "you should book profits"; that is a
      regulatory boundary, not a matter of taste.
  Shape: markdown flattened, trimmed to a phone (app/agent/shape.py).

Withheld means `None`, not an exception. The caller already has a deterministic
answer for this message — silence from here just means it uses it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.agent.r2d2 import Answer, R2D2Client, R2D2Unavailable
from app.agent.shape import shape
from app.channel.base import OutboundMessage
from app.compose.guard import check
from app.compose.voice import advice_violations
from app.infra import CircuitBreaker

log = logging.getLogger(__name__)


class Answerer:
    def __init__(
        self,
        client: R2D2Client,
        *,
        strict_numbers: bool = True,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._client = client
        self._strict = strict_numbers
        # A dead R2D2 would otherwise add its full timeout to every open
        # question. Five failures in a minute and we stop asking for two.
        self._breaker = breaker or CircuitBreaker()

    async def reply(
        self,
        question: str,
        *,
        wa_id: str,
        context: dict | None = None,
        local_facts: dict[str, Any] | None = None,
        on_heartbeat: Callable[[], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """An answer fit to send, or None to fall back to the typed reply."""
        if self._breaker.is_open:
            log.warning("R2D2 circuit open; not asking")
            return None

        started = time.monotonic()
        try:
            answer = await self._client.ask(question, context=context, on_heartbeat=on_heartbeat)
        except R2D2Unavailable as exc:
            self._breaker.record_failure()
            log.warning("R2D2 unavailable: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 - never let the desk lose its turn
            self._breaker.record_failure()
            log.exception("R2D2 call failed: %s", type(exc).__name__)
            return None

        self._breaker.record_success()
        ms = int((time.monotonic() - started) * 1000)
        body = self._vet(answer, local_facts or {})
        log.info(
            "r2d2 %dms tools=%d grounded=%s sent=%s",
            ms,
            len(answer.tool_calls),
            answer.grounded,
            bool(body),
        )
        if not body:
            return None
        return OutboundMessage(wa_id=wa_id, kind="text", text=body)

    # ---- the checks -------------------------------------------------

    def _vet(self, answer: Answer, local_facts: dict[str, Any]) -> str:
        text = shape(answer.text)
        if not text:
            return ""

        violations = advice_violations(text)
        if violations:
            log.error("R2D2 answer withheld, advice-shaped: %s", ", ".join(violations))
            return ""

        # The pool is R2D2's own tool results plus whatever GR-2 fetched for
        # this turn. Both are tool results; they just came from different tool
        # layers, and I1 is a claim about provenance, not about who called it.
        pool = {**local_facts, **answer.facts}
        result = check(text, pool)
        if result.ok:
            return text

        if self._strict:
            log.error(
                "R2D2 answer withheld, I1: %s not traceable to any tool result "
                "(tool results present: %s)",
                ", ".join(result.untraceable),
                bool(answer.facts),
            )
            return ""

        # Off by default, and the log says exactly what got through. Intended
        # for a single operator confirming the pipe works before the numbers
        # are trustworthy, never for the dogfooders.
        log.error(
            "R2D2_STRICT_NUMBERS=false: sending an answer with untraceable figures: %s",
            ", ".join(result.untraceable),
        )
        return text
