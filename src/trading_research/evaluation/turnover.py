"""Turnover metrics (docs/milestone-6.md Step 17).

Turnover is explicitly defined here as **executed notional traded over a
period, divided by average portfolio equity over that same period** — a
documented denominator, not left implicit. `fees`/`slippage` attribution is
kept as a separate, explicit field rather than netted silently into the
ratio, so a caller can report either "gross turnover" or "turnover net of
transaction cost" without this module guessing which one is wanted.

Because the current experiment policy (`SHADOW_ENHANCED`) never submits the
enhanced arm to paper execution, "turnover by arm" for the enhanced arm is
expected to be `INSUFFICIENT_DATA` (zero executed fills), not a bug — see
docs/milestone6-real-evidence-continuous-evaluation.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

STATUS_OK = "OK"
STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True)
class TurnoverResult:
    status: str
    value: Decimal | None
    executed_notional: Decimal
    fees_and_slippage: Decimal
    average_equity: Decimal | None
    sample_size: int
    reason: str | None = None


def compute_turnover(*, executed_notional: Decimal, average_equity: Decimal | None, fees_and_slippage: Decimal = Decimal("0"), sample_size: int = 1) -> TurnoverResult:
    if average_equity is None or average_equity <= 0:
        return TurnoverResult(
            status=STATUS_INSUFFICIENT_DATA, value=None, executed_notional=executed_notional,
            fees_and_slippage=fees_and_slippage, average_equity=average_equity, sample_size=sample_size,
            reason="average_equity is unavailable or non-positive — turnover denominator undefined",
        )
    return TurnoverResult(
        status=STATUS_OK, value=executed_notional / average_equity, executed_notional=executed_notional,
        fees_and_slippage=fees_and_slippage, average_equity=average_equity, sample_size=sample_size,
    )


@dataclass(frozen=True)
class Fill:
    """Minimal shape this module needs from a fill record — deliberately not
    importing `execution.models.PaperExecutionEvent` to keep this a pure,
    dependency-light module; callers map their own fill rows into this."""

    occurred_at_date: date
    notional: Decimal
    fees: Decimal = Decimal("0")


@dataclass(frozen=True)
class EquitySnapshot:
    snap_date: date
    equity: Decimal


def daily_turnover(fills: list[Fill], snapshots: list[EquitySnapshot]) -> dict[date, TurnoverResult]:
    equity_by_date = {s.snap_date: s.equity for s in snapshots}
    notional_by_date: dict[date, Decimal] = {}
    fees_by_date: dict[date, Decimal] = {}
    for f in fills:
        notional_by_date[f.occurred_at_date] = notional_by_date.get(f.occurred_at_date, Decimal("0")) + f.notional
        fees_by_date[f.occurred_at_date] = fees_by_date.get(f.occurred_at_date, Decimal("0")) + f.fees

    results: dict[date, TurnoverResult] = {}
    for d, notional in notional_by_date.items():
        results[d] = compute_turnover(
            executed_notional=notional, average_equity=equity_by_date.get(d), fees_and_slippage=fees_by_date.get(d, Decimal("0")),
        )
    return results


def rolling_turnover(daily_results: dict[date, TurnoverResult], *, min_sample_size: int = 3) -> TurnoverResult:
    ok_days = [r for r in daily_results.values() if r.status == STATUS_OK]
    if len(ok_days) < min_sample_size:
        return TurnoverResult(
            status=STATUS_INSUFFICIENT_DATA, value=None, executed_notional=sum((r.executed_notional for r in daily_results.values()), Decimal("0")),
            fees_and_slippage=sum((r.fees_and_slippage for r in daily_results.values()), Decimal("0")),
            average_equity=None, sample_size=len(ok_days),
            reason=f"need at least {min_sample_size} days with a defined daily turnover",
        )
    total_notional = sum((r.executed_notional for r in ok_days), Decimal("0"))
    total_fees = sum((r.fees_and_slippage for r in ok_days), Decimal("0"))
    average_equity = sum((r.average_equity for r in ok_days), Decimal("0")) / len(ok_days)
    return compute_turnover(executed_notional=total_notional, average_equity=average_equity, fees_and_slippage=total_fees, sample_size=len(ok_days))


def turnover_by_arm(baseline_fills: list[Fill], enhanced_fills: list[Fill], snapshots: list[EquitySnapshot]) -> dict[str, TurnoverResult]:
    baseline_daily = daily_turnover(baseline_fills, snapshots)
    enhanced_daily = daily_turnover(enhanced_fills, snapshots)
    return {
        "baseline": rolling_turnover(baseline_daily, min_sample_size=1) if baseline_daily else TurnoverResult(
            status=STATUS_INSUFFICIENT_DATA, value=None, executed_notional=Decimal("0"), fees_and_slippage=Decimal("0"),
            average_equity=None, sample_size=0, reason="no baseline fills recorded",
        ),
        "enhanced": rolling_turnover(enhanced_daily, min_sample_size=1) if enhanced_daily else TurnoverResult(
            status=STATUS_INSUFFICIENT_DATA, value=None, executed_notional=Decimal("0"), fees_and_slippage=Decimal("0"),
            average_equity=None, sample_size=0, reason="enhanced arm does not execute under the current experiment policy (shadow-only)",
        ),
    }
