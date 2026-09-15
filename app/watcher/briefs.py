"""The two bookends: one message before the open, one after the close.

These carry more of the product than any single alert. An alert proves the desk
is watching; the bookends are what make it a habit — the thing you read with
your first coffee and again on the way home. They fire on a schedule, so they
are the only messages whose arrival is predictable, and predictability is what
earns the right to interrupt at other times.

Two rules keep them from becoming wallpaper:

**Only send when there is something to say.** A brief for an empty book, or a
wrap on a day where nothing moved, teaches the reader to skim. Both return an
empty string rather than filler, and the scheduler sends nothing.

**Degrade, never pad.** Every section is optional. No margin data means no
margin line, not a zero. The overnight macro block appears when the signal
engine supplies it and is simply absent otherwise — the book-only brief is
still worth reading, which is why it is built that way round.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path

from app.render.templates import WARN, inr, signed
from app.tools.instruments import InstrumentIndex
from app.tools.pnl import MarginUtilisation, PortfolioPnl, PositionPnl
from app.watcher.exposure import Book, build_book
from app.watcher.schedule import Job


@dataclass(frozen=True)
class Expiring:
    symbol: str
    days: int
    strike: float
    spot: float

    @property
    def distance_pct(self) -> float:
        return abs(self.spot - self.strike) / self.strike * 100 if self.strike else 0.0

    @property
    def when(self) -> str:
        return {0: "expires today", 1: "expires tomorrow"}.get(
            self.days, f"expires in {self.days} sessions"
        )


@dataclass(frozen=True)
class BriefData:
    book: Book
    portfolio: PortfolioPnl
    positions: list[PositionPnl] = field(default_factory=list)
    margin: MarginUtilisation | None = None
    expiring: list[Expiring] = field(default_factory=list)
    suppressed: int = 0
    macro: str = ""  # supplied by the signal engine; absent is fine
    minutes_to_open: int | None = None

    @property
    def has_book(self) -> bool:
        return bool(self.portfolio.holdings or self.positions)


def _slots(d: BriefData) -> dict:
    """Every figure either brief may render, so the guard can trace it."""
    out: dict = {
        "total_value": d.portfolio.current_value,
        "day_pnl": d.portfolio.day_change,
        "day_pct": d.portfolio.day_change_pct,
        "holdings": len(d.portfolio.holdings),
        "positions": len(d.positions),
        "suppressed": d.suppressed,
        "macro": d.macro,
        "minutes_to_open": d.minutes_to_open,
    }
    if d.margin:
        out |= {
            "utilisation": d.margin.utilisation,
            "headroom": d.margin.headroom,
        }
    sigma = d.book.sigma_of(d.portfolio.day_change)
    if sigma is not None:
        out["sigma"] = sigma
    # Symbols are tool results too, and contract symbols carry digits
    # (NIFTY25SEP25000CE). Listing them makes those digits traceable without
    # loosening the guard, which is the only acceptable direction to fix this.
    out["symbols"] = [h.symbol for h in d.portfolio.holdings] + [p.symbol for p in d.positions]
    for e in d.expiring:
        out |= {
            f"{e.symbol}_sym": e.symbol,
            f"{e.symbol}_strike": e.strike,
            f"{e.symbol}_spot": e.spot,
            f"{e.symbol}_days": e.days,
            f"{e.symbol}_dist": e.distance_pct,
        }
    for h in d.portfolio.holdings:
        out[f"{h.symbol}_day"] = h.day_change
    return out


def _expiry_lines(d: BriefData, *, carry_only: bool = False) -> list[str]:
    """`carry_only` drops anything already settled — after the close, what
    matters is what you are still holding tomorrow morning."""
    lines: list[str] = []
    for e in d.expiring:
        if carry_only and e.days <= 0:
            continue
        side = "through" if e.spot > e.strike else "under"
        lines += [
            f"{WARN} {e.symbol} {e.when}",
            f"   Spot {inr(e.spot, 2)} · strike {inr(e.strike, 2)} · {e.distance_pct:.1f}% {side}",
        ]
    return lines


def pre_market(d: BriefData) -> tuple[str, dict]:
    """Before the open: what you are carrying in, and what is due today."""
    if not d.has_book:
        return "", {}

    # Computed, never written down. The guard caught this as a hand-typed
    # constant, which it also was in the worse sense: move the job by fifteen
    # minutes and a literal would have gone on confidently saying thirty.
    opening = f" Open in {d.minutes_to_open} minutes." if d.minutes_to_open is not None else ""
    lines = [f"Good morning.{opening}", ""]
    if d.macro:
        lines += [d.macro, ""]

    # Holdings value, not book.total_value: the latter includes the notional of
    # short legs, and a short option is an obligation rather than something you
    # own. Reporting it as book value overstates what the reader has.
    parts = [f"Book {inr(d.portfolio.current_value)}"]
    if d.portfolio.holdings:
        parts.append(f"{len(d.portfolio.holdings)} holdings")
    if d.positions:
        parts.append(f"{len(d.positions)} open")
    lines.append(" · ".join(parts))

    if d.margin:
        lines.append(
            f"Margin {d.margin.utilisation * 100:.0f}% used, {inr(d.margin.headroom)} free"
        )

    expiry = _expiry_lines(d)
    if expiry:
        lines += [""] + expiry

    return "\n".join(lines), _slots(d)


def post_close(d: BriefData) -> tuple[str, dict]:
    """After the close: what the day did to you, and what carries overnight."""
    if not d.has_book:
        return "", {}

    sigma = d.book.sigma_of(d.portfolio.day_change)
    head = f"Close. {signed(d.portfolio.day_change)} today"
    if sigma is not None and sigma >= 1.0:
        head += f", {sigma:.1f}× your usual day"
    lines = [head + ".", ""]

    movers = sorted(d.portfolio.holdings, key=lambda h: h.day_change)
    losers = [h for h in movers if h.day_change < 0][:2]
    winners = [h for h in reversed(movers) if h.day_change > 0][:2]
    if losers:
        lines.append(
            "Cost you   " + " · ".join(f"{h.symbol} {signed(h.day_change)}" for h in losers)
        )
    if winners:
        lines.append(
            "Made you   " + " · ".join(f"{h.symbol} {signed(h.day_change)}" for h in winners)
        )

    expiry = _expiry_lines(d, carry_only=True)
    if expiry:
        lines += [""] + expiry

    if d.suppressed:
        # Silence was a decision, not a failure — and if they routinely ask to
        # see the held-back items, the gate is too tight.
        lines += [
            "",
            f"Held back {d.suppressed} thing{'s' if d.suppressed > 1 else ''} today "
            f'— reply "show".',
        ]

    return "\n".join(lines), _slots(d)


# ---- fetch and deliver ----------------------------------------------

PRE_MARKET = "brief.pre_market"
POST_CLOSE = "brief.post_close"

RENDERERS = {PRE_MARKET: pre_market, POST_CLOSE: post_close}


async def fetch(tools, index, store, user_id: int) -> BriefData:
    """Assemble a brief's inputs. The only I/O in this module.

    A failure in any optional part costs that section, not the brief: margin is
    the piece most likely to be unavailable and the least essential to the
    reader, so it is fetched separately and allowed to be None.
    """
    import asyncio

    from app.tools.pnl import margin_utilisation, portfolio_pnl, position_pnl

    holdings, positions_raw = await asyncio.gather(tools.get_holdings(), tools.get_positions())
    keys = [f"NSE_{h.trading_symbol}" for h in holdings]
    ltps = await tools.get_ltp(keys, "CASH") if keys else {}
    quotes = {k.split("_", 1)[1]: v for k, v in ltps.items()}

    portfolio = portfolio_pnl(holdings, quotes, {})
    positions = [
        position_pnl(p, quotes.get(p.trading_symbol, p.credit_price or 0.0)) for p in positions_raw
    ]

    try:
        margin = margin_utilisation(await tools.get_margin())
    except Exception:  # noqa: BLE001 - a missing margin line beats no brief
        margin = None

    return BriefData(
        book=build_book(portfolio, positions),
        minutes_to_open=_minutes_to_open(),
        portfolio=portfolio,
        positions=positions,
        margin=margin,
        expiring=_expiring(positions, index),
        suppressed=await store.suppressions_today(user_id),
    )


def _minutes_to_open() -> int | None:
    """Minutes until the equity open, or None once it has opened."""
    from app.market.calendar import EQUITY_OPEN, now_ist

    now = now_ist()
    opens = now.replace(hour=EQUITY_OPEN.hour, minute=EQUITY_OPEN.minute, second=0, microsecond=0)
    if now >= opens:
        return None
    return int((opens - now).total_seconds() // 60)


def _expiring(positions: list[PositionPnl], index, within_days: int = 2) -> list[Expiring]:
    """Short option legs close to expiry, with where spot sits against strike."""
    from datetime import date

    today = date.today()
    out: list[Expiring] = []
    for p in positions:
        if p.net_qty >= 0:  # only a short leg can be assigned
            continue
        matches = index.by_symbol.get(p.symbol.upper(), [])
        inst = next((i for i in matches if i.expiry and i.strike), None)
        if inst is None:
            continue
        days = (inst.expiry - today).days
        if 0 <= days <= within_days:
            out.append(Expiring(p.symbol, days, inst.strike, p.ltp))
    return out


async def deliver(
    which: str, *, store, tools_for, index, enqueue, only_user: int | None = None
) -> int:
    """Compose and queue a brief for every linked user. Returns how many went out.

    One user's failure never costs another theirs — a dead credential or a
    broker timeout is per-user by nature, and a scheduled job that aborts
    halfway through silently drops everyone after the first failure.
    """
    import logging

    from app.compose.guard import guard
    from app.compose.voice import advice_violations

    log = logging.getLogger(__name__)
    render = RENDERERS[which]
    sent = 0

    column = "brief_pre_market" if which == PRE_MARKET else "brief_post_close"
    users = [only_user] if only_user is not None else await store.all_linked_users()

    for user_id in users:
        try:
            # Paused or snoozed means paused. A control command the scheduler
            # ignores is worse than not offering the command at all.
            if not await store.briefs_enabled(user_id):
                continue

            # A user who set their own time is served by their own job. The
            # idempotency key would mask a double-send here, but only when the
            # custom time is earlier than the default — relying on that would
            # mean someone asking for 09:30 quietly gets 08:45 instead.
            if only_user is None and (await store.get_prefs(user_id)).get(column):
                continue
            tools = await tools_for(user_id)
            body, slots = render(await fetch(tools, index, store, user_id))
            if not body:
                continue  # nothing worth saying
            body = guard(body, slots, where=which)
            if not body or advice_violations(body):
                log.error("%s for user %s failed its own checks; withheld", which, user_id)
                continue
            await enqueue(user_id, which, body, slots)
            sent += 1
        except Exception:
            log.exception("%s failed for user %s; continuing", which, user_id)
    return sent


# ---- scheduled jobs ------------------------------------------------


def jobs(store, tools_for, index) -> Awaitable[list[Job]]:
    """Everything the worker's scheduler runs. One place, so a new job is a line
    here rather than a closure buried in the process entrypoint."""
    from datetime import date, time

    from app.outbox import idempotency_key
    from app.tools.instruments import download, read_csv
    from app.watcher.schedule import Job

    log = logging.getLogger(__name__)

    async def enqueue(user_id: int, which: str, body: str, slots: dict) -> None:
        today = date.today()
        await store.enqueue_outbox(
            user_id,
            rule_id=which,
            fingerprint=f"{which}|{today}",
            idempotency_key=idempotency_key(user_id, which, "daily", today),
            body=body,
            route="interrupt",
            payload=slots,
        )

    def brief(kind: str, at: time, only_user: int | None = None) -> Job:
        # The job's name is its slot-ledger key, so a per-user job needs a name
        # of its own; the *kind* is what selects the renderer.
        name = kind if only_user is None else f"{kind}:u{only_user}"

        async def run() -> None:
            count = await deliver(
                kind,
                store=store,
                tools_for=tools_for,
                index=index,
                enqueue=enqueue,
                only_user=only_user,
            )
            log.info("%s queued for %d", name, count)

        return Job(name=name, at=at, fn=run, timeout_s=300)

    async def refresh_instruments() -> None:
        """Rebuild the master before the open. Runs every day, not just trading
        days, so a holiday edit to the file is still picked up."""
        path = Path("data/instruments.csv")
        download(path)
        fresh = InstrumentIndex(read_csv(path))
        index.__dict__.update(fresh.__dict__)  # swap in place, keep the reference
        log.info("instrument master refreshed: %d instruments", len(index))

    async def build() -> list[Job]:
        # After the close, not at it: the last prints need a moment to settle,
        # and a wrap quoting a stale close is worse than a late one.
        out = [
            Job(
                "instruments.refresh",
                time(7, 30),
                refresh_instruments,
                trading_days_only=False,
                timeout_s=600,
            ),
            brief(PRE_MARKET, time(8, 45)),
            brief(POST_CLOSE, time(15, 45)),
        ]
        # A user who moved a brief is served by a job of their own and is
        # skipped by the default one (see deliver). At ten users this is a
        # handful of jobs; at hundreds the shape to move to is one job that
        # sweeps by minute, not a scheduler change.
        for user_id in await store.all_linked_users():
            prefs = await store.get_prefs(user_id)
            for kind, column in (
                (PRE_MARKET, "brief_pre_market"),
                (POST_CLOSE, "brief_post_close"),
            ):
                if prefs.get(column):
                    out.append(brief(kind, prefs[column], only_user=user_id))
        return out

    return build()
