from datetime import date
from pathlib import Path

import pytest

from app.tools.instruments import InstrumentIndex
from app.tools.types import Instrument

CSV = Path(__file__).resolve().parents[1] / "data" / "instruments.csv"

# The date the bundled instrument master was pulled. Expiry-sensitive
# assertions are pinned to it so the suite does not rot.
MASTER_DATE = date(2026, 9, 13)


@pytest.fixture(scope="session")
def real_index() -> InstrumentIndex:
    if not CSV.exists():
        pytest.skip(f"instrument master not present at {CSV} — run `make instruments`")
    return InstrumentIndex.from_csv(CSV)


def inst(**kw) -> Instrument:
    base = dict(
        exchange="NSE",
        segment="CASH",
        trading_symbol="RELIANCE",
        exchange_token="2885",
        name="Reliance Industries",
        instrument_type="EQ",
    )
    return Instrument(**{**base, **kw})


@pytest.fixture
def tiny_index() -> InstrumentIndex:
    """A hand-built index for behaviour that does not need 136k rows."""
    return InstrumentIndex(
        [
            inst(isin="INE002A01018"),
            inst(exchange="BSE", trading_symbol="RELIANCE", exchange_token="500325",
                 isin="INE002A01018"),
            inst(trading_symbol="NIFTY", exchange_token="NIFTY", name="NIFTY 50",
                 instrument_type="IDX"),
            inst(
                segment="FNO",
                trading_symbol="NIFTY2691525000CE",
                exchange_token="47363",
                name="",
                instrument_type="CE",
                underlying="NIFTY",
                expiry=date(2026, 9, 15),
                strike=25000.0,
                lot_size=65,
            ),
            inst(
                segment="FNO",
                trading_symbol="NIFTY2692225000CE",
                exchange_token="47364",
                name="",
                instrument_type="CE",
                underlying="NIFTY",
                expiry=date(2026, 9, 22),
                strike=25000.0,
                lot_size=65,
            ),
        ]
    )
