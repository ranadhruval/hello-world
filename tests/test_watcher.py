"""The proactive layer: exposure, rules, gate, guard, shadow.

The claims these tests pin are the ones that make the difference between an
advisor and a stock ticker: that a signal is judged against the user's own book
rather than a constant, that silence is a decision, and that a number can never
reach a message without coming from a tool.
"""

from datetime import date, datetime, time, timedelta

import pytest

import app.market.calendar as cal
from app.compose.alerts import compose
from app.compose.guard import check, guard
from app.config import IST
from app.market.calendar import SessionState, minutes_to_close, next_open, session_state
from app.tools.pnl import HoldingPnl, MarginUtilisation, PortfolioPnl, PositionPnl
from app.tools.types import Greeks
from app.watcher import rules as R
from app.watcher.exposure import build_book
from app.watcher.gate import GateState, Route, score
from app.watcher.rules import Context, Family, Trigger, evaluate, margin_band
from app.watcher.shadow import ShadowEntry, ShadowLog, render_report
from app.watcher.signals import MissingField, from_wire

IST_NOON = datetime(2026, 9, 15, 12, 0, tzinfo=IST)  # a Tuesday


def h(symbol="TITAN", qty=40, value=123600.0, cost=104720.0, pct_=18.0, day=-4080.0):
    return HoldingPnl(
        symbol, qty, cost / qty, value / qty, value, cost, value - cost, pct_, day, -3.2, 0, 0
    )


def book(*holdings, sigma_history=None, positions=None):
    total = sum(x.current_value for x in holdings)
    p = PortfolioPnl(
        total,
        sum(x.cost for x in holdings),
        0,
        0,
        sum(x.day_change for x in holdings),
        -2.3,
        list(holdings),
    )
    return build_book(p, positions or [], daily_pnl_history=sigma_history)


def sig(kind, symbol="TITAN", payload=None, absent=(), at=IST_NOON):
    return from_wire(
        {
            "kind": kind,
            "source": "test",
            "source_event_id": "e1",
            "entity": {"nse_symbol": symbol, "exchange": "NSE", "segment": "CASH"},
            "observed_at": at.isoformat(),
            "payload": payload or {},
            "absent": list(absent),
        }
    )


# ---- signals: missing is a value -----------------------------------


def test_absent_field_is_none_not_zero():
    """A source that cannot compute a ratio must not have it become 0."""
    s = sig("market.volume_spike", payload={"rel_volume": None}, absent=["rel_volume"])
    assert s.number("rel_volume") is None
    assert s.has("rel_volume") is False


def test_requiring_an_absent_field_raises():
    s = sig("market.volume_spike", payload={}, absent=["rel_volume"])
    with pytest.raises(MissingField):
        s.require("rel_volume")


def test_unparseable_number_is_none_not_a_guess():
    s = sig("market.volume_spike", payload={"rel_volume": "n/a"})
    assert s.number("rel_volume") is None


def test_event_time_wins_over_observation_time():
    """News surfaces days after publication; the gap is the staleness signal."""
    s = from_wire(
        {
            "kind": "news.item",
            "source": "t",
            "source_event_id": "1",
            "entity": {"nse_symbol": "TITAN"},
            "observed_at": "2026-09-15T12:00:00+05:30",
            "event_at": "2026-09-09T09:00:00+05:30",
            "payload": {},
        }
    )
    assert s.at.day == 9
    assert s.is_stale(IST_NOON)


def test_fresh_news_is_not_stale():
    s = sig("news.item", at=IST_NOON - timedelta(minutes=5))
    assert not s.is_stale(IST_NOON)


# ---- exposure: relative to the user, never a constant ---------------


def test_share_of_book_is_computed_from_the_whole_book():
    b = book(h("TITAN", value=123600.0), h("INFY", value=217200.0))
    assert b.get("TITAN").share_of_book == pytest.approx(0.3627, abs=1e-3)


def test_a_dominant_holding_is_material_even_on_a_small_book():
    """With three positions the median is often the position being scored, so
    the spec's notional/typical ratio alone says a third of someone's wealth is
    unremarkable. Share of book is what stops that."""
    b = book(h("TITAN", value=300000.0), h("INFY", value=20000.0))
    assert b.materiality("TITAN") == pytest.approx(1.6)


def test_an_unheld_name_scores_at_the_floor_not_zero():
    """Market context has some value; it just never interrupts on its own."""
    assert book(h()).materiality("WIPRO") == 0.6


def test_account_level_events_are_materially_neutral():
    """Margin is about the whole book, so there is no notional to compare —
    it must not be scored as though the user does not hold it."""
    assert book(h()).materiality("ACCOUNT") == 1.0


def test_sigma_is_none_without_enough_history():
    """Two days of history produce a number that behaves like a coin flip."""
    assert book(h(), sigma_history=[100.0, -200.0]).daily_sigma is None


def test_sigma_of_a_move_is_none_when_history_is_missing():
    assert book(h()).sigma_of(50000.0) is None


def test_short_legs_count_as_exposure():
    pos = [PositionPnl("NIFTY25SEP25000CE", "FNO", -75, 182.0, 210.0, -2100, 0, -2100)]
    b = book(h(), positions=pos)
    assert b.get("NIFTY25SEP25000CE").notional == pytest.approx(15750.0)


# ---- rules ----------------------------------------------------------


def ctx(b, **kw):
    return Context(book=b, now=IST_NOON, **kw)


def test_volume_spike_needs_a_normalised_ratio():
    """Un-normalised relative volume makes every stock spike at 09:20."""
    b = book(h())
    c = ctx(b, signal=sig("market.volume_spike", payload={"volume": 4e6}, absent=["rel_volume"]))
    assert evaluate(c, {"market.volume_spike"}) == []


def test_volume_spike_fires_on_a_held_name():
    b = book(h())
    c = ctx(
        b, signal=sig("market.volume_spike", payload={"rel_volume": 4.1, "price_change_pct": -3.2})
    )
    out = evaluate(c, {"market.volume_spike"})
    assert len(out) == 1 and out[0].entity == "TITAN"


def test_signals_about_unheld_names_produce_nothing():
    b = book(h("TITAN"))
    c = ctx(b, signal=sig("market.volume_spike", symbol="WIPRO", payload={"rel_volume": 9.0}))
    assert evaluate(c, {"market.volume_spike"}) == []


def test_a_negligible_holding_does_not_trigger():
    """A 20% day in a ₹1,800 holding is a fun fact, not an alert."""
    b = book(h("TITAN", value=900000.0), h("TINY", qty=5, value=1800.0, cost=1700.0))
    c = ctx(b, signal=sig("market.volume_spike", symbol="TINY", payload={"rel_volume": 9.0}))
    assert evaluate(c, {"market.volume_spike"}) == []


def test_stale_news_does_not_fire():
    b = book(h())
    old = sig(
        "news.item",
        payload={"headline": "x", "category": "results"},
        at=IST_NOON - timedelta(days=3),
    )
    assert evaluate(ctx(b, signal=old), {"news.position_scoped"}) == []


def test_margin_band_needs_two_consecutive_evaluations():
    """A value resting on a boundary otherwise alerts every tick."""
    m = MarginUtilisation(0.72, 840000, 326000, 1166000, 610000, 230000, 0, 0, 0)
    b = book(h())
    assert evaluate(ctx(b, margin=m, prev_band="engaged", band_held=1), {"margin.band_up"}) == []
    assert (
        len(evaluate(ctx(b, margin=m, prev_band="engaged", band_held=2), {"margin.band_up"})) == 1
    )


def test_improving_margin_is_not_news():
    m = MarginUtilisation(0.40, 400000, 600000, 1000000, 300000, 100000, 0, 0, 0)
    c = ctx(book(h()), margin=m, prev_band="tight", band_held=3)
    assert evaluate(c, {"margin.band_up"}) == []


@pytest.mark.parametrize(
    "util,band",
    [
        (0.10, "comfortable"),
        (0.55, "engaged"),
        (0.72, "tight"),
        (0.88, "stressed"),
        (0.97, "critical"),
    ],
)
def test_margin_bands(util, band):
    assert margin_band(util) == band


def test_itm_short_escalates_towards_expiry():
    pos = [PositionPnl("NIFTY25SEP25000CE", "FNO", -75, 182.0, 210.0, -13500, 0, -13500)]
    b = book(h(), positions=pos)
    mags = []
    for days in (3, 0):
        c = ctx(
            b,
            positions=pos,
            expiries={"NIFTY25SEP25000CE": IST_NOON.date() + timedelta(days=days)},
            strikes={"NIFTY25SEP25000CE": 25000.0},
            spot={"NIFTY25SEP25000CE": 25180.0},
        )
        mags.append(evaluate(c, {"expiry.itm_short"})[0].magnitude)
    assert mags[1] > mags[0]


def test_out_of_the_money_short_does_not_fire():
    pos = [PositionPnl("NIFTY25SEP25000CE", "FNO", -75, 182.0, 120.0, 4000, 0, 4000)]
    c = ctx(
        book(h()),
        positions=pos,
        expiries={"NIFTY25SEP25000CE": IST_NOON.date()},
        strikes={"NIFTY25SEP25000CE": 25000.0},
        spot={"NIFTY25SEP25000CE": 24800.0},
    )
    assert evaluate(c, {"expiry.itm_short"}) == []


def test_a_long_option_cannot_be_assigned():
    pos = [PositionPnl("NIFTY25SEP25000CE", "FNO", 75, 182.0, 210.0, 2100, 0, 2100)]
    c = ctx(
        book(h()),
        positions=pos,
        expiries={"NIFTY25SEP25000CE": IST_NOON.date()},
        strikes={"NIFTY25SEP25000CE": 25000.0},
        spot={"NIFTY25SEP25000CE": 25180.0},
    )
    assert evaluate(c, {"expiry.itm_short"}) == []


def test_delta_drift_fires_when_risk_changes_shape():
    pos = [PositionPnl("NIFTY25SEP25000CE", "FNO", -75, 182.0, 210.0, -2100, 0, -2100)]
    c = ctx(
        book(h()),
        positions=pos,
        greeks={"NIFTY25SEP25000CE": Greeks(delta=-0.44)},
        entry_greeks={"NIFTY25SEP25000CE": Greeks(delta=-0.19)},
    )
    assert len(evaluate(c, {"greeks.delta_drift"})) == 1


def test_day_move_is_measured_in_the_users_own_sigmas():
    hist = [
        1000.0,
        -2000.0,
        500.0,
        -800.0,
        1500.0,
        -300.0,
        900.0,
        -1200.0,
        400.0,
        -600.0,
        2000.0,
        -1500.0,
    ]
    quiet = book(h(day=-500.0), sigma_history=hist)
    loud = book(h(day=-9000.0), sigma_history=hist)
    assert evaluate(ctx(quiet), {"pnl.day_move"}) == []
    assert len(evaluate(ctx(loud), {"pnl.day_move"})) == 1


def test_day_move_says_nothing_without_history():
    """Never fall back to a constant — that is a guess dressed as a threshold."""
    assert evaluate(ctx(book(h(day=-90000.0))), {"pnl.day_move"}) == []


def test_a_raising_rule_does_not_cost_the_others_their_turn():
    def explode(_):
        raise RuntimeError("boom")

    original = R.REGISTRY["market.breakout"]
    R.REGISTRY["market.breakout"] = R.Rule("market.breakout", Family.MARKET, 0.4, 0, explode)
    try:
        c = ctx(
            book(h()),
            signal=sig(
                "market.volume_spike", payload={"rel_volume": 4.1, "price_change_pct": -3.2}
            ),
        )
        assert len(evaluate(c, {"market.breakout", "market.volume_spike"})) == 1
    finally:
        R.REGISTRY["market.breakout"] = original


# ---- the gate -------------------------------------------------------


def gate(**kw):
    b = kw.pop("book", None) or book(h("TITAN", value=300000.0), h("INFY", value=100000.0))
    return GateState(now=IST_NOON, book=b, **kw)


def trig(rule_id="market.volume_spike", entity="TITAN", magnitude=0.6, family=Family.MARKET):
    return Trigger(rule_id, entity, {}, magnitude, f"{rule_id}|{entity}", family)


def test_margin_stress_interrupts():
    d = score(trig("margin.band_up", "ACCOUNT", 0.85, Family.MARGIN), gate())
    assert d.route is Route.INTERRUPT


def test_a_volume_spike_alone_does_not_interrupt():
    """Market intelligence is information, not an emergency."""
    assert score(trig(), gate()).route in (Route.DIGEST, Route.SILENT)


def test_confluence_lifts_a_signal_that_alone_would_wait():
    """Unusual volume is a question; volume plus a filing is an answer."""
    news = trig("news.position_scoped", "TITAN", 0.7, Family.NEWS)
    alone = score(news, gate())
    together = score(news, gate(confluence={"TITAN": {"market.volume_spike"}}))
    assert together.score > alone.score
    assert together.route is Route.BATCH and alone.route is Route.DIGEST


def test_cooldown_silences_a_repeat():
    t = trig()
    st = gate(cooldown_until={t.dedupe: IST_NOON + timedelta(minutes=30)})
    assert score(t, st).route is Route.SILENT


def test_quiet_hours_defer_to_the_digest():
    st = gate()
    st.now = datetime(2026, 9, 15, 23, 45, tzinfo=IST)
    assert score(trig(), st).route is Route.DIGEST


def test_quiet_hours_wrap_midnight():
    st = gate(quiet_start=time(23, 30), quiet_end=time(8, 0))
    st.now = datetime(2026, 9, 15, 2, 0, tzinfo=IST)
    assert st.in_quiet_hours()


def test_a_p0_overrides_quiet_hours():
    """An ITM short at expiry does not wait for morning."""
    st = gate()
    st.now = datetime(2026, 9, 15, 23, 45, tzinfo=IST)
    d = score(trig("expiry.itm_short", "NIFTY25SEP25000CE", 1.0, Family.EXPIRY), st)
    assert d.route is Route.INTERRUPT and d.reason == "p0_override"


def test_a_p0_overrides_an_exhausted_budget():
    st = gate(sent_today=9)
    d = score(trig("expiry.itm_short", "NIFTY25SEP25000CE", 1.0, Family.EXPIRY), st)
    assert d.route is Route.INTERRUPT


def test_an_exhausted_budget_batches_everything_else():
    assert score(trig(), gate(sent_today=9)).route is Route.BATCH


def test_fatigue_makes_the_fifth_message_worth_less_than_the_first():
    assert score(trig(), gate(sent_today=4)).score < score(trig(), gate(sent_today=0)).score


def test_muting_a_rule_silences_it():
    assert score(trig(), gate(muted_rules=frozenset({"market.volume_spike"}))).route is Route.SILENT


def test_muting_an_entity_silences_every_rule_on_it():
    assert score(trig(), gate(muted_entities=frozenset({"TITAN"}))).route is Route.SILENT


def test_a_mute_never_suppresses_a_p0():
    st = gate(muted_entities=frozenset({"NIFTY25SEP25000CE"}))
    d = score(trig("expiry.itm_short", "NIFTY25SEP25000CE", 1.0, Family.EXPIRY), st)
    assert d.route is Route.INTERRUPT


def test_learned_indifference_never_silences_a_protective_family():
    """Someone ignoring margin warnings is the last person whose margin
    warnings should be suppressed. The floor is deliberate — not a bug.

    Note what is and is not claimed: indifference may still *downgrade* an
    interrupt to a batch, because the floor is 0.5 against a 0.7 default. What
    it can never do is stop the alert reaching the user at all.
    """
    ignored = gate(beliefs={"response.margin": 0.0, "response.market": 0.0})
    t = trig("margin.band_up", "ACCOUNT", 0.85, Family.MARGIN)
    d = score(t, ignored)
    assert d.sends, "a protective alert was suppressed by learned indifference"
    assert d.route not in (Route.SILENT, Route.DIGEST)


def test_an_attentive_user_gets_margin_stress_as_an_interrupt():
    """The other side of the floor: with normal responsiveness it interrupts."""
    t = trig("margin.band_up", "ACCOUNT", 0.85, Family.MARGIN)
    assert score(t, gate()).route is Route.INTERRUPT


def test_learned_indifference_does_quieten_market_chatter():
    t = trig()
    keen = score(t, gate(beliefs={"response.market": 1.4}))
    bored = score(t, gate(beliefs={"response.market": 0.3}))
    assert keen.score > bored.score


def test_every_stage_lands_in_the_trace():
    """Tuning needs to know which stage let something through, not just the sum."""
    names = [n for n, _ in score(trig(), gate()).trace]
    assert names == [
        "severity",
        "materiality",
        "timing",
        "irreversibility",
        "confluence",
        "responsiveness",
        "fatigue",
    ]


def test_expiry_is_worth_more_near_the_close():
    t = trig("expiry.itm_short", "NIFTY25SEP25000CE", 0.5, Family.EXPIRY)
    late = score(t, gate(minutes_to_close=10)).score
    early = score(t, gate(minutes_to_close=300)).score
    assert late > early


# ---- I1: numbers never originate outside a tool result --------------


def test_a_fabricated_figure_is_caught():
    assert not check("TITAN is up 7.4% today", {"pct": -3.2}).ok


def test_rendered_indian_grouping_traces_to_its_slot():
    assert check("₹18,42,600", {"value": 1842600.0}).ok


def test_a_rounded_render_traces_to_the_underlying_value():
    assert check("34.7% of your book", {"share": 0.347}).ok
    assert check("35% of your book", {"share": 0.347}).ok


def test_a_number_from_source_text_traces():
    """A headline is a tool result too — its figures are not fabricated."""
    assert check("Promoter sells 1.2% stake", {"headline": "Promoter sells 1.2% stake"}).ok


def test_reply_option_numbers_are_structural():
    assert check("Reply 1 to mute this name today", {}).ok


def test_guard_withholds_rather_than_sending_a_wrong_number():
    assert guard("up 7.4%", {"pct": -3.2}) == ""


def test_guard_falls_back_to_the_template():
    assert guard("up 7.4%", {"pct": -3.2}, fallback="down 3.2%") == "down 3.2%"


@pytest.mark.parametrize(
    "rule_id,payload",
    [
        (
            "market.volume_spike",
            {
                "symbol": "TITAN",
                "rel_volume": 4.1,
                "price_change_pct": -3.2,
                "qty": 40,
                "notional": 123600.0,
                "share_of_book": 0.347,
                "unrealised_pct": 18.0,
            },
        ),
        (
            "expiry.itm_short",
            {
                "symbol": "N25000CE",
                "days_to_expiry": 0,
                "strike": 25000.0,
                "spot": 25180.0,
                "net_qty": -75,
                "unrealised": -13500.0,
                "moneyness_pct": 0.72,
            },
        ),
        (
            "margin.band_up",
            {
                "band": "tight",
                "from_band": "engaged",
                "utilisation": 0.72,
                "used": 840000.0,
                "headroom": 326000.0,
                "span": 610000.0,
                "exposure": 230000.0,
            },
        ),
        (
            "greeks.delta_drift",
            {
                "symbol": "N25000CE",
                "delta_now": 0.44,
                "delta_entry": 0.19,
                "iv_now": 14.2,
                "net_qty": -75,
                "unrealised": -13500.0,
            },
        ),
        (
            "pnl.day_move",
            {
                "day_pnl": -8420.0,
                "sigma": 2.3,
                "total_value": 356550.0,
                "daily_sigma_rupees": 3600.0,
            },
        ),
    ],
)
def test_every_composed_alert_survives_the_numeric_guard(rule_id, payload):
    """The ship-blocker. A template with a hand-typed constant is the same
    defect as a hallucinated one, with a longer fuse."""
    alert = compose(Trigger(rule_id, payload.get("symbol", "ACCOUNT"), payload, 0.6, "d"))
    assert alert.body, f"{rule_id} produced nothing — a number failed to trace"


def test_every_alert_has_all_three_parts():
    """STATE / SO WHAT / ACTION. If the middle cannot be written, the alert
    should not exist."""
    a = compose(
        Trigger(
            "market.volume_spike",
            "TITAN",
            {
                "symbol": "TITAN",
                "rel_volume": 4.1,
                "price_change_pct": -3.2,
                "qty": 40,
                "notional": 123600.0,
                "share_of_book": 0.347,
                "unrealised_pct": 18.0,
            },
            0.6,
            "d",
        )
    )
    assert "4.1×" in a.body  # state
    assert "of your book" in a.body  # so what
    assert "Reply" in a.body  # action


def test_an_unknown_rule_composes_to_nothing_rather_than_guessing():
    assert not compose(Trigger("nope.unknown", "X", {}, 0.5, "d")).body


# ---- calendar -------------------------------------------------------


def test_session_state_distinguishes_holiday_from_weekend():
    """'Shut for Diwali' and 'it's Sunday' are different messages."""
    assert session_state("CASH", datetime(2026, 9, 20, 11, 0, tzinfo=IST)) is SessionState.CLOSED
    holiday = sorted(__import__("app.market.calendar", fromlist=["holidays"]).holidays())[0]
    at = datetime.combine(holiday, time(11, 0), tzinfo=IST)
    assert session_state("CASH", at) is SessionState.HOLIDAY


def test_session_state_open_and_post_close():
    assert session_state("CASH", IST_NOON) is SessionState.OPEN
    assert (
        session_state("CASH", datetime(2026, 9, 15, 16, 0, tzinfo=IST)) is SessionState.POST_CLOSE
    )


def test_next_open_skips_the_weekend():
    friday_evening = datetime(2026, 9, 18, 18, 0, tzinfo=IST)
    assert next_open("CASH", friday_evening).date().weekday() == 0


def test_minutes_to_close_is_none_when_shut():
    assert minutes_to_close("CASH", datetime(2026, 9, 15, 17, 0, tzinfo=IST)) is None
    assert minutes_to_close("CASH", datetime(2026, 9, 15, 15, 0, tzinfo=IST)) == 30


def test_holidays_reload_when_the_file_changes(tmp_path, monkeypatch):
    """A watcher running for months must see an edited holiday file."""
    f = tmp_path / "holidays.json"
    f.write_text('{"2026": []}')
    monkeypatch.setattr(cal, "HOLIDAYS_PATH", f)
    monkeypatch.setattr(cal, "_HOLIDAYS", None)
    assert cal.holidays() == set()
    f.write_text('{"2026": ["2026-11-08"]}')
    assert date(2026, 11, 8) in cal.holidays()


def test_refuses_to_run_against_an_uncovered_year(tmp_path, monkeypatch):
    """A missing file reads as 'every weekday is a trading day' — silently."""
    f = tmp_path / "holidays.json"
    f.write_text('{"2026": []}')
    monkeypatch.setattr(cal, "HOLIDAYS_PATH", f)
    with pytest.raises(cal.HolidaysMissing):
        cal.assert_holidays_cover(2031)


# ---- shadow mode ----------------------------------------------------


def test_shadow_log_round_trips(tmp_path):
    log = ShadowLog(tmp_path)
    d = score(trig(), gate())
    log.record(
        trig(),
        d,
        compose(
            Trigger(
                "market.volume_spike",
                "TITAN",
                {
                    "symbol": "TITAN",
                    "rel_volume": 4.1,
                    "price_change_pct": -3.2,
                    "qty": 40,
                    "notional": 123600.0,
                    "share_of_book": 0.347,
                    "unrealised_pct": 18.0,
                },
                0.6,
                "d",
            )
        ),
    )
    entries = log.entries(datetime.now(IST).date())
    assert len(entries) == 1 and entries[0].entity == "TITAN"


def test_report_names_the_calibration_verdict():
    quiet = render_report(date(2026, 9, 15), [])
    assert "too quiet" in quiet

    loud = render_report(
        date(2026, 9, 15),
        [ShadowEntry(IST_NOON, "r", "X", Route.INTERRUPT, 0.8, "", "body") for _ in range(9)],
    )
    assert "too loud" in loud


def test_report_shows_suppressed_rows_so_silence_is_visible():
    out = render_report(
        date(2026, 9, 15),
        [ShadowEntry(IST_NOON, "market.volume_spike", "TINY", Route.SILENT, 0.09, "below_bar", "")],
    )
    assert "SILENT" in out and "below_bar" in out


def test_an_unchanged_margin_band_does_not_re_alert_all_day():
    """Cooldown must agree with dedupe granularity. The dedupe key is band+day,
    so a cooldown shorter than a session lets an unchanged band repeat with
    identical text every time it expires."""
    st = gate()
    t = trig("margin.band_up", "ACCOUNT", 0.85, Family.MARGIN)
    first = score(t, st)
    st.commit(t, first)
    st.now = IST_NOON + timedelta(hours=2)
    assert score(t, st).route is Route.SILENT


def test_a_worsening_band_is_never_blocked_by_that_cooldown():
    """Escalation builds a different dedupe key, so it goes straight through."""
    st = gate()
    tight = Trigger("margin.band_up", "ACCOUNT", {}, 0.6, "margin|tight|d", Family.MARGIN)
    st.commit(tight, score(tight, st))
    st.now = IST_NOON + timedelta(minutes=5)
    stressed = Trigger("margin.band_up", "ACCOUNT", {}, 0.85, "margin|stressed|d", Family.MARGIN)
    assert score(stressed, st).sends


def test_commit_spends_the_budget_only_on_an_interrupt():
    st = gate()
    t = trig()
    st.commit(t, score(t, st))  # a digest-routed trigger
    assert st.sent_today == 0
    p0 = trig("expiry.itm_short", "N25000CE", 1.0, Family.EXPIRY)
    st.commit(p0, score(p0, st))
    assert st.sent_today == 1


def test_account_events_do_not_amplify_each_other_through_confluence():
    """Margin, day P&L and an expiry all land on ACCOUNT; treating that as
    corroboration would let unrelated events stack into an interrupt."""
    st = gate(confluence={"ACCOUNT": {"pnl.day_move", "margin.band_up"}})
    assert st.confluence_factor("ACCOUNT", "expiry.itm_short") == 1.0
