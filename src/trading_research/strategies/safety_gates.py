"""Shared strategy safety gates (Milestone 23, B2).

Strategies do not implement a second screener. Every strategy must call
`classify_safety_status` first and only proceed to its own signal logic
when the result is `None` (i.e. no hard-gate failure) — reusing
`analysis.screener.screen_candidate` for every gate the screener already
owns (liquidity, spread, distress, staleness, ...).
"""
from __future__ import annotations

from ..analysis.screener import ScreeningResult
from ..backtesting.models import HistoricalBar
from .contracts import StrategyStatus

STALENESS_GATE = "max_data_staleness_seconds"


def sufficient_bar_history(bars: tuple[HistoricalBar, ...], minimum_bars: int) -> bool:
    return len(bars) >= minimum_bars


def classify_safety_status(
    screening_result: ScreeningResult,
    bars: tuple[HistoricalBar, ...],
    minimum_bars: int,
) -> tuple[StrategyStatus, tuple[str, ...]] | None:
    """Map shared-gate outcomes to a `StrategyStatus`.

    Returns `None` when every shared gate passes and bar history is
    sufficient — the strategy is clear to run its own logic. Otherwise
    returns the terminal `(status, reason_codes)` the strategy must emit
    without further evaluation (fail closed).
    """
    if not sufficient_bar_history(bars, minimum_bars):
        return StrategyStatus.INCOMPLETE, (
            f"insufficient_bar_history:have={len(bars)},need={minimum_bars}",
        )

    failed_gates = [g for g in screening_result.gate_results if not g.passed]
    if not failed_gates:
        return None

    staleness_failure = next((g for g in failed_gates if g.gate == STALENESS_GATE), None)
    if staleness_failure is not None:
        return StrategyStatus.STALE, (f"stale_data:{staleness_failure.reason}",)

    reasons = tuple(f"screener_gate_failed:{g.gate}" for g in failed_gates)
    return StrategyStatus.NOT_ELIGIBLE, reasons
