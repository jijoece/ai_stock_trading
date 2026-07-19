import ast
from pathlib import Path

import pytest

from trading_research.strategies.config import load_strategy_config
from trading_research.strategies.contracts import StrategyContext, StrategyContractError, StrategyMarketData, StrategyStatus
from trading_research.strategies.momentum_breakout import MomentumBreakoutStrategy

from tests.unit._strategy_test_helpers import NOW, build_bars, failing_screening_result, passing_screening_result

CONFIG = load_strategy_config().momentum_breakout
STRATEGY = MomentumBreakoutStrategy(CONFIG)


def test_strategy_cannot_bypass_screener_hard_gate_failure():
    bars = build_bars([50.0 + 0.05 * i for i in range(79)] + [55.0], volumes=[3_000_000] * 80)
    market_data = StrategyMarketData(symbol="TEST", bars=bars)
    context = StrategyContext(now=NOW, screening_result=failing_screening_result(gate_name="min_market_cap"))
    signal = STRATEGY.evaluate("TEST", market_data, context)
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert any("min_market_cap" in r for r in signal.reason_codes)


def test_unknown_symbol_with_no_data_fails_closed():
    market_data = StrategyMarketData(symbol="UNKNOWN", bars=())
    context = StrategyContext(now=NOW, screening_result=passing_screening_result(symbol="UNKNOWN"))
    signal = STRATEGY.evaluate("UNKNOWN", market_data, context)
    assert signal.status == StrategyStatus.INCOMPLETE


def test_missing_data_fails_closed_not_favorable():
    market_data = StrategyMarketData(symbol="TEST", bars=build_bars([50.0] * 3))
    context = StrategyContext(now=NOW, screening_result=passing_screening_result())
    signal = STRATEGY.evaluate("TEST", market_data, context)
    assert signal.status != StrategyStatus.ELIGIBLE


def test_signal_strength_out_of_range_is_rejected_at_construction():
    with pytest.raises(StrategyContractError):
        from trading_research.strategies.contracts import StrategySignal
        StrategySignal(
            strategy_id="momentum_breakout", strategy_version="1.0.0", symbol="TEST",
            signal_timestamp=NOW, data_as_of=NOW, status=StrategyStatus.ELIGIBLE,
            signal_strength=2.0,  # a model/researcher trying to inflate strength beyond [0,1] fails closed
            entry_reference=None, limit_reference=None, invalidation_price=None,
            initial_stop_reference=None, target_reference=None, expected_holding_period=10,
            reason_codes=("x",), factor_values={}, data_quality="complete", configuration_hash="h",
        )


def test_strategies_package_has_no_llm_touching_import():
    """No module under strategies/ may import research/ (the LLM committee)
    or any provider adapter — market scanning and ranking must be zero-LLM."""
    package_dir = Path(__file__).resolve().parents[2] / "src" / "trading_research" / "strategies"
    forbidden_substrings = ("research", "evidence_providers", "anthropic", "openai")

    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            for module in modules:
                assert not any(f in module for f in forbidden_substrings), (
                    f"{path.name} imports {module!r}, which touches LLM/provider code"
                )
