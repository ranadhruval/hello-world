from datetime import datetime

from app.config import IST
from app.market.calendar import (
    close_label,
    is_open,
    is_preopen,
    is_trading_day,
    previous_trading_day,
)

MON = datetime(2026, 9, 14, tzinfo=IST).date()  # a Monday
SAT = datetime(2026, 9, 12, tzinfo=IST).date()
REPUBLIC_DAY = datetime(2026, 1, 26, tzinfo=IST).date()


def at(h: int, m: int = 0, day: int = 11):
    """2026-09-11 is a Friday."""
    return datetime(2026, 9, day, h, m, tzinfo=IST)


def test_weekend_is_not_a_trading_day():
    assert not is_trading_day(SAT)
    assert is_trading_day(MON)


def test_listed_holiday_is_not_a_trading_day():
    assert not is_trading_day(REPUBLIC_DAY)


def test_equity_hours():
    assert not is_open("CASH", at(9, 0))
    assert is_open("CASH", at(9, 15))
    assert is_open("CASH", at(14, 32))
    assert not is_open("CASH", at(15, 30))
    assert not is_open("CASH", at(18, 0))


def test_mcx_runs_late():
    """MCX non-agri trades to 23:30 — treating it like equity is wrong for
    eight hours a day."""
    assert is_open("COMMODITY", at(18, 0))
    assert is_open("COMMODITY", at(23, 0))
    assert not is_open("COMMODITY", at(23, 45))


def test_mcx_agri_closes_early():
    assert not is_open("COMMODITY", at(18, 0), underlying="COTTON")
    assert is_open("COMMODITY", at(16, 0), underlying="COTTON")


def test_nothing_is_open_on_a_weekend():
    assert not is_open("CASH", datetime(2026, 9, 12, 11, 0, tzinfo=IST))
    assert not is_open("COMMODITY", datetime(2026, 9, 12, 11, 0, tzinfo=IST))


def test_preopen_window():
    assert is_preopen(at(9, 3))
    assert not is_preopen(at(9, 10))


def test_close_label_is_empty_while_open():
    assert close_label("CASH", at(11, 0)) == ""


def test_close_label_after_hours_names_the_session():
    label = close_label("CASH", at(18, 0))
    assert "at close" in label and "15:30" in label


def test_close_label_on_a_weekend_points_at_friday():
    label = close_label("CASH", datetime(2026, 9, 13, 11, 0, tzinfo=IST))  # Sunday
    assert "Fri" in label


def test_previous_trading_day_skips_the_weekend():
    assert previous_trading_day(MON) == datetime(2026, 9, 11, tzinfo=IST).date()
