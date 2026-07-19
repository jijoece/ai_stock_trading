from trading_research.strategies.contracts import StrategyStatus
from trading_research.strategies.safety_gates import classify_safety_status, sufficient_bar_history

from tests.unit._strategy_test_helpers import (
    build_bars,
    failing_screening_result,
    passing_screening_result,
    stale_screening_result,
)


def test_sufficient_bar_history_true_when_enough_bars():
    bars = build_bars([10] * 30)
    assert sufficient_bar_history(bars, 30) is True
    assert sufficient_bar_history(bars, 31) is False


def test_passes_through_when_all_gates_pass_and_history_sufficient():
    bars = build_bars([10] * 30)
    result = classify_safety_status(passing_screening_result(), bars, minimum_bars=20)
    assert result is None


def test_insufficient_bar_history_is_incomplete():
    bars = build_bars([10] * 5)
    status, reasons = classify_safety_status(passing_screening_result(), bars, minimum_bars=20)
    assert status == StrategyStatus.INCOMPLETE
    assert any("insufficient_bar_history" in r for r in reasons)


def test_screener_staleness_gate_maps_to_stale():
    bars = build_bars([10] * 30)
    status, reasons = classify_safety_status(stale_screening_result(), bars, minimum_bars=20)
    assert status == StrategyStatus.STALE
    assert any("stale_data" in r for r in reasons)


def test_other_screener_gate_failure_maps_to_not_eligible():
    bars = build_bars([10] * 30)
    status, reasons = classify_safety_status(failing_screening_result(gate_name="min_market_cap"), bars, minimum_bars=20)
    assert status == StrategyStatus.NOT_ELIGIBLE
    assert any("min_market_cap" in r for r in reasons)


def test_does_not_reimplement_a_second_screener():
    """The safety-gate module only maps `ScreeningResult` outcomes plus bar
    history — it does not itself evaluate market-cap/liquidity/etc gates."""
    import inspect
    from trading_research.strategies import safety_gates

    source = inspect.getsource(safety_gates)
    assert "min_market_cap" not in source
    assert "min_avg_daily_dollar_volume" not in source
