from decimal import Decimal

import pytest

from trading_research.strategies.contracts import StrategyContractError, StrategySignal, StrategyStatus
from trading_research.strategies.execution_boundary import (
    OverlayDisposition,
    StrategyExecutionBoundaryError,
    apply_overlay_disposition,
    build_strategy_order_intent_context,
)

from tests.unit._strategy_test_helpers import NOW


def _eligible_signal(**overrides) -> StrategySignal:
    fields = dict(
        strategy_id="momentum_breakout", strategy_version="1.0.0", symbol="AAPL",
        signal_timestamp=NOW, data_as_of=NOW, status=StrategyStatus.ELIGIBLE, signal_strength=0.8,
        entry_reference=Decimal("100"), limit_reference=Decimal("101"), invalidation_price=Decimal("95"),
        initial_stop_reference=Decimal("96"), target_reference=Decimal("110"), expected_holding_period=20,
        reason_codes=("breakout_confirmed",), factor_values={"breakout_distance": 1.5},
        data_quality="complete", configuration_hash="cfg-hash",
    )
    fields.update(overrides)
    return StrategySignal(**fields)


def test_eligible_signal_builds_order_intent_context_with_strategy_identity():
    signal = _eligible_signal()
    context = build_strategy_order_intent_context(signal)
    assert context.strategy_id == "momentum_breakout"
    assert context.symbol == "AAPL"
    assert context.expected_holding_period == 20
    assert context.strategy_stop == Decimal("96")
    assert "breakout_confirmed" in context.entry_condition


def test_non_eligible_signal_fails_closed():
    signal = _eligible_signal(status=StrategyStatus.NOT_ELIGIBLE, reason_codes=("no_breakout",))
    with pytest.raises(StrategyExecutionBoundaryError):
        build_strategy_order_intent_context(signal)


def test_eligible_signal_missing_stop_fails_closed():
    # Milestone 24 Part B6: an ELIGIBLE signal missing its stop now fails
    # closed at construction (StrategyContractError), one layer earlier
    # than the execution boundary that used to be the only enforcer.
    with pytest.raises(StrategyContractError):
        _eligible_signal(initial_stop_reference=None)


def test_eligible_signal_missing_invalidation_price_fails_closed():
    with pytest.raises(StrategyContractError):
        _eligible_signal(invalidation_price=None)


def test_overlay_can_only_shrink_size():
    context = build_strategy_order_intent_context(_eligible_signal())
    with pytest.raises(StrategyExecutionBoundaryError):
        apply_overlay_disposition(
            context, OverlayDisposition.ALLOW_ENTRY, requested_size_multiplier=Decimal("1.5"),
        )


def test_overlay_no_action_forces_zero_size_regardless_of_requested_multiplier():
    context = build_strategy_order_intent_context(_eligible_signal())
    result = apply_overlay_disposition(
        context, OverlayDisposition.NO_ACTION, requested_size_multiplier=Decimal("1"),
    )
    assert result.size_multiplier == Decimal("0")


def test_overlay_reduce_size_shrinks_but_does_not_zero():
    context = build_strategy_order_intent_context(_eligible_signal())
    result = apply_overlay_disposition(
        context, OverlayDisposition.REDUCE_SIZE, requested_size_multiplier=Decimal("0.5"),
    )
    assert result.size_multiplier == Decimal("0.5")


def test_overlay_never_edits_the_underlying_context():
    context = build_strategy_order_intent_context(_eligible_signal())
    result = apply_overlay_disposition(context, OverlayDisposition.ALLOW_ENTRY)
    assert result.context is context
    assert result.context.strategy_stop == Decimal("96")


def test_unknown_overlay_disposition_fails_closed():
    context = build_strategy_order_intent_context(_eligible_signal())
    with pytest.raises(StrategyExecutionBoundaryError):
        apply_overlay_disposition(context, "PROMOTE_TO_BUY")  # type: ignore[arg-type]
