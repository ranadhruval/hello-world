#!/usr/bin/env python3
"""Evaluation harness (spec §9.2).

Built in week 2, not week 6 — it is what lets one person move fast without
regressing.

Three layers of scoring:
  1. Intent accuracy      exact match, target >95%
  2. Numeric exactness    rendered figure == figure computed from a frozen
                          fixture. Must be 100%. Any failure is a ship-blocker.
  3. Prose quality        LLM-as-judge, 5-point rubric, target mean >4.2
                          (not wired until the LLM path lands)

Layer 2 is the one that matters. A finance assistant that is occasionally
wrong about a number is worse than no assistant.

Usage:
    python eval/run.py [--golden eval/golden.jsonl] [--verbose]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.render.templates import inr, portfolio_summary  # noqa: E402
from app.router.fastpath import classify  # noqa: E402
from app.tools.instruments import InstrumentIndex  # noqa: E402
from app.tools.pnl import holding_pnl, margin_utilisation, portfolio_pnl, position_pnl  # noqa: E402
from app.tools.types import FnoMargin, Holding, MarginState, Position  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "data" / "instruments.csv"
GOLDEN = ROOT / "eval" / "golden.jsonl"

# The date the bundled instrument master was pulled, so expiry-sensitive
# expectations stay stable.
MASTER_DATE = date(2026, 9, 13)

INTENT_TARGET = 0.95
PROSE_TARGET = 4.2
GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


@dataclass
class Layer:
    name: str
    passed: int = 0
    failed: int = 0
    pending: int = 0  # checks that cannot run yet — never counted as passes
    failures: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.passed + self.failed

    @property
    def score(self) -> float:
        return self.passed / self.total if self.total else 1.0

    def check(self, ok: bool, detail: str) -> None:
        if ok:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append(detail)


def load_golden(path: Path) -> list[dict]:
    cases = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as exc:
            sys.exit(f"{path}:{n}: {exc}")
    return cases


# ---- layer 1: intent accuracy --------------------------------------


def score_intents(cases: list[dict], index: InstrumentIndex) -> Layer:
    layer = Layer("intent accuracy")

    def resolves(text: str) -> bool:
        return index.resolve(text, today=MASTER_DATE).ok

    for case in cases:
        if "intent" not in case:
            continue
        route = classify(case["q"], resolves_to_instrument=resolves)
        want = case["intent"]
        got = str(route.intent)
        layer.check(got == want, f'"{case["q"]}" -> {got}, expected {want}')

        if "path" in case:
            layer.check(
                str(route.path) == case["path"],
                f'"{case["q"]}" path {route.path}, expected {case["path"]}',
            )
    return layer


# ---- layer 1b: instrument resolution -------------------------------


def score_resolution(cases: list[dict], index: InstrumentIndex) -> Layer:
    layer = Layer("instrument resolution")
    for case in cases:
        venues = {tuple(v) for v in case.get("user_venues", [])}
        res = index.resolve(case["q"], user_venues=venues, today=MASTER_DATE)

        if case.get("expect_ambiguous"):
            layer.check(res.ambiguous, f'"{case["q"]}" should disambiguate, got {res.instrument}')
            continue
        if case.get("expect_unresolved"):
            layer.check(
                res.instrument is None and not res.ambiguous,
                f'"{case["q"]}" should not resolve, got {res.instrument}',
            )
            continue
        if "resolve_to" not in case:
            continue

        if not res.ok:
            reason = "ambiguous" if res.ambiguous else "no match"
            layer.check(False, f'"{case["q"]}" -> {reason}')
            continue

        for attr, want in case["resolve_to"].items():
            got = getattr(res.instrument, attr, None)
            layer.check(got == want, f'"{case["q"]}".{attr} = {got!r}, expected {want!r}')
    return layer


# ---- layer 2: numeric exactness (ship-blocker) ---------------------

FIXTURE_HOLDINGS = [
    Holding(trading_symbol="KAYNES", quantity=40, average_price=5_000.0),
    Holding(trading_symbol="HINDZINC", quantity=500, average_price=460.0, pledge_quantity=100),
    Holding(trading_symbol="MCX", quantity=120, average_price=5_500.0, t1_quantity=20),
]
FIXTURE_LTPS = {"KAYNES": 5_460.0, "HINDZINC": 452.0, "MCX": 6_060.0}
FIXTURE_DAY_CHANGES = {"KAYNES": 460.0, "HINDZINC": -4.2, "MCX": 65.67}


def score_numbers() -> Layer:
    """Assert rendered output against figures computed directly from fixtures.

    Every expectation here is an independent arithmetic restatement, not a
    call back into the same function under test.
    """
    layer = Layer("numeric exactness")

    # Holding value and P&L
    r = holding_pnl(FIXTURE_HOLDINGS[0], 5_460.0, 460.0)
    layer.check(r.current_value == 40 * 5_460.0, f"KAYNES value {r.current_value}")
    layer.check(r.cost == 40 * 5_000.0, f"KAYNES cost {r.cost}")
    layer.check(r.unrealised == 40 * 460.0, f"KAYNES unrealised {r.unrealised}")
    layer.check(r.day_change == 40 * 460.0, f"KAYNES day change {r.day_change}")
    layer.check(abs(r.unrealised_pct - 9.2) < 1e-9, f"KAYNES pct {r.unrealised_pct}")

    # A loss must stay negative all the way to the rendered string
    loss = holding_pnl(FIXTURE_HOLDINGS[1], 452.0, -4.2)
    layer.check(loss.unrealised == 500 * (452.0 - 460.0), f"HINDZINC unrealised {loss.unrealised}")
    layer.check(loss.unrealised < 0, "HINDZINC loss must be negative")
    layer.check("−₹4,000" == inr(loss.unrealised), f"HINDZINC rendered {inr(loss.unrealised)}")

    # Portfolio aggregate
    p = portfolio_pnl(FIXTURE_HOLDINGS, FIXTURE_LTPS, FIXTURE_DAY_CHANGES)
    expected_value = 40 * 5_460.0 + 500 * 452.0 + 120 * 6_060.0
    expected_cost = 40 * 5_000.0 + 500 * 460.0 + 120 * 5_500.0
    layer.check(p.current_value == expected_value, f"portfolio value {p.current_value}")
    layer.check(p.cost == expected_cost, f"portfolio cost {p.cost}")
    layer.check(
        p.unrealised == expected_value - expected_cost, f"portfolio unrealised {p.unrealised}"
    )

    # Every figure in the rendered message must appear exactly (invariant I1)
    rendered = portfolio_summary(p)
    for figure in (inr(p.current_value), inr(p.day_change).lstrip("+")):
        layer.check(figure in rendered, f"{figure} missing from rendered summary")

    # Indian digit grouping
    for value, want in [
        (1_842_600, "₹18,42,600"),
        (124_580, "₹1,24,580"),
        (1_000, "₹1,000"),
        (999, "₹999"),
        (1_00_00_000, "₹1,00,00,000"),
    ]:
        layer.check(inr(value) == want, f"inr({value}) = {inr(value)}, expected {want}")

    # Position P&L, both directions
    long_leg = Position(
        trading_symbol="NIFTY26SEPFUT", segment="FNO",
        credit_quantity=130, credit_price=25_000.0,
    )
    lr = position_pnl(long_leg, 25_100.0)
    layer.check(lr.unrealised == 130 * 100.0, f"long unrealised {lr.unrealised}")

    short_leg = Position(
        trading_symbol="NIFTY2691525000CE", segment="FNO",
        debit_quantity=65, debit_price=180.0, realised_pnl=2_000.0,
    )
    sr = position_pnl(short_leg, 120.0)
    layer.check(sr.net_qty == -65, f"short net qty {sr.net_qty}")
    layer.check(sr.unrealised == 65 * 60.0, f"short unrealised {sr.unrealised}")
    layer.check(sr.total == 65 * 60.0 + 2_000.0, f"short total {sr.total}")

    # F&O quantities are units, not lots — no lot-size multiply
    layer.check(
        position_pnl(Position(trading_symbol="X", segment="FNO",
                              credit_quantity=65, credit_price=100.0), 101.0).unrealised == 65.0,
        "F&O P&L must not re-multiply by lot size",
    )

    # Margin utilisation
    m = MarginState(
        clear_cash=100_000.0, collateral_available=300_000.0, net_margin_used=600_000.0,
        fno=FnoMargin(span_margin_used=412_000.0, exposure_margin_used=88_000.0),
    )
    u = margin_utilisation(m)
    layer.check(u.utilisation == 600_000.0 / 1_000_000.0, f"utilisation {u.utilisation}")
    layer.check(u.headroom == 400_000.0, f"headroom {u.headroom}")

    # A missing quote is named, never zeroed
    gap = portfolio_pnl(
        FIXTURE_HOLDINGS + [Holding(trading_symbol="GHOST", quantity=10, average_price=1.0)],
        FIXTURE_LTPS, FIXTURE_DAY_CHANGES,
    )
    layer.check(gap.missing_quotes == ["GHOST"], f"missing quotes {gap.missing_quotes}")
    layer.check(gap.current_value == expected_value, "a missing quote must not change the total")

    return layer


# ---- layer 3: prose quality ----------------------------------------


def score_prose(cases: list[dict]) -> Layer:
    """Forbidden-phrase guardrails over rendered output (spec §6.5).

    Only the static template strings can be checked until the agent loop
    exists; generated prose is scored by LLM-as-judge once it does. Cases
    whose answer is not yet generated are counted as pending, not as passes —
    a green line for a check that did not run is worse than a red one.
    """
    import re

    from app.render import templates

    layer = Layer("prose guardrails")
    static = "\n".join(
        v for v in vars(templates).values() if isinstance(v, str) and not v.startswith("_")
    )
    for case in cases:
        pattern = case.get("forbid_regex")
        if not pattern:
            continue
        hit = re.search(pattern, static, re.I)
        layer.check(hit is None, f"template text matches forbidden {pattern!r}: {hit}")
        layer.pending += 1
    return layer


# ---- reporting -----------------------------------------------------


def report(layers: list[Layer], verbose: bool) -> int:
    print()
    worst = 0
    for layer in layers:
        if layer.total == 0:
            print(f"  {DIM}{layer.name:<24} no cases{RESET}")
            continue

        blocking = layer.name == "numeric exactness"
        target = 1.0 if blocking else INTENT_TARGET
        ok = layer.score >= target
        colour = GREEN if ok else RED
        flag = "" if ok else ("  SHIP-BLOCKER" if blocking else "  below target")
        pending = f"  {DIM}{layer.pending} pending LLM path{RESET}" if layer.pending else ""
        print(
            f"  {colour}{layer.name:<24} {layer.score * 100:6.2f}%  "
            f"({layer.passed}/{layer.total}){RESET}{flag}{pending}"
        )
        if not ok:
            worst = 1
        if layer.failures and (verbose or not ok):
            for detail in layer.failures[: None if verbose else 10]:
                print(f"      {YELLOW}·{RESET} {detail}")
            hidden = len(layer.failures) - 10
            if not verbose and hidden > 0:
                print(f"      {DIM}… {hidden} more (--verbose){RESET}")
    print()
    return worst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", type=Path, default=GOLDEN)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not CSV.exists():
        sys.exit(f"instrument master missing at {CSV} — run `make instruments` first")

    cases = load_golden(args.golden)
    index = InstrumentIndex.from_csv(CSV)
    print(f"\n{len(cases)} golden cases · {len(index)} instruments")

    layers = [
        score_intents(cases, index),
        score_resolution(cases, index),
        score_numbers(),
        score_prose(cases),
    ]
    return report(layers, args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
