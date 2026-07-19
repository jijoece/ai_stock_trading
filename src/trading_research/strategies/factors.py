"""Deterministic, stdlib-only bar-based indicator helpers for strategies.

`scripts/indicators.py` is close-price-only and lives outside the
`trading_research` package (built for the manual MCP-fetch workflow, not
for import from `src/`). Strategies need high/low-based ATR and
volume-based ratios that module doesn't provide, so this module holds the
small set of pure functions the strategy scanners share. Every function
returns `None` on insufficient input — never a favorable default.
"""
from __future__ import annotations

from statistics import pstdev

from ..backtesting.models import HistoricalBar


def closes(bars: tuple[HistoricalBar, ...]) -> list[float]:
    return [float(b.close) for b in bars]


def simple_moving_average(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def rolling_zscore(values: list[float], period: int) -> float | None:
    if period <= 1 or len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    sd = pstdev(window)
    if sd == 0:
        return None
    return (values[-1] - mean) / sd


def average_true_range(bars: tuple[HistoricalBar, ...], period: int = 14) -> float | None:
    if period <= 0 or len(bars) < period + 1:
        return None
    true_ranges: list[float] = []
    for i in range(1, len(bars)):
        high = float(bars[i].high)
        low = float(bars[i].low)
        prev_close = float(bars[i - 1].close)
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(true_ranges) < period:
        return None
    return sum(true_ranges[-period:]) / period


def volume_ratio(bars: tuple[HistoricalBar, ...], lookback_days: int) -> float | None:
    """Latest bar's volume vs the average of the `lookback_days` bars before it."""
    if lookback_days <= 0 or len(bars) < lookback_days + 1:
        return None
    prior = bars[-(lookback_days + 1):-1]
    avg = sum(b.volume for b in prior) / lookback_days
    if avg <= 0:
        return None
    return bars[-1].volume / avg


def rsi_wilder(values: list[float], period: int = 14) -> float | None:
    if period <= 0 or len(values) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def sma_slope(values: list[float], period: int, lookback: int) -> float | None:
    """Difference between the current SMA(period) and the SMA(period) `lookback` bars ago."""
    if len(values) < period + lookback:
        return None
    current = simple_moving_average(values, period)
    prior = simple_moving_average(values[:-lookback], period)
    if current is None or prior is None:
        return None
    return current - prior


def prior_high(bars: tuple[HistoricalBar, ...], lookback_days: int) -> float | None:
    """Highest high over the `lookback_days` bars strictly before the latest bar."""
    if lookback_days <= 0 or len(bars) < lookback_days + 1:
        return None
    window = bars[-(lookback_days + 1):-1]
    return float(max(b.high for b in window))
