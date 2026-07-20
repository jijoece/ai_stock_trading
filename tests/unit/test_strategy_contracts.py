from dataclasses import fields, replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading_research.strategies.contracts import (
    StrategyContext,
    StrategyContractError,
    StrategyMarketData,
    StrategySignal,
    StrategyStatus,
    derive_strategy_evaluation_id,
    derive_strategy_signal_content_id,
)

from tests.unit._strategy_test_helpers import build_bars, passing_screening_result

NOW = datetime(2026, 7, 11, 21, 0, 0, tzinfo=timezone.utc)


def make_signal(**overrides) -> StrategySignal:
    defaults = dict(
        strategy_id="momentum_breakout",
        strategy_version="1.0.0",
        symbol="TEST",
        signal_timestamp=NOW,
        data_as_of=NOW,
        status=StrategyStatus.ELIGIBLE,
        signal_strength=0.5,
        entry_reference=Decimal("100"),
        limit_reference=Decimal("100"),
        invalidation_price=Decimal("95"),
        initial_stop_reference=Decimal("95"),
        target_reference=None,
        expected_holding_period=20,
        reason_codes=("breakout_confirmed",),
        factor_values={"volume_ratio": 1.8},
        data_quality="complete",
        configuration_hash="abc123",
    )
    defaults.update(overrides)
    return StrategySignal(**defaults)


def test_status_enum_has_exactly_four_values():
    assert {s.value for s in StrategyStatus} == {"ELIGIBLE", "NOT_ELIGIBLE", "INCOMPLETE", "STALE"}


def test_signal_has_no_free_form_text_field():
    field_names = {f.name for f in fields(StrategySignal)}
    assert "notes" not in field_names
    assert "commentary" not in field_names
    assert "llm_summary" not in field_names


def test_signal_requires_timezone_aware_timestamp():
    with pytest.raises(StrategyContractError):
        make_signal(signal_timestamp=datetime(2026, 7, 11, 21, 0, 0))


def test_signal_strength_must_be_within_unit_interval():
    with pytest.raises(StrategyContractError):
        make_signal(signal_strength=1.5)


def test_eligible_signal_requires_reason_codes():
    with pytest.raises(StrategyContractError):
        make_signal(reason_codes=())


def test_factor_values_must_be_numeric():
    with pytest.raises(StrategyContractError):
        make_signal(factor_values={"note": "looks good"})


def test_market_data_bars_must_be_ordered_oldest_to_newest():
    bars = build_bars([10, 11, 12])
    with pytest.raises(StrategyContractError):
        StrategyMarketData(symbol="TEST", bars=tuple(reversed(bars)))


def test_context_requires_timezone_aware_now():
    with pytest.raises(StrategyContractError):
        StrategyContext(now=datetime(2026, 7, 11), screening_result=passing_screening_result())


def test_eligible_signal_requires_positive_entry_and_stop_fields():
    with pytest.raises(StrategyContractError):
        make_signal(entry_reference=None)
    with pytest.raises(StrategyContractError):
        make_signal(initial_stop_reference=None)
    with pytest.raises(StrategyContractError):
        make_signal(invalidation_price=None)
    with pytest.raises(StrategyContractError):
        make_signal(data_as_of=None)


def test_eligible_signal_stop_must_be_below_entry():
    with pytest.raises(StrategyContractError):
        make_signal(entry_reference=Decimal("100"), initial_stop_reference=Decimal("100"))
    with pytest.raises(StrategyContractError):
        make_signal(entry_reference=Decimal("100"), initial_stop_reference=Decimal("105"))


def test_eligible_signal_target_must_exceed_entry_when_present():
    with pytest.raises(StrategyContractError):
        make_signal(entry_reference=Decimal("100"), target_reference=Decimal("100"))
    with pytest.raises(StrategyContractError):
        make_signal(entry_reference=Decimal("100"), target_reference=Decimal("90"))
    # A valid target above entry is fine.
    signal = make_signal(entry_reference=Decimal("100"), target_reference=Decimal("110"))
    assert signal.target_reference == Decimal("110")


def test_non_eligible_signal_may_omit_execution_fields():
    signal = make_signal(
        status=StrategyStatus.NOT_ELIGIBLE, entry_reference=None, limit_reference=None,
        invalidation_price=None, initial_stop_reference=None, data_as_of=None,
    )
    assert signal.status == StrategyStatus.NOT_ELIGIBLE


def test_identical_signals_produce_identical_content_and_evaluation_ids():
    """Milestone 25 Part B10."""
    a = make_signal()
    b = make_signal()
    assert derive_strategy_signal_content_id(a) == derive_strategy_signal_content_id(b)
    assert derive_strategy_evaluation_id(a) == derive_strategy_evaluation_id(b)


def test_content_id_changes_when_execution_relevant_factors_change():
    base = make_signal()
    base_id = derive_strategy_signal_content_id(base)
    variants = (
        replace(base, entry_reference=Decimal("101")),
        replace(base, limit_reference=Decimal("101")),
        replace(base, initial_stop_reference=Decimal("90")),
        replace(base, invalidation_price=Decimal("90")),
        replace(base, target_reference=Decimal("120")),
        replace(base, expected_holding_period=21),
        replace(base, reason_codes=("breakout_confirmed", "volume_confirmed")),
        replace(base, factor_values={"volume_ratio": 2.5}),
        replace(base, configuration_hash="different-hash"),
        replace(base, data_as_of=NOW.replace(hour=22)),
        replace(base, status=StrategyStatus.NOT_ELIGIBLE, entry_reference=None, limit_reference=None,
                invalidation_price=None, initial_stop_reference=None),
    )
    for variant in variants:
        assert derive_strategy_signal_content_id(variant) != base_id


def test_content_id_is_unchanged_by_evaluation_time_but_evaluation_id_changes():
    """Milestone 25 Part B10: an unchanged signal re-evaluated at a
    different signal_timestamp keeps the same content ID but gets a
    different evaluation ID."""
    base = make_signal()
    later = replace(base, signal_timestamp=NOW.replace(hour=23))
    assert derive_strategy_signal_content_id(base) == derive_strategy_signal_content_id(later)
    assert derive_strategy_evaluation_id(base) != derive_strategy_evaluation_id(later)


def test_evaluation_id_changes_when_content_changes():
    base = make_signal()
    variant = replace(base, entry_reference=Decimal("101"))
    assert derive_strategy_evaluation_id(base) != derive_strategy_evaluation_id(variant)
