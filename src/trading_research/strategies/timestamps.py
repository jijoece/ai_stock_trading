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
    """Returns `(data_as_of, None)` derived from `max(bar.available_at for
    bar in bars)`, or `(None, reason_code)` when any bar in the series would
    introduce future information (its `available_at` is after `now`).

    Milestone 25 Part B1: availability timestamps are not assumed monotonic
    merely because session dates are ordered — a later correction to an
    older bar can carry a newer `available_at` than the series' last bar,
    so every bar actually used must be checked, not just the latest one."""
    future = [bar for bar in bars if bar.available_at > now]
    if future:
        offender = max(future, key=lambda bar: bar.available_at)
        return None, f"future_bar_available_at:{offender.session_date.isoformat()}"
    return max(bar.available_at for bar in bars), None
