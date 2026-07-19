"""Shared point-in-time `data_as_of` derivation for strategies (Milestone 24
Part B1).

A strategy's persisted `data_as_of` must reflect the actual availability of
the source data it used to decide — never `context.now` (the evaluation
clock) substituted in its place. `context.now` remains `signal_timestamp`,
which is a separate field.
"""
from __future__ import annotations

from datetime import datetime

from ..backtesting.models import HistoricalBar


def bar_series_data_as_of(
    bars: tuple[HistoricalBar, ...], now: datetime,
) -> tuple[datetime | None, str | None]:
    """Returns `(data_as_of, None)` derived from the latest input bar's
    `available_at`, or `(None, reason_code)` when that bar would introduce
    future information (its `available_at` is after `now`)."""
    latest = bars[-1]
    if latest.available_at > now:
        return None, f"future_bar_available_at:{latest.session_date.isoformat()}"
    return latest.available_at, None
