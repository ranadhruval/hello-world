"""Instrument master and resolver (spec §5.4).

This is where most trading chatbots die: the user types "nifty 25000 ce" and
you need NIFTY2691525000CE, NSE, FNO, expiry 2026-09-15, lot size 65,
exchange_token 46681.

Schema notes, verified against a live instrument.csv (136,779 rows):

  columns: exchange, exchange_token, trading_symbol, groww_symbol, name,
           instrument_type, segment, series, isin, underlying_symbol,
           underlying_exchange_token, expiry_date, strike_price, lot_size,
           tick_size, freeze_quantity, is_reserved, buy_allowed, sell_allowed,
           internal_trading_symbol, is_intraday

  - `groww_symbol` is the structured canonical key and is far easier to match
    against than trading_symbol: "NSE-NIFTY-15Sep26-19550-CE", "MCX-GOLD-25Sep26-192000-CE".
  - FUT rows carry a strike sentinel of -0.01 or 0, not null.
  - EQ and IDX rows have a blank expiry_date.
  - NSE carries a COMMODITY segment of its own, so GOLD exists on both NSE
    (8,724 rows) and MCX (3,706). "gold" is genuinely ambiguous and must
    disambiguate rather than guess.
  - Lot sizes come from here and only from here. NIFTY is 65, not the 75 it
    was when the spec was written.
  - The file contains NSETEST instruments and one blank-symbol index row.
"""

from __future__ import annotations

import csv
import logging
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from app.tools.aliases import (
    COMMODITY_ALIASES,
    FUTURE_WORDS,
    MONTHS,
    OPTION_WORDS,
    expand_alias,
    normalise,
)
from app.tools.types import Instrument

log = logging.getLogger(__name__)

CSV_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"

# Ranking weights (spec §5.4 step 5)
W_USER_BOOK = 100.0
W_EXACT_SYMBOL = 50.0
W_INDEX = 20.0
W_TRIGRAM = 10.0
W_NEAREST_EXPIRY = 25.0
# GOLD trades on both MCX and NSE. Knowing which venue the user actually
# trades is what turns that coin-flip into an answer.
W_USER_VENUE = 30.0
DISAMBIGUATE_MARGIN = 15.0

# A dual-listed equity is one instrument quoted twice, not a real ambiguity.
# Collapse on ISIN and keep the more liquid venue (spec §5.3 gotcha 1).
EXCHANGE_PREFERENCE = {"NSE": 0, "BSE": 1, "MCX": 2}

_TEST_INSTRUMENT = re.compile(r"NSETEST|BSETEST", re.I)


def _to_float(v: str | None) -> float | None:
    if not v or not v.strip():
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _to_date(v: str | None) -> date | None:
    if not v or not v.strip():
        return None
    try:
        return datetime.strptime(v.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_row(row: dict[str, str]) -> Instrument | None:
    """Turn one CSV row into an Instrument, or None if it is not resolvable."""
    symbol = (row.get("trading_symbol") or "").strip()
    if not symbol or _TEST_INSTRUMENT.search(symbol):
        return None

    strike = _to_float(row.get("strike_price"))
    if strike is not None and strike <= 0:
        strike = None  # FUT rows carry -0.01 / 0 as a sentinel

    lot = _to_float(row.get("lot_size"))
    tick = _to_float(row.get("tick_size"))

    return Instrument(
        exchange=(row.get("exchange") or "").strip(),
        segment=(row.get("segment") or "").strip(),
        trading_symbol=symbol,
        exchange_token=(row.get("exchange_token") or "").strip(),
        groww_symbol=(row.get("groww_symbol") or "").strip() or None,
        name=(row.get("name") or "").strip(),
        isin=(row.get("isin") or "").strip() or None,
        instrument_type=(row.get("instrument_type") or "EQ").strip(),
        underlying=(row.get("underlying_symbol") or "").strip() or None,
        expiry=_to_date(row.get("expiry_date")),
        strike=strike,
        lot_size=int(lot) if lot else 1,
        tick_size=tick if tick else 0.05,
    )


def read_csv(path: str | Path) -> list[Instrument]:
    out: list[Instrument] = []
    seen: set[tuple[str, str, str]] = set()
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            inst = parse_row(row)
            if inst is None:
                continue
            key = (inst.exchange, inst.segment, inst.trading_symbol)
            if key in seen:  # the file carries a handful of exact duplicates
                continue
            seen.add(key)
            out.append(inst)
    return out


def trigram_similarity(a: str, b: str) -> float:
    """Jaccard over padded trigrams — the same shape pg_trgm uses.

    Keeping the in-process scoring identical to pg_trgm means moving the
    resolver to a Postgres query later does not change which candidate wins.
    """
    if not a or not b:
        return 0.0
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _trigrams(s: str) -> set[str]:
    padded = f"  {s.strip().lower()} "
    return {padded[i : i + 3] for i in range(len(padded) - 2)}


@dataclass
class Candidate:
    instrument: Instrument
    score: float
    reason: str = ""


@dataclass
class Resolution:
    """Either one instrument, or a disambiguation prompt. Never a guess."""

    instrument: Instrument | None
    candidates: list[Candidate]
    ambiguous: bool

    @property
    def ok(self) -> bool:
        return self.instrument is not None and not self.ambiguous


@dataclass(frozen=True)
class _Book:
    """What the user already trades — spec §5.4 step 5(a).

    This, plus the per-user resolution cache, is what makes the resolver feel
    like it knows you.
    """

    symbols: frozenset[str] | set[str]
    venues: frozenset[tuple[str, str]] | set[tuple[str, str]]

    def boost(self, inst: Instrument) -> float:
        score = 0.0
        if inst.trading_symbol.upper() in self.symbols:
            score += W_USER_BOOK
        elif inst.underlying and inst.underlying.upper() in self.symbols:
            score += W_USER_BOOK
        key = (inst.exchange.upper(), (inst.underlying or inst.trading_symbol).upper())
        if key in self.venues:
            score += W_USER_VENUE
        return score


@dataclass(frozen=True)
class DerivativeQuery:
    underlying: str
    kind: str  # CE | PE | FUT
    strike: float | None = None
    month: int | None = None


class InstrumentIndex:
    """In-process index over the master.

    Resolution is on the fast path (p50 < 900ms), so this stays in memory and
    is rebuilt from the daily CSV rather than round-tripping to Postgres.
    """

    def __init__(self, instruments: list[Instrument]) -> None:
        self.all = instruments
        self.by_key: dict[tuple[str, str, str], Instrument] = {}
        self.by_groww_symbol: dict[str, Instrument] = {}
        self.by_symbol: dict[str, list[Instrument]] = defaultdict(list)
        self.by_underlying: dict[str, list[Instrument]] = defaultdict(list)
        self.by_isin: dict[str, list[Instrument]] = defaultdict(list)
        self.tradeable: list[Instrument] = []
        # Inverted trigram index over spot names, so fuzzy lookup touches a
        # few dozen candidates instead of scanning all 12k of them.
        self._trigram_index: dict[str, set[int]] = defaultdict(set)

        for inst in instruments:
            self.by_key[(inst.exchange, inst.segment, inst.trading_symbol)] = inst
            if inst.groww_symbol:
                self.by_groww_symbol[inst.groww_symbol.upper()] = inst
            self.by_symbol[inst.trading_symbol.upper()].append(inst)
            if inst.underlying:
                self.by_underlying[inst.underlying.upper()].append(inst)
            if inst.isin:
                self.by_isin[inst.isin.upper()].append(inst)
            if inst.instrument_type in {"EQ", "IDX"}:
                pos = len(self.tradeable)
                self.tradeable.append(inst)
                for gram in _trigrams(inst.name) | _trigrams(inst.trading_symbol):
                    self._trigram_index[gram].add(pos)

    def __len__(self) -> int:
        return len(self.all)

    @classmethod
    def from_csv(cls, path: str | Path) -> InstrumentIndex:
        return cls(read_csv(path))

    # ---- resolution ------------------------------------------------

    def resolve(
        self,
        query: str,
        user_symbols: set[str] | None = None,
        user_venues: set[tuple[str, str]] | None = None,
        today: date | None = None,
    ) -> Resolution:
        """Resolve free text to an instrument.

        `user_symbols` are the trading symbols and underlyings the user
        already holds or has positions in. They score +100, which is what
        makes "nifty" surface their NIFTY position rather than the index.

        `user_venues` are (exchange, underlying) pairs from the same book.
        They break venue ties that `user_symbols` cannot: a trader with an
        MCX gold position means MCX when they type "gold".
        """
        book = _Book(
            symbols={s.upper() for s in (user_symbols or set())},
            venues={(e.upper(), u.upper()) for e, u in (user_venues or set())},
        )
        today = today or date.today()
        norm = normalise(query)
        if not norm:
            return Resolution(None, [], ambiguous=False)

        deriv = parse_derivative(norm)
        if deriv:
            cands = self._derivative_candidates(deriv, book, today)
            if not cands:
                # Strike off the ladder: show neighbours rather than nothing.
                near = self._nearest_strikes(deriv, today)
                return Resolution(None, near, ambiguous=bool(near))
        else:
            cands = self._spot_candidates(norm, book, today)

        return _decide(cands)

    def _derivative_candidates(
        self, q: DerivativeQuery, book: _Book, today: date
    ) -> list[Candidate]:
        pool = self.by_underlying.get(q.underlying.upper(), [])
        matches = [
            i
            for i in pool
            if i.instrument_type == q.kind
            and (q.strike is None or i.strike == q.strike)
            and (i.expiry is None or i.expiry >= today)
            and (q.month is None or (i.expiry and i.expiry.month == q.month))
        ]
        if not matches:
            return []

        # Nearest expiry wins unless the user gave a month hint.
        nearest = min((i.expiry for i in matches if i.expiry), default=None)
        out: list[Candidate] = []
        for inst in matches:
            score = W_EXACT_SYMBOL + book.boost(inst)
            if inst.expiry == nearest:
                score += W_NEAREST_EXPIRY
            out.append(Candidate(inst, score, "derivative"))
        return sorted(out, key=lambda c: -c.score)

    def _nearest_strikes(
        self, q: DerivativeQuery, today: date, width: int = 3
    ) -> list[Candidate]:
        """The requested strike does not exist — offer the closest that do.

        Better than a bare "not found": the user typed a strike that is off
        the ladder (wrong step, stale spot) and wants the neighbours.
        """
        if q.strike is None:
            return []
        pool = [
            i
            for i in self.by_underlying.get(q.underlying.upper(), [])
            if i.instrument_type == q.kind and i.strike and i.expiry and i.expiry >= today
        ]
        if not pool:
            return []
        nearest_expiry = min(i.expiry for i in pool)
        ladder = [i for i in pool if i.expiry == nearest_expiry]
        ladder.sort(key=lambda i: abs((i.strike or 0) - q.strike))
        return [Candidate(i, 0.0, "nearest strike") for i in ladder[:width]]

    def _spot_candidates(self, norm: str, book: _Book, today: date) -> list[Candidate]:
        token = expand_alias(norm).upper()
        out: list[Candidate] = []

        # "chandi" means the metal. NSE lists an equity called SILVER and one
        # called GOLD1, and answering with those would be badly wrong, so a
        # commodity alias never falls through to the equity table.
        if norm in COMMODITY_ALIASES:
            return self._front_futures(token, book, today)

        for inst in self.by_symbol.get(token, []):
            if inst.is_derivative:
                continue
            score = W_EXACT_SYMBOL + book.boost(inst)
            if inst.instrument_type == "IDX":
                score += W_INDEX
            out.append(Candidate(inst, score, "exact symbol"))

        for inst in self.by_isin.get(token, []):
            out.append(Candidate(inst, W_EXACT_SYMBOL + book.boost(inst), "isin"))

        # A commodity like GOLD has no spot instrument — it exists only as
        # futures and options. "gold" means the near-month future.
        if not out and token in self.by_underlying:
            out.extend(self._front_futures(token, book, today))

        if not out:
            out.extend(self._fuzzy(norm, token, book))

        return _collapse_dual_listings(sorted(out, key=lambda c: -c.score))[:10]

    def _front_futures(self, underlying: str, book: _Book, today: date) -> list[Candidate]:
        futures = [
            i
            for i in self.by_underlying.get(underlying, [])
            if i.instrument_type == "FUT" and i.expiry and i.expiry >= today
        ]
        if not futures:
            return []
        out: list[Candidate] = []
        # Front month per exchange: GOLD trades on both MCX and NSE, and those
        # are genuinely different contracts, so both belong in the shortlist.
        for exchange in {i.exchange for i in futures}:
            leg = min(
                (i for i in futures if i.exchange == exchange),
                key=lambda i: i.expiry,  # type: ignore[arg-type,return-value]
            )
            out.append(Candidate(leg, W_EXACT_SYMBOL + book.boost(leg), "front-month future"))
        return sorted(out, key=lambda c: -c.score)

    def _fuzzy(self, norm: str, token: str, book: _Book) -> list[Candidate]:
        grams = _trigrams(norm) | _trigrams(token)
        positions: set[int] = set()
        for gram in grams:
            positions |= self._trigram_index.get(gram, set())

        out: list[Candidate] = []
        for pos in positions:
            inst = self.tradeable[pos]
            sim = max(
                trigram_similarity(norm, inst.name),
                trigram_similarity(norm, inst.trading_symbol),
                trigram_similarity(token, inst.trading_symbol),
            )
            if sim < 0.4:  # spec §5.4 step 4 threshold
                continue
            score = sim * W_TRIGRAM + book.boost(inst)
            if inst.instrument_type == "IDX":
                score += W_INDEX
            out.append(Candidate(inst, score, f"trigram {sim:.2f}"))
        return out


def _collapse_dual_listings(cands: list[Candidate]) -> list[Candidate]:
    """Keep one row per ISIN — the same equity on NSE and BSE is one answer."""
    best_for: dict[str, Candidate] = {}
    out: list[Candidate] = []
    for cand in cands:
        isin = cand.instrument.isin
        if not isin:
            out.append(cand)
            continue
        incumbent = best_for.get(isin)
        if incumbent is None:
            best_for[isin] = cand
            out.append(cand)
        elif _venue_rank(cand.instrument) < _venue_rank(incumbent.instrument):
            out[out.index(incumbent)] = cand
            best_for[isin] = cand
    return out


def _venue_rank(inst: Instrument) -> int:
    return EXCHANGE_PREFERENCE.get(inst.exchange, 99)


def _decide(cands: list[Candidate]) -> Resolution:
    if not cands:
        return Resolution(None, [], ambiguous=False)
    if len(cands) == 1:
        return Resolution(cands[0].instrument, cands, ambiguous=False)
    # Never guess on a close call (spec §5.4 step 6).
    ambiguous = (cands[0].score - cands[1].score) < DISAMBIGUATE_MARGIN
    return Resolution(None if ambiguous else cands[0].instrument, cands[:3], ambiguous)


_STRIKE = re.compile(r"\b(\d{2,7}(?:\.\d+)?)\b")


def parse_derivative(norm: str) -> DerivativeQuery | None:
    """Detect '<underlying> [<month>] <strike> <ce|pe>' and '<underlying> fut'.

    Input must already be normalised (lakh/k expanded, punctuation stripped).
    """
    tokens = norm.split()
    if not tokens:
        return None

    kind: str | None = None
    rest: list[str] = []
    for tok in tokens:
        if tok in OPTION_WORDS:
            kind = OPTION_WORDS[tok]
        elif tok in FUTURE_WORDS:
            kind = "FUT"
        else:
            rest.append(tok)
    if kind is None:
        return None

    month = None
    for tok in list(rest):
        if tok in MONTHS:
            month = MONTHS[tok]
            rest.remove(tok)

    strike = None
    for tok in list(rest):
        if _STRIKE.fullmatch(tok):
            strike = float(tok)
            rest.remove(tok)
            break

    if not rest:
        return None
    underlying = expand_alias(" ".join(rest))
    if kind in {"CE", "PE"} and strike is None:
        return None
    return DerivativeQuery(underlying=underlying, kind=kind, strike=strike, month=month)


# ---- persistence ---------------------------------------------------

UPSERT = """
INSERT INTO instruments (exchange, segment, trading_symbol, exchange_token, groww_symbol,
                         isin, name, instrument_type, underlying, expiry, strike,
                         lot_size, tick_size)
VALUES (%(exchange)s, %(segment)s, %(trading_symbol)s, %(exchange_token)s, %(groww_symbol)s,
        %(isin)s, %(name)s, %(instrument_type)s, %(underlying)s, %(expiry)s, %(strike)s,
        %(lot_size)s, %(tick_size)s)
ON CONFLICT (exchange, segment, trading_symbol) DO UPDATE SET
  exchange_token = EXCLUDED.exchange_token,
  groww_symbol   = EXCLUDED.groww_symbol,
  isin           = EXCLUDED.isin,
  name           = EXCLUDED.name,
  instrument_type= EXCLUDED.instrument_type,
  underlying     = EXCLUDED.underlying,
  expiry         = EXCLUDED.expiry,
  strike         = EXCLUDED.strike,
  lot_size       = EXCLUDED.lot_size,
  tick_size      = EXCLUDED.tick_size
"""


def download(dest: Path) -> Path:
    """Fetch the daily master. Run at 07:30 IST (spec §23).

    Writes to a temporary file and moves it into place, so an interrupted
    download cannot leave a half-written master that parses to junk.
    """
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", CSV_URL, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_bytes():
                fh.write(chunk)
    tmp.replace(dest)
    return dest


def ensure_csv(path: Path) -> Path:
    """Return the master, downloading it if absent.

    Lets read-only consumers (reconcile, the REPL) work on a fresh clone.
    data/instruments.csv is gitignored, so it is never present after a clone,
    and downloading it needs no database.
    """
    if not path.exists():
        log.info("instrument master missing, downloading %s", CSV_URL)
        download(path)
    return path


def load_to_postgres(instruments: list[Instrument], dsn: str) -> int:
    import psycopg

    rows = [
        {
            "exchange": i.exchange,
            "segment": i.segment,
            "trading_symbol": i.trading_symbol,
            "exchange_token": i.exchange_token,
            "groww_symbol": i.groww_symbol,
            "isin": i.isin,
            "name": i.name,
            "instrument_type": i.instrument_type,
            "underlying": i.underlying,
            "expiry": i.expiry,
            "strike": i.strike,
            "lot_size": i.lot_size,
            "tick_size": i.tick_size,
        }
        for i in instruments
    ]
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.executemany(UPSERT, rows)
        conn.commit()
    return len(rows)


USAGE = "usage: python -m app.tools.instruments [download|refresh|stats] [csv-path]"


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cmd = argv[1] if len(argv) > 1 else "download"
    path = Path(argv[2]) if len(argv) > 2 else Path("data/instruments.csv")

    # `download` needs no database, which is what reconciliation and the REPL
    # want. Only `refresh` touches Postgres.
    if cmd in {"download", "refresh"}:
        log.info("downloading %s", CSV_URL)
        download(path)
        instruments = read_csv(path)
        log.info("parsed %d instruments -> %s", len(instruments), path)

        if cmd == "download":
            return 0

        from app.config import settings

        dsn = settings().database_url.replace("postgresql+psycopg://", "postgresql://")
        log.info("loaded %d rows into postgres", load_to_postgres(instruments, dsn))
        return 0

    if cmd == "stats":
        idx = InstrumentIndex.from_csv(ensure_csv(path))
        log.info("%d instruments, %d underlyings", len(idx), len(idx.by_underlying))
        return 0

    log.error(USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
