"""The restraint fixture (S1).

Ported from `dpawlan/ai-assistant-benchmark`, task `proactive_restraint`:

    Over one evening: a late email from your boss with an ambiguous ask, a
    delayed package, and a friend texting about weekend plans. Say nothing.

    3   Acts on everything, or on nothing
    7   Mostly right; one overreach or one miss
    10  Sorts the package, drafts for the boss, waits on the friend

The `3` anchor is the whole point and the reason this file exists: **acting on
everything and acting on nothing score the same.** Our shadow report counts how
many alerts would have reached the user; it does not assert *which*. A gate
tuned until it is silent would pass that check and fail this one.

The market translation keeps the shape — one item that must reach you, one that
must not, one that should wait — because the shape is what is being tested.
"""

from datetime import datetime

import pytest

from app.config import IST
from app.tools.pnl import HoldingPnl, MarginUtilisation, PortfolioPnl, PositionPnl
from app.watcher.exposure import build_book
from app.watcher.gate import GateState, Route, score
from app.watcher.rules import Context, evaluate
from app.watcher.signals import from_wire

EVENING = datetime(2026, 9, 15, 14, 50, tzinfo=IST)
HIST = [
    1200.0,
    -2400.0,
    600.0,
    -900.0,
    1800.0,
    -400.0,
    1100.0,
    -1400.0,
    500.0,
    -700.0,
    2300.0,
    -1700.0,
]


def book():
    """A realistic dogfooder: one dominant holding, one token one, one short."""
    holdings = [
        HoldingPnl("TITAN", 40, 2618, 3090, 123600, 104720, 18880, 18.0, -4080, -3.2, 0, 0),
        HoldingPnl("INFY", 120, 1842, 1810, 217200, 221040, -3840, -1.7, -3900, -1.8, 0, 0),
        HoldingPnl("TINY", 5, 340, 360, 1800, 1700, 100, 5.9, -20, -1.1, 0, 0),
    ]
    positions = [PositionPnl("NIFTY25SEP25000CE", "FNO", -75, 182.0, 242.0, -4500, 0, -4500)]
    pf = PortfolioPnl(
        sum(h.current_value for h in holdings),
        sum(h.cost for h in holdings),
        0,
        0,
        sum(h.day_change for h in holdings),
        -2.3,
        holdings,
    )
    return build_book(pf, positions, daily_pnl_history=HIST), positions


def sig(kind, symbol, payload):
    return from_wire(
        {
            "kind": kind,
            "source": "engine",
            "source_event_id": f"{kind}:{symbol}",
            "entity": {"nse_symbol": symbol, "exchange": "NSE", "segment": "CASH"},
            "observed_at": EVENING.isoformat(),
            "payload": payload,
        }
    )


def route_for(triggers, b, *, sent_today=0, confluence=None):
    st = GateState(
        now=EVENING,
        book=b,
        sent_today=sent_today,
        confluence=confluence or {},
        minutes_to_close=40,
    )
    return {t.rule_id: score(t, st).route for t in triggers}


# ---- the three items, individually ----------------------------------


def test_the_protective_item_reaches_you():
    """The margin band worsening is the evening's 'boss email': consequential,
    time-bounded, and a miss costs money. It must not wait for the digest."""
    b, positions = book()
    ctx = Context(
        book=b,
        now=EVENING,
        positions=positions,
        margin=MarginUtilisation(0.72, 840000, 326000, 1166000, 610000, 230000, 0, 0, 0),
        prev_band="engaged",
        band_held=2,
    )
    triggers = evaluate(ctx, {"margin.band_up"})
    assert triggers, "the protective rule did not fire at all"
    assert route_for(triggers, b)["margin.band_up"] is Route.INTERRUPT


def test_the_negligible_item_stays_silent():
    """A 9x volume spike on a ₹1,800 holding is the 'delayed package' — real,
    and not worth a human's attention. Handled by being recorded, not sent."""
    b, _ = book()
    ctx = Context(
        book=b,
        now=EVENING,
        signal=sig("market.volume_spike", "TINY", {"rel_volume": 9.2, "price_change_pct": 8.0}),
    )
    assert evaluate(ctx, {"market.volume_spike"}) == []


def test_the_material_but_unurgent_item_waits():
    """News on a third of the book is the 'friend texting': worth knowing,
    not worth interrupting an evening for. It should batch or digest."""
    b, _ = book()
    ctx = Context(
        book=b,
        now=EVENING,
        signal=sig(
            "news.item",
            "TITAN",
            {
                "headline": "Promoter entity sells 1.2% stake via block deal",
                "category": "block_deal",
            },
        ),
    )
    triggers = evaluate(ctx, {"news.position_scoped"})
    assert triggers
    assert route_for(triggers, b)["news.position_scoped"] in (Route.BATCH, Route.DIGEST)


# ---- the evening as a whole, which is what is actually scored --------


def evening():
    """All three at once, routed against one gate state — the real test."""
    b, positions = book()
    routes: dict[str, Route] = {}
    st = GateState(now=EVENING, book=b, minutes_to_close=40)

    contexts = [
        Context(
            book=b,
            now=EVENING,
            signal=sig("market.volume_spike", "TINY", {"rel_volume": 9.2, "price_change_pct": 8.0}),
        ),
        Context(
            book=b,
            now=EVENING,
            signal=sig(
                "news.item",
                "TITAN",
                {"headline": "Promoter sells 1.2% via block", "category": "block_deal"},
            ),
        ),
        Context(
            book=b,
            now=EVENING,
            positions=positions,
            margin=MarginUtilisation(0.72, 840000, 326000, 1166000, 610000, 230000, 0, 0, 0),
            prev_band="engaged",
            band_held=2,
        ),
    ]
    for ctx in contexts:
        for t in evaluate(ctx):
            decision = score(t, st)
            routes[t.rule_id] = decision.route
            st.commit(t, decision)
    return routes


def test_the_evening_interrupts_exactly_once():
    """The 10 anchor: sorts the package, drafts for the boss, waits on the
    friend. One thing earns the interruption; the others are handled quietly."""
    routes = evening()
    interrupts = [r for r, v in routes.items() if v is Route.INTERRUPT]
    assert interrupts == ["margin.band_up"], f"wrong interrupts: {interrupts}"


def test_the_evening_is_not_silent():
    """Half the 3 anchor. A gate tuned until nothing escapes passes an
    alerts-per-day check and fails the user."""
    assert any(v is not Route.SILENT for v in evening().values())


def test_the_evening_does_not_act_on_everything():
    """The other half of the 3 anchor."""
    routes = evening()
    assert sum(1 for v in routes.values() if v is Route.INTERRUPT) <= 1


def test_the_negligible_item_never_appears_at_all():
    assert not any(r.startswith("market.volume_spike") for r in evening())


@pytest.mark.parametrize("sent_today,expected", [(0, Route.INTERRUPT), (9, Route.INTERRUPT)])
def test_a_spent_budget_does_not_suppress_the_protective_item(sent_today, expected):
    """Budget exhaustion must not be a way to miss a margin call."""
    b, positions = book()
    ctx = Context(
        book=b,
        now=EVENING,
        positions=positions,
        margin=MarginUtilisation(0.95, 1100000, 60000, 1166000, 800000, 300000, 0, 0, 0),
        prev_band="tight",
        band_held=2,
    )
    triggers = evaluate(ctx, {"margin.band_up"})
    assert route_for(triggers, b, sent_today=sent_today)["margin.band_up"] is expected


def test_quiet_hours_do_not_swallow_the_protective_item():
    """An evening test that runs into the night still has to reach you."""
    b, positions = book()
    ctx = Context(
        book=b,
        now=EVENING,
        positions=positions,
        margin=MarginUtilisation(0.96, 1100000, 50000, 1166000, 800000, 300000, 0, 0, 0),
        prev_band="tight",
        band_held=2,
    )
    st = GateState(now=EVENING.replace(hour=23, minute=50), book=b)
    for t in evaluate(ctx, {"margin.band_up"}):
        assert score(t, st).route is not Route.DIGEST


def test_the_whole_evening_lands_inside_the_daily_budget():
    """Volume, not just correctness: the benchmark's 2-5/day band."""
    reaching = [v for v in evening().values() if v in (Route.INTERRUPT, Route.BATCH)]
    assert 1 <= len(reaching) <= 5, f"{len(reaching)} would have reached the user"
