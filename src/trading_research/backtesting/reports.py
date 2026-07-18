"""Deterministic reporting helpers for historical control runs."""
from __future__ import annotations

from decimal import Decimal

from .models import BacktestDailyState, BacktestFill


def build_report(
    *, initial_cash: Decimal, daily_states: tuple[BacktestDailyState, ...],
    fills: tuple[BacktestFill, ...], rejected_entries: tuple[dict, ...],
    benchmark_return: Decimal | None, unresolved_count: int,
) -> dict:
    ending = daily_states[-1].equity if daily_states else initial_cash
    sells = [fill for fill in fills if fill.side == "SELL"]
    exit_counts: dict[str, int] = {}
    for fill in sells:
        key = fill.exit_reason or "UNKNOWN"
        exit_counts[key] = exit_counts.get(key, 0) + 1
    return {
        "initial_equity": initial_cash,
        "ending_equity": ending,
        "total_return": (ending - initial_cash) / initial_cash,
        "benchmark_return": benchmark_return,
        "maximum_drawdown": min(
            (state.drawdown_fraction for state in daily_states), default=Decimal("0")
        ),
        "realized_pnl": daily_states[-1].realized_pnl if daily_states else Decimal("0"),
        "completed_trades": len(sells),
        "stop_exits": sum(count for reason, count in exit_counts.items() if "STOP" in reason),
        "trailing_stop_exits": exit_counts.get("TRAILING_STOP", 0),
        "breakeven_exits": exit_counts.get("BREAKEVEN_STOP", 0),
        "partial_profit_fills": exit_counts.get("PARTIAL_PROFIT", 0),
        "final_target_exits": exit_counts.get("FINAL_TARGET", 0),
        "daily_loss_blocks": sum(1 for row in rejected_entries if row["reason"] == "DAILY_LOSS_LIMIT"),
        "drawdown_blocks": sum(1 for row in rejected_entries if row["reason"] == "DRAWDOWN_LIMIT"),
        "economic_event_blocks": sum(1 for row in rejected_entries if row["reason"] == "ECONOMIC_EVENT_BLACKOUT"),
        "rejected_signals": len(rejected_entries),
        "unresolved_or_incomplete_evaluations": unresolved_count,
    }
