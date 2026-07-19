import ast
from decimal import Decimal

from trading_research.models.trading_models import PortfolioState
from trading_research.strategies.config import load_strategy_config
from trading_research.strategies.contracts import StrategySignal, StrategyStatus
from trading_research.strategies.selector import select_shortlist

from tests.unit._strategy_test_helpers import NOW

SHORTLIST_CONFIG = load_strategy_config().shortlist


def _signal(symbol: str, strength: float, status: StrategyStatus = StrategyStatus.ELIGIBLE, strategy_id: str = "momentum_breakout") -> StrategySignal:
    return StrategySignal(
        strategy_id=strategy_id, strategy_version="1.0.0", symbol=symbol,
        signal_timestamp=NOW, data_as_of=NOW, status=status, signal_strength=strength,
        entry_reference=None, limit_reference=None, invalidation_price=None,
        initial_stop_reference=None, target_reference=None, expected_holding_period=10,
        reason_codes=("ok",), factor_values={"x": 1.0}, data_quality="complete",
        configuration_hash="cfgabc",
    )


def test_per_strategy_cap_is_enforced():
    from dataclasses import replace
    config = replace(SHORTLIST_CONFIG, maximum_candidates_per_strategy=2, maximum_combined_shortlist=10)
    signals = {"momentum_breakout": tuple(_signal(f"S{i}", 1.0 - i * 0.01) for i in range(5))}
    result = select_shortlist(signals, config)
    assert len(result.entries) == 2
    assert result.symbols == ("S0", "S1")


def test_combined_cap_is_enforced():
    from dataclasses import replace
    config = replace(SHORTLIST_CONFIG, maximum_candidates_per_strategy=10, maximum_combined_shortlist=3)
    signals = {"momentum_breakout": tuple(_signal(f"S{i}", 1.0 - i * 0.01) for i in range(5))}
    result = select_shortlist(signals, config)
    assert len(result.entries) == 3
    assert len(result.excluded) == 2
    assert all(e.reason == "combined_shortlist_cap_reached" for e in result.excluded)


def test_symbol_deduplication_retains_all_strategy_signals():
    signals = {
        "momentum_breakout": (_signal("AAA", 0.8, strategy_id="momentum_breakout"),),
        "mean_reversion": (_signal("AAA", 0.6, strategy_id="mean_reversion"),),
    }
    result = select_shortlist(signals, SHORTLIST_CONFIG)
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.symbol == "AAA"
    assert {s.strategy_id for s in entry.strategy_signals} == {"momentum_breakout", "mean_reversion"}
    assert entry.best_signal_strength == 0.8


def test_only_eligible_signals_are_considered():
    signals = {"momentum_breakout": (_signal("AAA", 0.9, status=StrategyStatus.NOT_ELIGIBLE),)}
    result = select_shortlist(signals, SHORTLIST_CONFIG)
    assert result.entries == ()


def test_symbol_at_allocation_cap_is_excluded():
    """Milestone 24 Part C6: a symbol already at its per-symbol allocation
    cap must be excluded even though the rest of the account has room —
    the previous gate only checked total portfolio exposure and would have
    let this symbol through."""
    portfolio = PortfolioState(
        account_equity=Decimal("10000"), settled_cash=Decimal("100"),
        symbol_exposure_fraction={"AAA": SHORTLIST_CONFIG.maximum_symbol_allocation_fraction},
    )
    signals = {"momentum_breakout": (_signal("AAA", 0.9),)}
    result = select_shortlist(signals, SHORTLIST_CONFIG, portfolio=portfolio)
    assert result.entries == ()
    assert result.excluded[0].reason == "symbol_allocation_cap_reached"


def test_symbol_with_allocation_room_is_included():
    portfolio = PortfolioState(
        account_equity=Decimal("10000"), settled_cash=Decimal("100"),
        symbol_exposure_fraction={"AAA": 0.0},
    )
    signals = {"momentum_breakout": (_signal("AAA", 0.9),)}
    result = select_shortlist(signals, SHORTLIST_CONFIG, portfolio=portfolio)
    assert result.symbols == ("AAA",)


def test_zero_llm_calls_during_scanning():
    """The selector module must not import anything from research/ or any provider package."""
    import trading_research.strategies.selector as selector_module

    tree = ast.parse(open(selector_module.__file__).read())
    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)
        elif isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
    assert not any("research" in m or "evidence_providers" in m for m in imported_modules)
