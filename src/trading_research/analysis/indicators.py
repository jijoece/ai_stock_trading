"""Point-in-time-safe deterministic market indicators.

The functions in this module consume already-ordered OHLC bars and never
consult a clock or provider.  Financial callers therefore control the exact
information set.  Decimal is retained end-to-end at the risk boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable


class IndicatorError(ValueError):
    pass


@dataclass(frozen=True)
class OHLCBar:
    session_date: date
    high: Decimal
    low: Decimal
    close: Decimal
    point_in_time_safe: bool = True

    def __post_init__(self) -> None:
        if not self.point_in_time_safe:
            raise IndicatorError("bar is not point-in-time safe")
        if self.high <= 0 or self.low <= 0 or self.close <= 0:
            raise IndicatorError("OHLC values must be positive")
        if self.high < self.low:
            raise IndicatorError("bar high must be greater than or equal to low")


def _decimal(value: Decimal | int | str, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise IndicatorError(f"{name} is not a valid decimal") from exc
    if not result.is_finite():
        raise IndicatorError(f"{name} must be finite")
    return result


def true_range(
    *, high: Decimal | int | str, low: Decimal | int | str,
    previous_close: Decimal | int | str,
) -> Decimal:
    """Return ``max(high-low, abs(high-prev_close), abs(low-prev_close))``."""
    high_d = _decimal(high, "high")
    low_d = _decimal(low, "low")
    previous_d = _decimal(previous_close, "previous_close")
    if high_d <= 0 or low_d <= 0 or previous_d <= 0:
        raise IndicatorError("high, low, and previous_close must be positive")
    if high_d < low_d:
        raise IndicatorError("high must be greater than or equal to low")
    return max(high_d - low_d, abs(high_d - previous_d), abs(low_d - previous_d))


def average_true_range(bars: Iterable[OHLCBar], *, period: int = 14) -> Decimal | None:
    """Return Wilder ATR using no bar after the supplied information set.

    ``period + 1`` bars are required because every true range includes the
    preceding close.  The first ATR is the arithmetic mean of the first
    ``period`` true ranges; subsequent values use Wilder smoothing.
    ``None`` is the explicit insufficient-history result.
    """
    if type(period) is not int or period <= 0:
        raise IndicatorError("period must be a positive integer")
    ordered = tuple(bars)
    if len(ordered) < period + 1:
        return None
    for previous, current in zip(ordered, ordered[1:]):
        if current.session_date <= previous.session_date:
            raise IndicatorError("bars must have unique, strictly increasing session dates")
    ranges = [
        true_range(high=current.high, low=current.low, previous_close=previous.close)
        for previous, current in zip(ordered, ordered[1:])
    ]
    atr = sum(ranges[:period], Decimal("0")) / Decimal(period)
    for current_range in ranges[period:]:
        atr = ((atr * Decimal(period - 1)) + current_range) / Decimal(period)
    return atr


def atr_risk_levels(
    *, entry_price: Decimal, atr: Decimal, stop_multiple: Decimal,
    target_multiple: Decimal,
) -> tuple[Decimal, Decimal]:
    """Calculate long-only deterministic ATR stop and target levels."""
    if entry_price <= 0 or atr <= 0 or stop_multiple <= 0 or target_multiple <= 0:
        raise IndicatorError("entry price, ATR, and multiples must be positive")
    stop = entry_price - atr * stop_multiple
    target = entry_price + atr * target_multiple
    if stop <= 0 or stop >= entry_price or target <= entry_price:
        raise IndicatorError("ATR configuration produced invalid long risk levels")
    return stop, target
