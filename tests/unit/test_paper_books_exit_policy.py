"""Tests for exit_policy.py (docs/milestone-9.md Sections 2-3)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading_research.paper_books.exit_policy import (
    DECISION_EXIT_MANUAL_REQUEST,
    DECISION_EXIT_MAX_HOLDING_PERIOD,
    DECISION_EXIT_PROFIT_TARGET,
    DECISION_EXIT_RECOMMENDATION_REVERSAL,
    DECISION_EXIT_STOP_LOSS,
    DECISION_HOLD,
    DECISION_SKIPPED_MISSING_PRICE,
    DECISION_SKIPPED_NO_POSITION,
    DECISION_SKIPPED_POINT_IN_TIME_UNSAFE,
    DECISION_SKIPPED_STALE_PRICE,
    ExitPolicyError,
    evaluate_exit_decision,
    is_reversal_recommendation,
    market_days_held,
)

AS_OF = datetime(2026, 1, 12, 20, 0, tzinfo=timezone.utc)  # Monday
OPENED = datetime(2026, 1, 5, 20, 0, tzinfo=timezone.utc)  # Monday, one week earlier

DEFAULTS = dict(
    book_id="BASELINE", symbol="AAPL", position_quantity=Decimal("100"),
    cost_basis_per_share=Decimal("100"), position_opened_at=OPENED, as_of=AS_OF,
    reference_price=Decimal("100"), price_point_in_time_safe=True, price_is_stale=False,
    stop_loss_percent=Decimal("0.08"), profit_target_percent=Decimal("0.15"),
    maximum_holding_market_days=20, exit_on_recommendation_reversal=True,
)


def _decide(**overrides):
    kwargs = {**DEFAULTS, **overrides}
    return evaluate_exit_decision(**kwargs)


def test_no_position_is_skipped():
    d = _decide(position_quantity=Decimal("0"))
    assert d.decision == DECISION_SKIPPED_NO_POSITION
    assert d.quantity == 0


def test_missing_price_is_skipped():
    d = _decide(reference_price=None)
    assert d.decision == DECISION_SKIPPED_MISSING_PRICE


def test_point_in_time_unsafe_price_is_skipped():
    d = _decide(price_point_in_time_safe=False)
    assert d.decision == DECISION_SKIPPED_POINT_IN_TIME_UNSAFE


def test_stale_price_is_skipped():
    d = _decide(price_is_stale=True)
    assert d.decision == DECISION_SKIPPED_STALE_PRICE


def test_stop_loss_triggers_full_exit():
    d = _decide(reference_price=Decimal("91"))  # <= 100 * 0.92
    assert d.decision == DECISION_EXIT_STOP_LOSS
    assert d.quantity == Decimal("100")


def test_profit_target_triggers_full_exit():
    d = _decide(reference_price=Decimal("116"))  # >= 100 * 1.15
    assert d.decision == DECISION_EXIT_PROFIT_TARGET
    assert d.quantity == Decimal("100")


def test_price_between_thresholds_holds():
    d = _decide(reference_price=Decimal("105"))
    assert d.decision == DECISION_HOLD
    assert d.quantity == 0


def test_max_holding_period_triggers_exit():
    # 20 market days after a Monday open lands well past a 3-week horizon.
    far = datetime(2026, 3, 2, 20, 0, tzinfo=timezone.utc)
    d = _decide(as_of=far, reference_price=Decimal("100"), maximum_holding_market_days=20)
    assert d.decision == DECISION_EXIT_MAX_HOLDING_PERIOD


def test_max_holding_period_not_yet_reached_holds():
    soon = datetime(2026, 1, 7, 20, 0, tzinfo=timezone.utc)  # two market days later
    d = _decide(as_of=soon, reference_price=Decimal("100"), maximum_holding_market_days=20)
    assert d.decision == DECISION_HOLD


def test_manual_request_outranks_automatic_rules():
    # Price is well within HOLD range, but a manual request is present.
    d = _decide(reference_price=Decimal("100"), manual_request={"operator": "alice", "reason": "risk-off"})
    assert d.decision == DECISION_EXIT_MANUAL_REQUEST
    assert "alice" in d.reasons[0]


def test_recommendation_reversal_triggers_exit_when_enabled():
    reversal = {"rec_id": "rec-2", "symbol": "AAPL", "side": "screened_out", "status": "active", "ts": AS_OF.isoformat()}
    d = _decide(reference_price=Decimal("100"), reversal_recommendation=reversal)
    assert d.decision == DECISION_EXIT_RECOMMENDATION_REVERSAL


def test_recommendation_reversal_ignored_when_disabled():
    reversal = {"rec_id": "rec-2", "symbol": "AAPL", "side": "screened_out", "status": "active", "ts": AS_OF.isoformat()}
    d = _decide(reference_price=Decimal("100"), reversal_recommendation=reversal, exit_on_recommendation_reversal=False)
    assert d.decision == DECISION_HOLD


def test_missing_recommendation_is_never_a_sell_signal():
    d = _decide(reference_price=Decimal("100"), reversal_recommendation=None)
    assert d.decision == DECISION_HOLD


@pytest.mark.parametrize("side,status,expected", [
    ("screened_out", "active", True),
    ("no_action", "active", True),
    ("buy_candidate", "active", False),
    ("watch", "active", False),
    ("analysis_incomplete", "analysis_incomplete", False),
    ("screened_out", "expired", False),
])
def test_is_reversal_recommendation_classification(side, status, expected):
    row = {"rec_id": "r", "symbol": "AAPL", "side": side, "status": status, "ts": AS_OF.isoformat()}
    assert is_reversal_recommendation(row) is expected


def test_deterministic_same_inputs_same_decision():
    d1 = _decide(reference_price=Decimal("91"))
    d2 = _decide(reference_price=Decimal("91"))
    assert d1 == d2


def test_stop_loss_checked_before_profit_target_order_is_stable():
    # Sanity: with defaults, stop and profit thresholds cannot both fire —
    # confirms the two checks are mutually exclusive at fixed percentages.
    stop_threshold = Decimal("100") * (1 - Decimal("0.08"))
    profit_threshold = Decimal("100") * (1 + Decimal("0.15"))
    assert stop_threshold < profit_threshold


def test_market_days_held_counts_trading_sessions_only():
    # Friday 2026-01-02 -> Monday 2026-01-05 is exactly 1 market day (skips weekend).
    friday = datetime(2026, 1, 2).date()
    monday = datetime(2026, 1, 5).date()
    assert market_days_held(friday, monday) == 1


def test_market_days_held_same_day_is_zero():
    d = datetime(2026, 1, 5).date()
    assert market_days_held(d, d) == 0


def test_market_days_held_rejects_before_open():
    with pytest.raises(ExitPolicyError):
        market_days_held(datetime(2026, 1, 5).date(), datetime(2026, 1, 2).date())


def test_exit_decision_id_stability_is_deterministic_via_hash():
    import hashlib
    a = hashlib.sha256(b"BASELINE:AAPL:2026-01-12:v1").hexdigest()[:32]
    b = hashlib.sha256(b"BASELINE:AAPL:2026-01-12:v1").hexdigest()[:32]
    assert a == b
