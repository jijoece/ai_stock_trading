"""Point-in-time historical data contract and offline fixture provider."""
from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from .models import BacktestError, HistoricalBar


class HistoricalDataProvider(Protocol):
    def bars(
        self, *, symbol: str, start: date, end: date, as_of: datetime,
    ) -> tuple[HistoricalBar, ...]: ...


class FixtureHistoricalDataProvider:
    def __init__(self, bars_by_symbol: dict[str, tuple[HistoricalBar, ...]]):
        self._bars = bars_by_symbol

    def bars(
        self, *, symbol: str, start: date, end: date, as_of: datetime,
    ) -> tuple[HistoricalBar, ...]:
        if as_of.tzinfo is None:
            raise BacktestError("as_of must be timezone-aware")
        selected = tuple(
            bar for bar in self._bars.get(symbol, ())
            if start <= bar.session_date <= end and bar.available_at <= as_of
        )
        if any(bar.available_at > as_of or not bar.point_in_time_safe for bar in selected):
            raise BacktestError("provider returned look-ahead or unsafe bars")
        return tuple(sorted(selected, key=lambda item: item.session_date))
