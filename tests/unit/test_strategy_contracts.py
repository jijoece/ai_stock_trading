from dataclasses import fields
from datetime import datetime, timezone

import pytest

from trading_research.strategies.contracts import (
    StrategyContext,
    StrategyContractError,
    StrategyMarketData,
    StrategySignal,
    StrategyStatus,
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
        entry_reference=None,
        limit_reference=None,
        invalidation_price=None,
        initial_stop_reference=None,
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
