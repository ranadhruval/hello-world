"""Resolver behaviour (spec §5.4).

Assertions run against the real 136k-row instrument master where the point is
that real data has sharp edges, and against a tiny hand-built index where the
point is a specific rule.
"""

import time
from datetime import date

import pytest

from app.tools.aliases import normalise
from app.tools.instruments import (
    InstrumentIndex,
    parse_derivative,
    parse_row,
    trigram_similarity,
)
from tests.conftest import MASTER_DATE

# ---- CSV parsing ---------------------------------------------------


def test_future_strike_sentinel_becomes_none():
    """FUT rows carry -0.01 or 0, not null."""
    for sentinel in ("-0.01", "0"):
        row = {
            "exchange": "NSE",
            "segment": "FNO",
            "trading_symbol": "NIFTY26SEPFUT",
            "exchange_token": "68407",
            "instrument_type": "FUT",
            "strike_price": sentinel,
            "expiry_date": "2026-09-29",
            "lot_size": "65",
            "tick_size": "0.05",
        }
        assert parse_row(row).strike is None


def test_blank_expiry_becomes_none():
    row = {
        "exchange": "NSE",
        "segment": "CASH",
        "trading_symbol": "RELIANCE",
        "exchange_token": "2885",
        "instrument_type": "EQ",
        "expiry_date": "",
        "strike_price": "",
        "lot_size": "1",
        "tick_size": "0.05",
    }
    parsed = parse_row(row)
    assert parsed.expiry is None and parsed.strike is None


def test_test_instruments_are_dropped():
    row = {
        "exchange": "NSE",
        "segment": "FNO",
        "trading_symbol": "031NSETEST36DECFUT",
        "exchange_token": "36691",
        "instrument_type": "FUT",
    }
    assert parse_row(row) is None


def test_blank_symbol_is_dropped():
    assert parse_row({"exchange": "BSE", "segment": "CASH", "trading_symbol": ""}) is None


# ---- normalisation -------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Nifty 25000 CE", "nifty 25000 ce"),
        ("gold 1 lakh call", "gold 100000 call"),
        ("gold 1.05 lakh pe", "gold 105000 pe"),
        ("nifty 25k ce", "nifty 25000 ce"),
        ("  BANK   NIFTY  ", "bank nifty"),
    ],
)
def test_normalise(raw, expected):
    assert normalise(raw) == expected


# ---- derivative parsing --------------------------------------------


def test_parse_option():
    q = parse_derivative("nifty 25000 ce")
    assert (q.underlying, q.kind, q.strike) == ("NIFTY", "CE", 25000.0)


def test_parse_option_with_month_hint():
    q = parse_derivative("banknifty nov 54000 pe")
    assert (q.underlying, q.kind, q.strike, q.month) == ("BANKNIFTY", "PE", 54000.0, 11)


def test_parse_future_needs_no_strike():
    q = parse_derivative("nifty fut")
    assert (q.underlying, q.kind, q.strike) == ("NIFTY", "FUT", None)


def test_option_without_strike_is_not_a_derivative_query():
    assert parse_derivative("nifty ce") is None


def test_plain_symbol_is_not_a_derivative_query():
    assert parse_derivative("reliance") is None


# ---- trigram -------------------------------------------------------


def test_trigram_identical_is_one():
    assert trigram_similarity("reliance", "reliance") == 1.0


def test_trigram_unrelated_is_low():
    assert trigram_similarity("reliance", "zzzzzz") < 0.1


def test_trigram_handles_empty():
    assert trigram_similarity("", "reliance") == 0.0


# ---- resolution against a tiny index -------------------------------


def test_dual_listing_collapses_to_nse(tiny_index):
    """RELIANCE on NSE and BSE is one instrument quoted twice, not ambiguity."""
    r = tiny_index.resolve("reliance", today=MASTER_DATE)
    assert r.ok
    assert r.instrument.exchange == "NSE"


def test_nearest_expiry_wins(tiny_index):
    r = tiny_index.resolve("nifty 25000 ce", today=MASTER_DATE)
    assert r.ok
    assert r.instrument.expiry == date(2026, 9, 15)


def test_expired_contracts_are_excluded(tiny_index):
    r = tiny_index.resolve("nifty 25000 ce", today=date(2026, 9, 20))
    assert r.ok
    assert r.instrument.expiry == date(2026, 9, 22)


def test_lot_size_comes_from_the_master(tiny_index):
    """NIFTY is 65, not the 75 the spec was written against."""
    assert tiny_index.resolve("nifty 25000 ce", today=MASTER_DATE).instrument.lot_size == 65


def test_unknown_query_resolves_to_nothing(tiny_index):
    r = tiny_index.resolve("zzzznotathing", today=MASTER_DATE)
    assert not r.ok and not r.ambiguous and r.instrument is None


def test_empty_query(tiny_index):
    assert tiny_index.resolve("", today=MASTER_DATE).instrument is None


def test_missing_strike_offers_neighbours(tiny_index):
    r = tiny_index.resolve("nifty 25001 ce", today=MASTER_DATE)
    assert r.ambiguous
    assert r.candidates
    assert all(c.instrument.strike == 25000.0 for c in r.candidates)


# ---- resolution against the real master ----------------------------


def test_real_master_loads(real_index):
    assert len(real_index) > 100_000


@pytest.mark.parametrize(
    "query,expected",
    [
        ("reliance", "RELIANCE"),
        ("ril", "RELIANCE"),
        ("infosys", "INFY"),
        ("sbi", "SBIN"),
        ("nifty", "NIFTY"),
        ("banknifty", "BANKNIFTY"),
        ("bn 54000 pe", "BANKNIFTY26SEP54000PE"),
        ("nifty 25000 ce", "NIFTY2691525000CE"),
    ],
)
def test_real_resolution(real_index, query, expected):
    r = real_index.resolve(query, today=MASTER_DATE)
    assert r.ok, f"{query} did not resolve ({'ambiguous' if r.ambiguous else 'no match'})"
    assert r.instrument.trading_symbol == expected


def test_commodity_alias_never_returns_an_equity(real_index):
    """NSE lists an equity called SILVER. "chandi" means the metal, always."""
    r = real_index.resolve("chandi", today=MASTER_DATE)
    for c in r.candidates:
        assert c.instrument.segment == "COMMODITY"
        assert c.instrument.instrument_type == "FUT"


def test_gold_is_ambiguous_across_venues(real_index):
    """GOLD trades on both MCX and NSE — a genuine ambiguity, so never guess."""
    r = real_index.resolve("gold", today=MASTER_DATE)
    assert r.ambiguous
    assert {c.instrument.exchange for c in r.candidates} == {"MCX", "NSE"}


def test_user_venue_breaks_the_gold_tie(real_index):
    r = real_index.resolve("gold", user_venues={("MCX", "GOLD")}, today=MASTER_DATE)
    assert r.ok
    assert r.instrument.exchange == "MCX"


def test_user_book_boost_is_symmetric(real_index):
    nse = real_index.resolve("gold", user_venues={("NSE", "GOLD")}, today=MASTER_DATE)
    assert nse.ok and nse.instrument.exchange == "NSE"


def test_resolution_is_fast_enough_for_the_fast_path(real_index):
    """The fast path budgets p50 < 900ms for the whole turn."""
    start = time.perf_counter()
    for q in ["reliance", "nifty 25000 ce", "gold", "xyzabc", "tata motors"]:
        real_index.resolve(q, today=MASTER_DATE)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 200, f"5 resolutions took {elapsed_ms:.0f}ms"


def test_index_built_from_empty_list():
    idx = InstrumentIndex([])
    assert len(idx) == 0
    assert idx.resolve("anything").instrument is None
