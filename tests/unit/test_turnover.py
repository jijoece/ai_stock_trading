"""Unit tests for evaluation/turnover.py — Milestone 6 docs/milestone-6.md
Step 22 category J (turnover)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from trading_research.evaluation.turnover import (
    EquitySnapshot,
    Fill,
    STATUS_INSUFFICIENT_DATA,
    STATUS_OK,
    compute_turnover,
    daily_turnover,
    rolling_turnover,
    turnover_by_arm,
)


def test_compute_turnover_basic_ratio():
    result = compute_turnover(executed_notional=Decimal("10000"), average_equity=Decimal("100000"))
    assert result.status == STATUS_OK
    assert result.value == Decimal("0.1")


def test_compute_turnover_undefined_denominator_is_insufficient_data():
    result = compute_turnover(executed_notional=Decimal("10000"), average_equity=None)
    assert result.status == STATUS_INSUFFICIENT_DATA
    result2 = compute_turnover(executed_notional=Decimal("10000"), average_equity=Decimal("0"))
    assert result2.status == STATUS_INSUFFICIENT_DATA


def test_daily_turnover_groups_by_date():
    fills = [
        Fill(occurred_at_date=date(2026, 7, 1), notional=Decimal("1000")),
        Fill(occurred_at_date=date(2026, 7, 1), notional=Decimal("500")),
        Fill(occurred_at_date=date(2026, 7, 2), notional=Decimal("2000")),
    ]
    snapshots = [EquitySnapshot(date(2026, 7, 1), Decimal("10000")), EquitySnapshot(date(2026, 7, 2), Decimal("10000"))]
    results = daily_turnover(fills, snapshots)
    assert results[date(2026, 7, 1)].value == Decimal("0.15")
    assert results[date(2026, 7, 2)].value == Decimal("0.2")


def test_rolling_turnover_requires_minimum_sample():
    result = rolling_turnover({}, min_sample_size=3)
    assert result.status == STATUS_INSUFFICIENT_DATA


def test_turnover_by_arm_enhanced_arm_is_insufficient_when_never_executes():
    baseline_fills = [Fill(occurred_at_date=date(2026, 7, 1), notional=Decimal("1000"))]
    snapshots = [EquitySnapshot(date(2026, 7, 1), Decimal("10000"))]
    result = turnover_by_arm(baseline_fills, [], snapshots)
    assert result["baseline"].status == STATUS_OK
    assert result["enhanced"].status == STATUS_INSUFFICIENT_DATA
    assert "shadow" in result["enhanced"].reason.lower()
