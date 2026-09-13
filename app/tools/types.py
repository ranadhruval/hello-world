"""Typed objects returned by every tool (spec §5.1).

Invariant I1: every rupee figure in an outbound message is rendered from one
of these. The LLM writes prose around numeric slots; it never emits a digit.
Tools therefore return dataclasses, never strings and never raw JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


def _f(d: dict, *keys: str, default: float = 0.0) -> float:
    """First present key, coerced to float. Groww omits zero-valued fields."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return float(v)
    return default


def _s(d: dict, *keys: str, default: str = "") -> str:
    for k in keys:
        v = d.get(k)
        if v is not None:
            return str(v)
    return default


@dataclass(frozen=True)
class Instrument:
    exchange: str
    segment: str
    trading_symbol: str
    exchange_token: str
    name: str = ""
    isin: str | None = None
    instrument_type: str = "EQ"  # EQ | FUT | CE | PE | IDX
    underlying: str | None = None
    expiry: date | None = None
    strike: float | None = None
    lot_size: int = 1
    tick_size: float = 0.05
    groww_symbol: str | None = None

    @property
    def is_derivative(self) -> bool:
        return self.instrument_type in {"FUT", "CE", "PE"}

    @property
    def ltp_key(self) -> str:
        """The key get_ltp/get_ohlc use, e.g. 'NSE_RELIANCE'."""
        return f"{self.exchange}_{self.trading_symbol}"


@dataclass(frozen=True)
class Holding:
    """Groww's holdings payload carries no LTP and no current value (spec §5.3)."""

    trading_symbol: str
    quantity: float
    average_price: float
    isin: str | None = None
    pledge_quantity: float = 0.0
    demat_locked_quantity: float = 0.0
    groww_locked_quantity: float = 0.0
    repledge_quantity: float = 0.0
    t1_quantity: float = 0.0
    demat_free_quantity: float = 0.0
    corporate_action_additional_quantity: float = 0.0
    active_demat_transfer_quantity: float = 0.0

    @classmethod
    def from_api(cls, d: dict) -> Holding:
        return cls(
            trading_symbol=_s(d, "trading_symbol", "tradingSymbol"),
            quantity=_f(d, "quantity"),
            average_price=_f(d, "average_price", "averagePrice"),
            isin=d.get("isin"),
            pledge_quantity=_f(d, "pledge_quantity", "pledgeQuantity"),
            demat_locked_quantity=_f(d, "demat_locked_quantity", "dematLockedQuantity"),
            groww_locked_quantity=_f(d, "groww_locked_quantity", "growwLockedQuantity"),
            repledge_quantity=_f(d, "repledge_quantity", "repledgeQuantity"),
            t1_quantity=_f(d, "t1_quantity", "t1Quantity"),
            demat_free_quantity=_f(d, "demat_free_quantity", "dematFreeQuantity"),
            corporate_action_additional_quantity=_f(
                d, "corporate_action_additional_quantity", "corporateActionAdditionalQuantity"
            ),
            active_demat_transfer_quantity=_f(
                d, "active_demat_transfer_quantity", "activeDematTransferQuantity"
            ),
        )


@dataclass(frozen=True)
class Position:
    """Groww models positions as credit (bought) vs debit (sold) legs.

    Carries realised_pnl but no unrealised P&L and no LTP (spec §5.3).
    """

    trading_symbol: str
    segment: str
    credit_quantity: float = 0.0
    credit_price: float = 0.0
    debit_quantity: float = 0.0
    debit_price: float = 0.0
    carry_forward_credit_quantity: float = 0.0
    carry_forward_credit_price: float = 0.0
    carry_forward_debit_quantity: float = 0.0
    carry_forward_debit_price: float = 0.0
    quantity: float = 0.0
    net_price: float = 0.0
    net_carry_forward_price: float = 0.0
    realised_pnl: float = 0.0
    exchange: str = ""
    symbol_isin: str | None = None
    product: str = ""

    @classmethod
    def from_api(cls, d: dict) -> Position:
        return cls(
            trading_symbol=_s(d, "trading_symbol", "tradingSymbol"),
            segment=_s(d, "segment"),
            credit_quantity=_f(d, "credit_quantity", "creditQuantity"),
            credit_price=_f(d, "credit_price", "creditPrice"),
            debit_quantity=_f(d, "debit_quantity", "debitQuantity"),
            debit_price=_f(d, "debit_price", "debitPrice"),
            carry_forward_credit_quantity=_f(
                d, "carry_forward_credit_quantity", "carryForwardCreditQuantity"
            ),
            carry_forward_credit_price=_f(
                d, "carry_forward_credit_price", "carryForwardCreditPrice"
            ),
            carry_forward_debit_quantity=_f(
                d, "carry_forward_debit_quantity", "carryForwardDebitQuantity"
            ),
            carry_forward_debit_price=_f(
                d, "carry_forward_debit_price", "carryForwardDebitPrice"
            ),
            quantity=_f(d, "quantity"),
            net_price=_f(d, "net_price", "netPrice"),
            net_carry_forward_price=_f(d, "net_carry_forward_price", "netCarryForwardPrice"),
            realised_pnl=_f(d, "realised_pnl", "realisedPnl", "realized_pnl"),
            exchange=_s(d, "exchange"),
            symbol_isin=d.get("symbol_isin") or d.get("symbolIsin"),
            product=_s(d, "product"),
        )


@dataclass(frozen=True)
class FnoMargin:
    net_fno_margin_used: float = 0.0
    span_margin_used: float = 0.0
    exposure_margin_used: float = 0.0
    future_balance_available: float = 0.0
    option_buy_balance_available: float = 0.0
    option_sell_balance_available: float = 0.0


@dataclass(frozen=True)
class EquityMargin:
    net_equity_margin_used: float = 0.0
    cnc_margin_used: float = 0.0
    mis_margin_used: float = 0.0
    cnc_balance_available: float = 0.0
    mis_balance_available: float = 0.0


@dataclass(frozen=True)
class MarginState:
    clear_cash: float = 0.0
    net_margin_used: float = 0.0
    brokerage_and_charges: float = 0.0
    collateral_used: float = 0.0
    collateral_available: float = 0.0
    adhoc_margin: float = 0.0
    fno: FnoMargin = field(default_factory=FnoMargin)
    equity: EquityMargin = field(default_factory=EquityMargin)

    @classmethod
    def from_api(cls, d: dict) -> MarginState:
        fno = d.get("fno_margin_details") or d.get("fnoMarginDetails") or {}
        eq = d.get("equity_margin_details") or d.get("equityMarginDetails") or {}
        return cls(
            clear_cash=_f(d, "clear_cash", "clearCash"),
            net_margin_used=_f(d, "net_margin_used", "netMarginUsed"),
            brokerage_and_charges=_f(d, "brokerage_and_charges", "brokerageAndCharges"),
            collateral_used=_f(d, "collateral_used", "collateralUsed"),
            collateral_available=_f(d, "collateral_available", "collateralAvailable"),
            adhoc_margin=_f(d, "adhoc_margin", "adhocMargin"),
            fno=FnoMargin(
                net_fno_margin_used=_f(fno, "net_fno_margin_used", "netFnoMarginUsed"),
                span_margin_used=_f(fno, "span_margin_used", "spanMarginUsed"),
                exposure_margin_used=_f(fno, "exposure_margin_used", "exposureMarginUsed"),
                future_balance_available=_f(
                    fno, "future_balance_available", "futureBalanceAvailable"
                ),
                option_buy_balance_available=_f(
                    fno, "option_buy_balance_available", "optionBuyBalanceAvailable"
                ),
                option_sell_balance_available=_f(
                    fno, "option_sell_balance_available", "optionSellBalanceAvailable"
                ),
            ),
            equity=EquityMargin(
                net_equity_margin_used=_f(eq, "net_equity_margin_used", "netEquityMarginUsed"),
                cnc_margin_used=_f(eq, "cnc_margin_used", "cncMarginUsed"),
                mis_margin_used=_f(eq, "mis_margin_used", "misMarginUsed"),
                cnc_balance_available=_f(eq, "cnc_balance_available", "cncBalanceAvailable"),
                mis_balance_available=_f(eq, "mis_balance_available", "misBalanceAvailable"),
            ),
        )


@dataclass(frozen=True)
class OHLC:
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0

    @classmethod
    def from_api(cls, d: dict) -> OHLC:
        return cls(
            open=_f(d, "open"), high=_f(d, "high"), low=_f(d, "low"), close=_f(d, "close")
        )


@dataclass(frozen=True)
class Quote:
    trading_symbol: str
    last_price: float
    day_change: float = 0.0
    day_change_perc: float = 0.0
    ohlc: OHLC = field(default_factory=OHLC)
    volume: float = 0.0
    open_interest: float = 0.0
    oi_day_change: float = 0.0
    implied_volatility: float | None = None
    upper_circuit_limit: float | None = None
    lower_circuit_limit: float | None = None
    week_52_high: float | None = None
    week_52_low: float | None = None
    as_of: datetime | None = None

    @classmethod
    def from_api(cls, d: dict, trading_symbol: str = "") -> Quote:
        iv = d.get("implied_volatility") or d.get("impliedVolatility")
        return cls(
            trading_symbol=trading_symbol or _s(d, "trading_symbol", "tradingSymbol"),
            last_price=_f(d, "last_price", "lastPrice", "ltp"),
            day_change=_f(d, "day_change", "dayChange"),
            day_change_perc=_f(d, "day_change_perc", "dayChangePerc"),
            ohlc=OHLC.from_api(d.get("ohlc") or {}),
            volume=_f(d, "volume"),
            open_interest=_f(d, "open_interest", "openInterest"),
            oi_day_change=_f(d, "oi_day_change", "oiDayChange"),
            implied_volatility=float(iv) if iv is not None else None,
            upper_circuit_limit=_opt(d, "upper_circuit_limit", "upperCircuitLimit"),
            lower_circuit_limit=_opt(d, "lower_circuit_limit", "lowerCircuitLimit"),
            week_52_high=_opt(d, "week_52_high", "week52High"),
            week_52_low=_opt(d, "week_52_low", "week52Low"),
        )


def _opt(d: dict, *keys: str) -> float | None:
    for k in keys:
        v = d.get(k)
        if v is not None:
            return float(v)
    return None


@dataclass(frozen=True)
class Greeks:
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    rho: float = 0.0
    iv: float = 0.0

    @classmethod
    def from_api(cls, d: dict) -> Greeks:
        return cls(
            delta=_f(d, "delta"),
            gamma=_f(d, "gamma"),
            theta=_f(d, "theta"),
            vega=_f(d, "vega"),
            rho=_f(d, "rho"),
            iv=_f(d, "iv", "implied_volatility", "impliedVolatility"),
        )


@dataclass(frozen=True)
class OptionLeg:
    trading_symbol: str
    ltp: float = 0.0
    open_interest: float = 0.0
    volume: float = 0.0
    greeks: Greeks = field(default_factory=Greeks)

    @classmethod
    def from_api(cls, d: dict) -> OptionLeg:
        return cls(
            trading_symbol=_s(d, "trading_symbol", "tradingSymbol"),
            ltp=_f(d, "ltp", "last_price", "lastPrice"),
            open_interest=_f(d, "open_interest", "openInterest"),
            volume=_f(d, "volume"),
            greeks=Greeks.from_api(d.get("greeks") or {}),
        )


@dataclass(frozen=True)
class OptionChain:
    """One call gives OI walls, max pain, the IV surface and greeks (spec Appendix A)."""

    underlying: str
    expiry: str
    underlying_ltp: float
    strikes: dict[float, dict[str, OptionLeg]]  # {25000.0: {"CE": leg, "PE": leg}}
    as_of: datetime | None = None

    @classmethod
    def from_api(cls, d: dict, underlying: str, expiry: str) -> OptionChain:
        raw = d.get("strikes") or d.get("option_chain") or {}
        strikes: dict[float, dict[str, OptionLeg]] = {}
        for strike, legs in raw.items():
            side: dict[str, OptionLeg] = {}
            for key in ("CE", "PE"):
                leg = legs.get(key) if isinstance(legs, dict) else None
                if leg:
                    side[key] = OptionLeg.from_api(leg)
            if side:
                strikes[float(strike)] = side
        return cls(
            underlying=underlying,
            expiry=expiry,
            underlying_ltp=_f(d, "underlying_ltp", "underlyingLtp"),
            strikes=strikes,
        )


@dataclass(frozen=True)
class Order:
    groww_order_id: str
    trading_symbol: str
    status: str
    transaction_type: str = ""
    order_type: str = ""
    segment: str = ""
    exchange: str = ""
    product: str = ""
    quantity: float = 0.0
    filled_quantity: float = 0.0
    remaining_quantity: float = 0.0
    price: float = 0.0
    trigger_price: float = 0.0
    average_fill_price: float = 0.0
    created_at: str = ""

    OPEN_STATES = ("NEW", "ACKED", "OPEN", "TRIGGER_PENDING", "APPROVED", "MODIFICATION_REQUESTED")

    @property
    def is_open(self) -> bool:
        return self.status.upper() in self.OPEN_STATES

    @classmethod
    def from_api(cls, d: dict) -> Order:
        return cls(
            groww_order_id=_s(d, "groww_order_id", "growwOrderId", "order_id"),
            trading_symbol=_s(d, "trading_symbol", "tradingSymbol"),
            status=_s(d, "order_status", "orderStatus", "status"),
            transaction_type=_s(d, "transaction_type", "transactionType"),
            order_type=_s(d, "order_type", "orderType"),
            segment=_s(d, "segment"),
            exchange=_s(d, "exchange"),
            product=_s(d, "product"),
            quantity=_f(d, "quantity"),
            filled_quantity=_f(d, "filled_quantity", "filledQuantity"),
            remaining_quantity=_f(d, "remaining_quantity", "remainingQuantity"),
            price=_f(d, "price"),
            trigger_price=_f(d, "trigger_price", "triggerPrice"),
            average_fill_price=_f(d, "average_fill_price", "averageFillPrice"),
            created_at=_s(d, "created_at", "createdAt", "order_timestamp"),
        )
