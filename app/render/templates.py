"""Slot-based rendering (spec §7.1).

Every number in an outbound message is substituted here, from a typed object.
The LLM never writes a digit (invariant I1) — it supplies at most one line of
prose, appended after the numbers.

Format rules (spec §1.3, §1.4):
  - six lines maximum before the user has to scroll
  - one decimal for percentages, none for rupee amounts above ₹1,000
  - Indian digit grouping throughout
  - colour via glyph: ▲ ▼ · and ⚠, no other emoji
  - timestamps on anything older than 60s
"""

from __future__ import annotations

from datetime import datetime

from app.config import IST
from app.tools.pnl import HoldingPnl, MarginUtilisation, PortfolioPnl, PositionPnl
from app.tools.types import Instrument, Order, Quote

UP, DOWN, FLAT, WARN = "▲", "▼", "·", "⚠"
MINUS = "−"  # U+2212, not a hyphen
STALE_AFTER_S = 60


def inr(x: float, decimals: int = 0) -> str:
    """Indian digit grouping. 1842600 -> '₹18,42,600'."""
    neg = x < 0
    s = f"{abs(x):.{decimals}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = ",".join([head[max(i - 2, 0) : i] for i in range(len(head), 0, -2)][::-1])
        whole = f"{head},{tail}"
    out = whole + (f".{frac}" if frac else "")
    return (f"{MINUS}₹" if neg else "₹") + out


def signed(x: float, decimals: int = 0) -> str:
    """Rupees with an explicit sign, for deltas."""
    return ("+" if x >= 0 else "") + inr(x, decimals)


def pct(x: float, decimals: int = 1) -> str:
    return f"{x:+.{decimals}f}%"


def arrow(x: float) -> str:
    return UP if x > 0 else (DOWN if x < 0 else FLAT)


def qty(x: float) -> str:
    """Quantities are whole units unless the instrument is fractional."""
    return f"{x:.0f}" if float(x).is_integer() else f"{x:g}"


def as_of(ts: datetime | None, *, scope: str = "") -> str:
    """Footer line. Staleness is stated, never hidden (invariant I5)."""
    parts = []
    if ts:
        age = (datetime.now(IST) - ts).total_seconds()
        parts.append(f"as of {ts.astimezone(IST):%H:%M}" if age > STALE_AFTER_S else "live")
    if scope:
        parts.append(scope)
    return "   " + " · ".join(parts) if parts else ""


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


# ---- portfolio -----------------------------------------------------


def portfolio_summary(p: PortfolioPnl, limit: int = 3) -> str:
    lines = [
        f"{arrow(p.day_change)} Portfolio {inr(p.current_value)}",
        f"   {signed(p.day_change)} today ({pct(p.day_change_pct, 2)})",
        "",
    ]
    for h in p.movers[:limit]:
        lines.append(
            f"   {h.symbol:<9} {arrow(h.day_change)} {abs(h.day_change_pct):.1f}%"
            f"  {signed(h.day_change)}"
        )
    lines.append("")
    lines.append(as_of(p.as_of, scope=_plural(len(p.holdings), "holding")))
    if p.missing_quotes:
        lines.append(f"   {WARN} no quote for {_unpriced(p.missing_quotes)}")
    return "\n".join(lines)


def _unpriced(symbols: list[str]) -> str:
    """Name the holdings we can name, count the ones we cannot.

    Groww returns the occasional holding with no trading symbol. It still has
    to be reported -- a holding we could not value is exactly what I5 says to
    say out loud -- but joining a blank name produced a warning that trailed
    off into nothing and read like a bug in the message itself.
    """
    named = [s for s in symbols if s.strip()]
    unnamed = len(symbols) - len(named)
    parts = [", ".join(named[:3])] if named else []
    if unnamed:
        parts.append(f"{unnamed} unnamed holding{'' if unnamed == 1 else 's'}")
    return " and ".join(parts)


def portfolio_day_change(p: PortfolioPnl) -> str:
    return "\n".join(
        [
            f"{arrow(p.day_change)} {signed(p.day_change)} today ({pct(p.day_change_pct, 2)})",
            f"   Portfolio {inr(p.current_value)}",
            f"   Overall {signed(p.unrealised)} ({pct(p.unrealised_pct, 1)})",
            "",
            as_of(p.as_of, scope=_plural(len(p.holdings), "holding")),
        ]
    )


def holding_detail(h: HoldingPnl, ts: datetime | None = None) -> str:
    held = qty(h.qty)
    if h.pledged:
        held += f" ({qty(h.pledged)} pledged)"
    lines = [
        f"{arrow(h.day_change)} {h.symbol} {inr(h.ltp, 2)}",
        f"   {signed(h.day_change)} today ({pct(h.day_change_pct)})",
        "",
        f"   {held} @ {inr(h.avg_price, 2)}",
        f"   Value {inr(h.current_value)} · {signed(h.unrealised)} ({pct(h.unrealised_pct)})",
    ]
    if h.t1:
        lines.append(f"   {qty(h.t1)} unsettled (T1)")
    lines.append(as_of(ts))
    return "\n".join(lines)


# ---- positions -----------------------------------------------------


def positions_open(rows: list[PositionPnl], ts: datetime | None = None) -> str:
    if not rows:
        return "No open positions."
    total = sum(r.total for r in rows)
    lines = [f"{arrow(total)} Positions {signed(total)}", ""]
    for r in rows[:4]:
        side = "L" if r.net_qty > 0 else "S"
        lines.append(f"   {r.symbol:<20} {side}{qty(abs(r.net_qty))}  {signed(r.unrealised)}")
    lines.append("")
    lines.append(as_of(ts, scope=_plural(len(rows), "position")))
    return "\n".join(lines)


def position_detail(r: PositionPnl, lot_size: int = 0, ts: datetime | None = None) -> str:
    size = qty(abs(r.net_qty))
    if lot_size > 1 and r.net_qty:
        lots = abs(r.net_qty) / lot_size
        size += f" ({qty(lots)} lot{'' if lots == 1 else 's'})"
    lines = [
        f"{arrow(r.unrealised)} {r.symbol}",
        f"   {r.direction.title()} {size} @ {inr(r.basis, 2)}",
        f"   LTP {inr(r.ltp, 2)}",
        "",
        f"   Open {signed(r.unrealised)} · Realised {signed(r.realised)}",
        f"   Total {signed(r.total)}",
    ]
    lines.append(as_of(ts))
    return "\n".join(lines)


# ---- margin --------------------------------------------------------


def margin_available(m: MarginUtilisation, ts: datetime | None = None) -> str:
    return "\n".join(
        [
            f"Margin {m.utilisation * 100:.0f}% used",
            f"   Available {inr(m.headroom)}",
            f"   Used {inr(m.used)} of {inr(m.used + m.headroom)}",
            "",
            f"   SPAN {inr(m.span)} + exposure {inr(m.exposure)}",
            as_of(ts),
        ]
    )


def margin_utilisation(m: MarginUtilisation, ts: datetime | None = None) -> str:
    glyph = WARN if m.utilisation >= 0.85 else FLAT
    return "\n".join(
        [
            f"{glyph} Margin at {m.utilisation * 100:.0f}%",
            f"   Used {inr(m.used)} · headroom {inr(m.headroom)}",
            f"   SPAN {inr(m.span)} + exposure {inr(m.exposure)}",
            f"   Collateral {inr(m.collateral_available)}",
            as_of(ts),
        ]
    )


# ---- market --------------------------------------------------------


def quote_line(q: Quote, label: str | None = None) -> str:
    return (
        f"{arrow(q.day_change)} {label or q.trading_symbol} {inr(q.last_price, 2)}"
        f"  {pct(q.day_change_perc, 2)}"
    )


def market_quote(q: Quote, closed_label: str = "") -> str:
    lines = [
        quote_line(q),
        f"   {signed(q.day_change, 2)} today",
        "",
        f"   O {inr(q.ohlc.open, 2)}  H {inr(q.ohlc.high, 2)}",
        f"   L {inr(q.ohlc.low, 2)}  C {inr(q.ohlc.close, 2)}",
    ]
    lines.append("   " + closed_label if closed_label else as_of(q.as_of))
    return "\n".join(lines)


def market_index(quotes: list[Quote], ts: datetime | None = None, closed_label: str = "") -> str:
    lines = ["Markets", ""]
    lines.extend(f"   {quote_line(q)}" for q in quotes[:4])
    lines.append("")
    lines.append("   " + closed_label if closed_label else as_of(ts))
    return "\n".join(lines)


def market_ohlc(symbol: str, q: Quote) -> str:
    return "\n".join(
        [
            f"{arrow(q.day_change)} {symbol}",
            f"   Open {inr(q.ohlc.open, 2)}",
            f"   High {inr(q.ohlc.high, 2)}",
            f"   Low  {inr(q.ohlc.low, 2)}",
            f"   Close {inr(q.ohlc.close, 2)}",
            as_of(q.as_of),
        ]
    )


# ---- orders --------------------------------------------------------


def orders_open(orders: list[Order], ts: datetime | None = None) -> str:
    if not orders:
        return "No open orders."
    lines = [f"{_plural(len(orders), 'open order').capitalize()}", ""]
    for o in orders[:4]:
        price = inr(o.price, 2) if o.price else "MKT"
        lines.append(
            f"   {o.transaction_type[:1]} {o.trading_symbol:<18} {qty(o.quantity)} @ {price}"
        )
    lines.append("")
    lines.append(as_of(ts))
    return "\n".join(lines)


def order_status(o: Order) -> str:
    lines = [
        f"{o.trading_symbol} — {o.status.replace('_', ' ').title()}",
        f"   {o.transaction_type.title()} {qty(o.quantity)} @ "
        f"{inr(o.price, 2) if o.price else 'MKT'}",
    ]
    if o.filled_quantity:
        lines.append(f"   Filled {qty(o.filled_quantity)} @ {inr(o.average_fill_price, 2)}")
    if o.remaining_quantity:
        lines.append(f"   Pending {qty(o.remaining_quantity)}")
    return "\n".join(lines)


# ---- disambiguation and errors -------------------------------------


def disambiguate(query: str, candidates: list[Instrument]) -> tuple[str, list[tuple[str, str]]]:
    """Never guess on a close call — return a list message (spec §5.4 step 6)."""
    text = f'Which "{query}"?'
    rows = [(i.trading_symbol, _describe(i)) for i in candidates[:3]]
    return text, rows


def _describe(i: Instrument) -> str:
    if i.instrument_type in {"CE", "PE"} and i.strike:
        return f"{i.underlying} {i.strike:g} {i.instrument_type} · {i.expiry} · {i.exchange}"
    if i.instrument_type == "FUT":
        return f"{i.underlying} FUT · {i.expiry} · {i.exchange}"
    return f"{i.name or i.trading_symbol} · {i.exchange}"


def not_found(query: str) -> str:
    return f'Couldn\'t find "{query}".'


OUT_OF_SCOPE = (
    "I only do markets and your Groww account. "
    "Try: portfolio, positions, orders, margin, or a stock name."
)
RECONNECT = "Reconnect needed — your Groww session expired. Reply LINK to reconnect."
RATE_LIMITED = "Groww is rate-limiting me. Try again in a minute."
TOO_SLOW = "Taking too long, try again."

HELP = """I'm your Groww desk. Ask me:

   portfolio · positions · orders · margin
   nifty · reliance · nifty 25000 ce
   today's pnl · option chain

Type a stock name for a quote."""
