"""Two-tier routing (spec §6.1).

Twelve of the twenty-four intents need no model at all. That cuts p50 latency
by 3-4x and LLM spend by roughly 70% — which for a solo build is the
difference between shipping and not.

Router patterns include romanised Hindi, because Indian users code-switch
constantly: "nifty ka kya scene hai", "mera pnl kitna hai" (spec §6.4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class Path(StrEnum):
    FAST = "fast"
    LLM = "llm"
    REJECT = "reject"


class Intent(StrEnum):
    PORTFOLIO_SUMMARY = "portfolio.summary"
    PORTFOLIO_HOLDING = "portfolio.holding"
    PORTFOLIO_DAY_CHANGE = "portfolio.day_change"
    POSITIONS_OPEN = "positions.open"
    POSITIONS_DETAIL = "positions.detail"
    POSITIONS_RISK = "positions.risk"
    ORDERS_OPEN = "orders.open"
    ORDERS_STATUS = "orders.status"
    ORDERS_HISTORY = "orders.history"
    MARGIN_AVAILABLE = "margin.available"
    MARGIN_UTILISATION = "margin.utilisation"
    MARKET_QUOTE = "market.quote"
    MARKET_INDEX = "market.index"
    MARKET_OHLC = "market.ohlc"
    FNO_CHAIN = "fno.chain"
    FNO_LEVELS = "fno.levels"
    FNO_GREEKS = "fno.greeks"
    FNO_PAYOFF = "fno.payoff"
    ANALYSIS_WHY = "analysis.why"
    ANALYSIS_EXPLAIN = "analysis.explain"
    WATCH_ADD = "watch.add"
    WATCH_LIST = "watch.list"
    META_LINK = "meta.link"
    META_UNLINK = "meta.unlink"
    META_HELP = "meta.help"
    META_SETTINGS = "meta.settings"
    REJECT = "reject"


# Intents answered without touching a model.
FAST_INTENTS = frozenset(
    {
        Intent.PORTFOLIO_SUMMARY,
        Intent.PORTFOLIO_HOLDING,
        Intent.PORTFOLIO_DAY_CHANGE,
        Intent.POSITIONS_OPEN,
        Intent.POSITIONS_DETAIL,
        Intent.ORDERS_OPEN,
        Intent.ORDERS_STATUS,
        Intent.ORDERS_HISTORY,
        Intent.MARGIN_AVAILABLE,
        Intent.MARGIN_UTILISATION,
        Intent.MARKET_QUOTE,
        Intent.MARKET_INDEX,
        Intent.MARKET_OHLC,
        Intent.FNO_CHAIN,
        Intent.FNO_LEVELS,
        Intent.FNO_GREEKS,
        Intent.WATCH_ADD,
        Intent.WATCH_LIST,
        Intent.META_LINK,
        Intent.META_UNLINK,
        Intent.META_HELP,
        Intent.META_SETTINGS,
    }
)

INDEX_WORDS = r"nifty|sensex|banknifty|bank\s*nifty|finnifty|midcpnifty|bnf|bankex"

FAST_PATTERNS: list[tuple[str, Intent]] = [
    (rf"^\s*(market|markets|indices|index|{INDEX_WORDS})\s*$", Intent.MARKET_INDEX),
    (r"^\s*(portfolio|pf|holdings?|how am i doing|kitna hua|kaisa chal raha)\s*$",
     Intent.PORTFOLIO_SUMMARY),
    (r"^\s*(positions?|pos|open positions?)\s*$", Intent.POSITIONS_OPEN),
    (r"^\s*(orders?|pending|open orders?|pending orders?)\s*$", Intent.ORDERS_OPEN),
    (r"^\s*(margin|balance|funds?|kitna balance|kitna margin)\s*$", Intent.MARGIN_AVAILABLE),
    (r"^\s*(margin (used|utilisation|utilization)|how much margin)\s*\??$",
     Intent.MARGIN_UTILISATION),
    (r"^\s*(pnl|p\s*&\s*l|p and l|profit|loss|today'?s pnl|day pnl|aaj ka pnl|"
     r"mera pnl( kitna( hai)?)?|kitna (fayda|nuksan))\s*\??$", Intent.PORTFOLIO_DAY_CHANGE),
    (r"^\s*(did my order fill|order status|filled\??|bhar gaya\??)\s*$", Intent.ORDERS_STATUS),
    (r"^\s*(what did i trade( today)?|trades?( today)?|kal kya khareeda|aaj kya khareeda)\s*\??$",
     Intent.ORDERS_HISTORY),
    (r"^\s*(help|what can you do|commands?|\?)\s*$", Intent.META_HELP),
    (r"^\s*(link|connect|connect (my )?account)\s*$", Intent.META_LINK),
    (r"^\s*(unlink|disconnect|delete my data)\s*$", Intent.META_UNLINK),
    (r"^\s*(settings|prefs|preferences|mute|quiet|quiet hours|stop)\s*$", Intent.META_SETTINGS),
    (r"^\s*(watch(es|list)?|my alerts|alerts)\s*$", Intent.WATCH_LIST),
    (r"\b(option chain|chain)\b", Intent.FNO_CHAIN),
    (r"\b(max oi|oi wall|support|resistance|oi profile)\b", Intent.FNO_LEVELS),
    (r"\b(delta|gamma|theta|vega|greeks?)\b", Intent.FNO_GREEKS),
    (r"\b(open high low|ohlc)\b", Intent.MARKET_OHLC),
    (r"\btell me (if|when)\b.*\b(hits?|crosses|above|below|touches)\b", Intent.WATCH_ADD),
]

LLM_PATTERNS: list[tuple[str, Intent]] = [
    (r"\b(why|kyun|kyu|what happened|kya hua)\b", Intent.ANALYSIS_WHY),
    (r"\b(what is|what'?s a|explain|samjhao|matlab kya)\b", Intent.ANALYSIS_EXPLAIN),
    (r"\b(risk|exposed|exposure|am i safe)\b", Intent.POSITIONS_RISK),
    (r"\b(payoff|breakeven|break even)\b", Intent.FNO_PAYOFF),
]

_COMPILED_FAST = [(re.compile(p, re.I), i) for p, i in FAST_PATTERNS]
_COMPILED_LLM = [(re.compile(p, re.I), i) for p, i in LLM_PATTERNS]

# Anything not about markets or this user's Groww account gets one redirect
# line. No helpful improvisation — that is what keeps it specialised.
OUT_OF_SCOPE = re.compile(
    r"\b(weather|joke|recipe|movie|song|translate|code|python|email|who (is|are)|"
    r"president|capital of|meaning of life)\b",
    re.I,
)

MAX_FAST_TOKENS = 3


@dataclass
class Route:
    intent: Intent
    path: Path
    query: str
    entities: dict[str, object] = field(default_factory=dict)
    matched: str = ""


def classify(text: str, resolves_to_instrument=None) -> Route:
    """Route a message.

    `resolves_to_instrument` is an optional callable: given a short token, it
    returns True when that token resolves to exactly one instrument. A bare
    symbol is the most common message there is, so it gets the fast path.
    """
    raw = (text or "").strip()
    if not raw:
        return Route(Intent.REJECT, Path.REJECT, raw)

    if OUT_OF_SCOPE.search(raw):
        return Route(Intent.REJECT, Path.REJECT, raw, matched="out-of-scope")

    for pattern, intent in _COMPILED_FAST:
        if pattern.search(raw):
            return Route(intent, Path.FAST, raw, matched=pattern.pattern)

    # A bare token that resolves to exactly one instrument is a quote request.
    tokens = raw.split()
    if len(tokens) <= MAX_FAST_TOKENS and resolves_to_instrument:
        if resolves_to_instrument(raw):
            return Route(Intent.MARKET_QUOTE, Path.FAST, raw, matched="instrument")

    for pattern, intent in _COMPILED_LLM:
        if pattern.search(raw):
            return Route(intent, Path.LLM, raw, matched=pattern.pattern)

    return Route(Intent.ANALYSIS_EXPLAIN, Path.LLM, raw, matched="fallback")
