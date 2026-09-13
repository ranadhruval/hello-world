"""Query normalisation and the alias table (spec §5.4 step 1, §6.4 rule 3).

Instrument names are never translated — "sona" resolves to GOLD, it does not
become a Hindi word in the reply.
"""

from __future__ import annotations

import re

ALIASES: dict[str, str] = {
    # Index shorthand
    "bn": "BANKNIFTY",
    "bnf": "BANKNIFTY",
    "banknifty": "BANKNIFTY",
    "bank nifty": "BANKNIFTY",
    "nf": "NIFTY",
    "nifty50": "NIFTY",
    "nifty 50": "NIFTY",
    "fin nifty": "FINNIFTY",
    "finnifty": "FINNIFTY",
    "midcpnifty": "MIDCPNIFTY",
    "midcap nifty": "MIDCPNIFTY",
    "sensex": "SENSEX",
    "bankex": "BANKEX",
    # Hinglish commodity names
    "sona": "GOLD",
    "chandi": "SILVER",
    "tel": "CRUDEOIL",
    "crude": "CRUDEOIL",
    "gas": "NATURALGAS",
    "natgas": "NATURALGAS",
    "nat gas": "NATURALGAS",
    "copper": "COPPER",
    "tamba": "COPPER",
    "gold mini": "GOLDM",
    "silver mini": "SILVERM",
    # Common equity longhand
    "reliance industries": "RELIANCE",
    "ril": "RELIANCE",
    "hind zinc": "HINDZINC",
    "hindustan zinc": "HINDZINC",
    "tata motors": "TATAMOTORS",
    "hdfc bank": "HDFCBANK",
    "icici bank": "ICICIBANK",
    "sbi": "SBIN",
    "state bank": "SBIN",
    "infy": "INFY",
    "infosys": "INFY",
}

# Aliases that unambiguously mean a commodity contract. NSE also lists
# equities called SILVER and GOLD1, so "chandi" must not be allowed to fall
# through to an equity spot match — it means the metal, always.
COMMODITY_ALIASES = {
    "sona", "chandi", "tel", "crude", "gas", "natgas", "nat gas", "tamba",
    "gold", "silver", "crudeoil", "naturalgas", "copper", "zinc", "aluminium",
    "gold mini", "silver mini", "goldm", "silverm",
}

OPTION_WORDS = {
    "ce": "CE",
    "call": "CE",
    "calls": "CE",
    "pe": "PE",
    "put": "PE",
    "puts": "PE",
}

FUTURE_WORDS = {"fut", "future", "futures", "fut."}

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# "1 lakh", "1.05 lakh", "25k" — Indians quote strikes this way
_LAKH = re.compile(r"(\d+(?:\.\d+)?)\s*(lakh|lac|l)\b")
_THOUSAND = re.compile(r"\b(\d+(?:\.\d+)?)\s*k\b")

_PUNCT = re.compile(r"[^\w\s.]")
_WS = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, expand numeric shorthand, collapse space."""
    s = text.lower().strip()
    s = _PUNCT.sub(" ", s)
    s = _LAKH.sub(lambda m: str(int(float(m.group(1)) * 100_000)), s)
    s = _THOUSAND.sub(lambda m: str(int(float(m.group(1)) * 1_000)), s)
    return _WS.sub(" ", s).strip()


def expand_alias(token: str) -> str:
    return ALIASES.get(token.lower().strip(), token.upper())
