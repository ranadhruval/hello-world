#!/usr/bin/env python3
"""Reconcile computed P&L against the Groww app (spec §5.3 gotcha 5).

    Open the Groww app, open your bot, compare portfolio value to the rupee.
    If they differ, your basis logic is wrong, not theirs. Do this before
    writing a single alert.

This is the check that gate stands on, and it also settles Appendix B item 4:
Groww's positions payload exposes credit_price and debit_price, and the docs
do not say whether those are per-unit averages or whole-leg notionals. Getting
it backwards scales every F&O P&L by the lot size and produces numbers that
look plausible and are wrong.

So the script computes both conventions side by side and asks you which column
matches the app. Pin the winner in app/tools/pnl.py:DEFAULT_BASIS.

Usage:
    TOTP_TOKEN=... TOTP_SECRET=... python scripts/reconcile.py

Read-only. It places no orders and writes nothing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pyotp  # noqa: E402
from growwapi import GrowwAPI  # noqa: E402

from app.auth.broker import _access_token  # noqa: E402
from app.render.templates import inr, signed  # noqa: E402
from app.tools.instruments import InstrumentIndex, ensure_csv  # noqa: E402
from app.tools.pnl import (  # noqa: E402
    DEFAULT_BASIS,
    Basis,
    holding_pnl,
    margin_utilisation,
    position_pnl,
)
from app.tools.types import Holding, MarginState, Position  # noqa: E402

CSV = Path(__file__).resolve().parents[1] / "data" / "instruments.csv"
RULE = "─" * 68


def connect() -> GrowwAPI:
    token, secret = os.environ.get("TOTP_TOKEN"), os.environ.get("TOTP_SECRET")
    if not token or not secret:
        sys.exit("Set TOTP_TOKEN and TOTP_SECRET (from groww.in/trade-api/api-keys)")
    access = GrowwAPI.get_access_token(api_key=token, totp=pyotp.TOTP(secret).now())
    return GrowwAPI(_access_token(access))


def rows(payload, *keys) -> list[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in keys:
            if isinstance(payload.get(k), list):
                return payload[k]
        for v in payload.values():
            if isinstance(v, list) and all(isinstance(r, dict) for r in v):
                return v
    return []


def reconcile_holdings(groww: GrowwAPI, index: InstrumentIndex) -> float:
    holdings = [Holding.from_api(d) for d in rows(groww.get_holdings_for_user(timeout=10), "holdings")]
    if not holdings:
        print("No holdings.\n")
        return 0.0

    # Holdings carry no exchange, so the LTP key is rebuilt from the master.
    keys: dict[str, str] = {}
    unresolved: list[str] = []
    for h in holdings:
        inst = index.by_key.get(("NSE", "CASH", h.trading_symbol)) or index.by_key.get(
            ("BSE", "CASH", h.trading_symbol)
        )
        if inst is None:
            unresolved.append(h.trading_symbol)
        else:
            keys[h.trading_symbol] = inst.ltp_key

    ltps: dict[str, float] = {}
    batch = list(keys.values())
    for i in range(0, len(batch), 50):  # get_ltp caps at 50 per call
        ltps.update(groww.get_ltp(exchange_trading_symbols=tuple(batch[i : i + 50]), segment="CASH"))

    print("HOLDINGS")
    print(RULE)
    print(f"{'SYMBOL':<14}{'QTY':>8}{'AVG':>12}{'LTP':>12}{'VALUE':>16}{'P&L':>14}")
    print(RULE)

    total_value = total_cost = 0.0
    for h in holdings:
        key = keys.get(h.trading_symbol)
        ltp = ltps.get(key) if key else None
        if ltp is None:
            print(f"{h.trading_symbol:<14}{h.quantity:>8.0f}{'— no quote —':>54}")
            continue
        r = holding_pnl(h, float(ltp), 0.0)
        total_value += r.current_value
        total_cost += r.cost
        flag = f"  ({r.pledged:.0f} pledged)" if r.pledged else ""
        print(
            f"{r.symbol:<14}{r.qty:>8.0f}{r.avg_price:>12.2f}{r.ltp:>12.2f}"
            f"{inr(r.current_value):>16}{signed(r.unrealised):>14}{flag}"
        )

    print(RULE)
    print(f"{'TOTAL':<14}{'':>8}{'':>12}{'':>12}{inr(total_value):>16}"
          f"{signed(total_value - total_cost):>14}")
    if unresolved:
        print(f"\n  ! not in the instrument master: {', '.join(unresolved)}")
    print(f"\n  >> Compare {inr(total_value)} against the app's portfolio value.")
    print("     A mismatch here means the basis logic is wrong, not Groww.\n")
    return total_value


def reconcile_positions(groww: GrowwAPI) -> None:
    """The heart of it: both basis conventions, side by side."""
    # Dedupe across segment calls. If Groww ignores the segment filter on any
    # of them the same leg comes back more than once, and concatenating would
    # silently double the totals.
    seen: dict[tuple[str, str], Position] = {}
    for segment in ("FNO", "CASH", "COMMODITY"):
        try:
            payload = groww.get_positions_for_user(segment=segment, timeout=10)
        except Exception as exc:  # MCX portfolio parity is Appendix B item 1
            print(f"  ! {segment} positions unavailable: {type(exc).__name__}: {exc}")
            continue
        for d in rows(payload, "positions"):
            p = Position.from_api(d)
            seen.setdefault((p.trading_symbol, p.segment or segment), p)

    positions = list(seen.values())
    live = [p for p in positions if (p.credit_quantity - p.debit_quantity) != 0]
    if not live:
        print("No open positions.\n")
        return

    ltps: dict[str, float] = {}
    for p in live:
        key = f"{p.exchange or 'NSE'}_{p.trading_symbol}"
        try:
            ltps[key] = float(
                groww.get_ltp(
                    exchange_trading_symbols=(key,), segment=p.segment or "FNO"
                ).get(key, 0.0)
            )
        except Exception:
            ltps[key] = 0.0

    print("POSITIONS — basis convention comparison")
    print(RULE)
    print(f"{'SYMBOL':<24}{'NET':>7}{'LTP':>11}{'AVERAGE':>13}{'NOTIONAL':>13}")
    print(RULE)

    avg_total = notional_total = 0.0
    for p in live:
        key = f"{p.exchange or 'NSE'}_{p.trading_symbol}"
        ltp = ltps.get(key, 0.0)
        a = position_pnl(p, ltp, Basis.AVERAGE)
        n = position_pnl(p, ltp, Basis.NOTIONAL)
        avg_total += a.total
        notional_total += n.total
        print(
            f"{p.trading_symbol:<24}{a.net_qty:>7.0f}{ltp:>11.2f}"
            f"{signed(a.total):>13}{signed(n.total):>13}"
        )

    print(RULE)
    print(f"{'TOTAL':<24}{'':>7}{'':>11}{signed(avg_total):>13}{signed(notional_total):>13}")
    print()
    report_basis(live, ltps)


def report_basis(live: list[Position], ltps: dict[str, float]) -> None:
    """Decide whether credit_price/debit_price are per-unit or whole-leg.

    The two are separated by a factor of the quantity, so the raw field sits
    either near the LTP (per-unit) or near qty x LTP (notional). Reading that
    off the magnitudes is more reliable than asking someone to compare two
    columns of P&L against an app, and it is the same evidence either way.

    Positions with quantity 1 cannot discriminate and are skipped.
    """
    votes: list[tuple[str, str]] = []
    for p in live:
        net = p.credit_quantity - p.debit_quantity
        raw = p.credit_price if net > 0 else p.debit_price
        qty = abs(net)
        ltp = ltps.get(f"{p.exchange or 'NSE'}_{p.trading_symbol}", 0.0)
        if not (raw and ltp and qty) or qty == 1:
            continue
        # Whichever hypothesis puts the per-unit basis closer to the LTP wins.
        per_unit_err = abs(raw - ltp) / ltp
        notional_err = abs(raw / qty - ltp) / ltp
        votes.append((p.trading_symbol, "AVERAGE" if per_unit_err <= notional_err else "NOTIONAL"))

    if not votes:
        print("  >> Cannot tell which basis convention applies: no position with a\n"
              "     quantity above 1 and a live price. Both columns above are\n"
              "     identical for quantity-1 legs. DEFAULT_BASIS stays unverified\n"
              "     until you hold a multi-unit F&O position.\n")
        return

    verdicts = {v for _, v in votes}
    if len(verdicts) > 1:
        print("  >> Positions disagree about the basis convention:")
        for symbol, verdict in votes:
            print(f"       {symbol:<24} {verdict}")
        print("     That should not happen. Send me this output.\n")
        return

    verdict = verdicts.pop()
    current = DEFAULT_BASIS.name
    print(f"  >> Basis convention is {verdict} (agreed across {len(votes)} position(s)).")
    if verdict == current:
        print(f"     app/tools/pnl.py:DEFAULT_BASIS is already {current}. Nothing to change.\n")
    else:
        print(f"     DEFAULT_BASIS is currently {current} — change it to Basis.{verdict}.")
        print("     Every F&O number reported so far was wrong by a factor of the\n"
              "     quantity, so re-check anything you relied on.\n")


def reconcile_margin(groww: GrowwAPI) -> None:
    m = MarginState.from_api(groww.get_available_margin_details(timeout=10))
    u = margin_utilisation(m)
    print("MARGIN")
    print(RULE)
    print(f"  Utilisation      {u.utilisation * 100:6.2f}%")
    print(f"  Used             {inr(u.used):>16}")
    print(f"  Headroom         {inr(u.headroom):>16}")
    print(f"  Clear cash       {inr(m.clear_cash):>16}")
    print(f"  Collateral       {inr(m.collateral_available):>16}")
    print(f"  SPAN             {inr(u.span):>16}")
    print(f"  Exposure         {inr(u.exposure):>16}")
    print("""
  >> Compare utilisation against the app before building any margin alert.
     Phase 2's bands (comfortable/engaged/tight/stressed/critical) are
     meaningless until this number matches.
""")


def verify_day_change(groww: GrowwAPI, index: InstrumentIndex) -> None:
    """Is ohlc.close the previous close?

    If it is, day change can be derived from one batched get_ohlc call instead
    of one get_quote per holding — 9 Live Data calls down to 2 on the portfolio
    path. Worth one check here rather than guessing in dispatch.
    """
    probes = [s for s in ("RELIANCE", "INFY", "HDFCBANK") if index.by_key.get(("NSE", "CASH", s))]
    if not probes:
        return

    print("DAY-CHANGE DERIVATION")
    print(RULE)
    print(f"{'SYMBOL':<14}{'LTP':>11}{'OHLC.CLOSE':>13}{'LTP-CLOSE':>12}{'day_change':>12}{'':>8}")
    print(RULE)
    for symbol in probes:
        try:
            q = groww.get_quote(trading_symbol=symbol, exchange="NSE", segment="CASH", timeout=10)
        except Exception as exc:
            print(f"{symbol:<14}  {type(exc).__name__}: {exc}")
            continue
        ltp = float(q.get("last_price") or 0.0)
        close = float((q.get("ohlc") or {}).get("close") or 0.0)
        stated = float(q.get("day_change") or 0.0)
        derived = ltp - close
        match = "match" if abs(derived - stated) < 0.01 else "DIFFERS"
        print(f"{symbol:<14}{ltp:>11.2f}{close:>13.2f}{derived:>12.2f}{stated:>12.2f}{match:>8}")

    print("""
  >> If every row says "match", ohlc.close is the previous close and the
     portfolio path can batch day change via get_ohlc. If any row differs,
     keep the per-symbol get_quote.
""")


def main() -> int:
    print("\nLoading instrument master…")
    index = InstrumentIndex.from_csv(ensure_csv(CSV))
    print(f"  {len(index)} instruments\n")

    groww = connect()
    print()

    reconcile_holdings(groww, index)
    reconcile_positions(groww)
    reconcile_margin(groww)
    verify_day_change(groww, index)

    print(RULE)
    print("Reconciliation is the Phase 1 gate. Do not build alerts on numbers")
    print("that do not match the app to the rupee.")
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
