"""R2D2's /response API — the answering engine behind open questions.

GR-2 answers what it can from typed templates. Everything else classifies to
`Path.LLM` and, until now, hit the desk's catch-all redirect line. This is what
sits behind that path: R2D2 already runs an agentic loop over Groww's MCP tools,
so GR-2 asks a question and receives prose plus the tool results behind it.

Nothing here decides whether the answer is fit to send. That is `answer.py`,
deliberately separate: a transport that also judged its own output would be a
transport nobody could reason about.

WORK AGENT: `parse_consolidated` is the only function you should need to change.
Everything above it is transport and everything below it is policy. See
docs/INTEGRATE_R2D2.md.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

RESPONSE_PATH = "/response"


class R2D2Unavailable(RuntimeError):
    """R2D2 could not be reached, or answered in a shape we do not understand.

    One exception for both because the caller treats them identically: fall
    back to the deterministic reply. They are distinguished in the log, where
    the difference actually matters to whoever is debugging.
    """


@dataclass(frozen=True)
class Answer:
    """One answer, and the evidence for every number in it."""

    text: str
    # Tool results, keyed by tool name. Fed to NumericGuard as the slot map:
    # guard.allowed() already walks nested dicts and lists, so this goes in
    # exactly as R2D2 returned it. Empty means every figure in `text` is
    # unverifiable and the answer will be withheld.
    facts: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def grounded(self) -> bool:
        return bool(self.facts)


# Field names to try, in order, when reading the consolidated object. Listed
# rather than hardcoded because the shape is knowable from ml-r2d2-backend and
# guessing wrong should be a one-line fix, not a rewrite.
_TEXT_KEYS = ("response", "answer", "text", "content", "message", "output", "final_response")
_TOOLS_KEYS = ("tools_called", "tool_calls", "tools", "steps", "trace")
_TOOL_NAME_KEYS = ("name", "tool", "tool_name", "function")
_TOOL_OUTPUT_KEYS = ("output", "result", "response", "tool_output", "data")


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _text_of(value: Any) -> str:
    """Pull prose out of whatever shape the text field turned out to be."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        inner = _first(value, _TEXT_KEYS)
        return _text_of(inner) if inner is not None else ""
    if isinstance(value, list):
        return "\n".join(p for p in (_text_of(v) for v in value) if p).strip()
    return ""


class R2D2Client:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        auth_header: str = "Authorization",
        auth_scheme: str = "Bearer",
        timeout_s: float = 45.0,
        stream: bool = True,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._stream = stream
        self._timeout = timeout_s
        headers = {"content-type": "application/json"}
        if api_key:
            headers[auth_header] = f"{auth_scheme} {api_key}".strip()
        self._http = http or httpx.AsyncClient(timeout=timeout_s, headers=headers)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def ask(
        self,
        question: str,
        *,
        context: dict | None = None,
        on_heartbeat: Callable[[], Awaitable[None]] | None = None,
    ) -> Answer:
        """Ask R2D2 and return the consolidated answer.

        Streaming is consumed but the tokens are thrown away. WhatsApp sends one
        message and Baileys edits render poorly, so a progressive answer has
        nowhere to go. What the stream is actually for is `on_heartbeat`: an
        agentic call can run half a minute, and WhatsApp's typing indicator
        expires in about ten seconds, so without a liveness signal the user
        watches it stop and concludes the bot died.
        """
        body = {"query": question, **(context or {})}
        try:
            payload = (
                await self._stream_consolidated(body, on_heartbeat)
                if self._stream
                else await self._post(body)
            )
        except httpx.HTTPError as exc:
            raise R2D2Unavailable(f"{type(exc).__name__}: {exc}") from exc
        return self.parse_consolidated(payload)

    async def _post(self, body: dict) -> dict:
        r = await self._http.post(f"{self._base}{RESPONSE_PATH}", json=body)
        r.raise_for_status()
        return r.json()

    async def _stream_consolidated(
        self, body: dict, on_heartbeat: Callable[[], Awaitable[None]] | None
    ) -> dict:
        """Read the stream for liveness; keep the last object that carries text.

        Handles server-sent events and newline-delimited JSON with the same
        loop, because which one it is is a detail of R2D2's framework rather
        than of this integration.
        """
        best: dict = {}
        async with self._http.stream(
            "POST", f"{self._base}{RESPONSE_PATH}", json={**body, "stream": True}
        ) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if on_heartbeat is not None:
                    await on_heartbeat()
                line = line.strip()
                if not line or line == "[DONE]":
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line in ("", "[DONE]") or not line.startswith("{"):
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Keep the richest object seen: the consolidated one arrives
                # last, but a truncated stream should still yield the best
                # partial rather than nothing.
                if isinstance(chunk, dict) and _first(chunk, _TEXT_KEYS) is not None:
                    best = chunk
        if not best:
            raise R2D2Unavailable("stream carried no object with an answer in it")
        return best

    # ---- the seam ---------------------------------------------------

    @staticmethod
    def parse_consolidated(payload: dict) -> Answer:
        """Map R2D2's consolidated object onto `Answer`.

        WORK AGENT: this is the function to adjust. Read the /response handler
        in ml-r2d2-backend and confirm three things:

          1. Where the final prose sits          -> _TEXT_KEYS
          2. Whether tool results are returned   -> _TOOLS_KEYS, _TOOL_OUTPUT_KEYS
          3. What one tool entry looks like      -> _TOOL_NAME_KEYS

        Point 2 is the one that matters. Every number GR-2 sends has to trace
        back to a tool result (invariant I1, enforced in app/compose/guard.py).
        Tool results are what R2D2's figures trace to. Without them the guard
        has nothing to check against and every answer containing a figure is
        withheld, so if /response does not return them, say so rather than
        working around it — the fallback is described in the integration doc.
        """
        if not isinstance(payload, dict):
            raise R2D2Unavailable(f"expected a JSON object, got {type(payload).__name__}")

        body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        text = _text_of(_first(body, _TEXT_KEYS))
        if not text:
            raise R2D2Unavailable(
                f"no answer text in the response; top-level keys were {sorted(body)[:12]}. "
                "Adjust _TEXT_KEYS in app/agent/r2d2.py"
            )

        raw_tools = _first(body, _TOOLS_KEYS) or []
        if isinstance(raw_tools, dict):
            raw_tools = list(raw_tools.values())

        facts: dict[str, Any] = {}
        tool_calls: list[dict] = []
        for i, entry in enumerate(raw_tools if isinstance(raw_tools, list) else []):
            if not isinstance(entry, dict):
                continue
            name = str(_first(entry, _TOOL_NAME_KEYS) or f"tool_{i}")
            out = _first(entry, _TOOL_OUTPUT_KEYS)
            if out is not None:
                # Suffixed so two calls to the same tool do not overwrite each
                # other: both sets of numbers have to stay in the pool.
                facts[f"{name}_{i}"] = out
            tool_calls.append({"name": name, "ms": entry.get("ms") or entry.get("duration_ms")})

        if not facts:
            log.warning(
                "R2D2 returned no tool results; any figure in this answer is unverifiable "
                "(keys present: %s)",
                sorted(body)[:12],
            )
        return Answer(text=text, facts=facts, tool_calls=tool_calls, raw=payload)
