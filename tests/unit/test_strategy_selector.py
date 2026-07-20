import ast
from datetime import timedelta
from decimal import Decimal

from trading_research.models.trading_models import PortfolioPositionSnapshot, PortfolioState
from trading_research.strategies.config import load_strategy_config
from trading_research.strategies.contracts import StrategySignal, StrategyStatus
from trading_research.strategies.selector import select_shortlist

from tests.unit._strategy_test_helpers import NOW

SHORTLIST_CONFIG = load_strategy_config().shortlist


def _signal(
    symbol: str, strength: float, status: StrategyStatus = StrategyStatus.ELIGIBLE,
    strategy_id: str = "momentum_breakout", data_as_of=NOW,
) -> StrategySignal:
    entry = Decimal("100") if status == StrategyStatus.ELIGIBLE else None
    stop = Decimal("95") if status == StrategyStatus.ELIGIBLE else None
    return StrategySignal(
        strategy_id=strategy_id, strategy_version="1.0.0", symbol=symbol,
        signal_timestamp=NOW, data_as_of=data_as_of, status=status, signal_strength=strength,
        entry_reference=entry, limit_reference=entry, invalidation_price=stop,
        initial_stop_reference=stop, target_reference=None, expected_holding_period=10,
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
        symbol_exposure_complete=True,
    )
    signals = {"momentum_breakout": (_signal("AAA", 0.9),)}
    result = select_shortlist(signals, SHORTLIST_CONFIG, portfolio=portfolio)
    assert result.entries == ()
    assert result.excluded[0].reason == "symbol_allocation_cap_reached"


def test_symbol_with_allocation_room_is_included():
    portfolio = PortfolioState(
        account_equity=Decimal("10000"), settled_cash=Decimal("100"),
        symbol_exposure_fraction={"AAA": 0.0},
        symbol_exposure_complete=True,
    )
    signals = {"momentum_breakout": (_signal("AAA", 0.9),)}
    result = select_shortlist(signals, SHORTLIST_CONFIG, portfolio=portfolio)
    assert result.symbols == ("AAA",)


def test_complete_snapshot_proves_unheld_symbol_has_known_zero_exposure():
    portfolio = PortfolioState(
        account_equity=Decimal("10000"), settled_cash=Decimal("10000"),
        existing_positions={}, symbol_exposure_fraction={}, symbol_exposure_complete=True, as_of=NOW,
    )
    result = select_shortlist({"momentum_breakout": (_signal("AAA", 0.9),)}, SHORTLIST_CONFIG, portfolio)
    assert result.symbols == ("AAA",)


def test_held_symbol_missing_from_complete_exposure_map_is_unknown():
    portfolio = PortfolioState(
        account_equity=Decimal("10000"), settled_cash=Decimal("100"),
        existing_positions={"AAA": 10}, symbol_exposure_fraction={},
        symbol_exposure_complete=True, as_of=NOW,
    )
    result = select_shortlist({"momentum_breakout": (_signal("AAA", 0.9),)}, SHORTLIST_CONFIG, portfolio)
    assert result.entries == ()
    assert result.excluded[0].reason == "symbol_exposure_unknown"


def test_incomplete_exposure_snapshot_is_excluded_before_research():
    portfolio = PortfolioState(
        account_equity=Decimal("10000"), settled_cash=Decimal("100"),
        symbol_exposure_fraction={"AAA": 0.0}, symbol_exposure_complete=False, as_of=NOW,
    )
    result = select_shortlist({"momentum_breakout": (_signal("AAA", 0.9),)}, SHORTLIST_CONFIG, portfolio)
    assert result.entries == ()
    assert result.excluded[0].reason == "symbol_exposure_unknown"


def test_missing_account_equity_is_not_favorable_zero_exposure():
    portfolio = PortfolioState(
        account_equity=None, settled_cash=Decimal("100"),
        symbol_exposure_fraction={"AAA": 0.0}, symbol_exposure_complete=True, as_of=NOW,
    )
    result = select_shortlist({"momentum_breakout": (_signal("AAA", 0.9),)}, SHORTLIST_CONFIG, portfolio)
    assert result.entries == ()
    assert result.excluded[0].reason == "symbol_exposure_unknown"


def test_portfolio_builder_populates_point_in_time_symbol_exposure():
    portfolio = PortfolioState.from_position_snapshots(
        account_equity=Decimal("10000"), settled_cash=Decimal("5000"),
        positions={
            "AAA": PortfolioPositionSnapshot(
                quantity=5, market_price=Decimal("100"), price_as_of=NOW - timedelta(seconds=30),
            ),
        },
        as_of=NOW, maximum_price_age_seconds=60,
    )
    assert portfolio.symbol_exposure_complete is True
    assert portfolio.symbol_exposure_fraction == {"AAA": 0.05}
    assert portfolio.portfolio_exposure_fraction == 0.05


def test_stale_or_missing_position_price_keeps_exposure_incomplete():
    for position in (
        PortfolioPositionSnapshot(quantity=5, market_price=None, price_as_of=NOW),
        PortfolioPositionSnapshot(
            quantity=5, market_price=Decimal("100"), price_as_of=NOW - timedelta(seconds=61),
        ),
    ):
        portfolio = PortfolioState.from_position_snapshots(
            account_equity=Decimal("10000"), settled_cash=Decimal("5000"), positions={"AAA": position},
            as_of=NOW, maximum_price_age_seconds=60,
        )
        assert portfolio.symbol_exposure_complete is False
        result = select_shortlist(
            {"momentum_breakout": (_signal("AAA", 0.9),)}, SHORTLIST_CONFIG, portfolio,
        )
        assert result.excluded[0].reason == "symbol_exposure_unknown"


def test_freshness_tie_break_prefers_newer_data_as_of():
    """Milestone 24 Part B2: two otherwise-identical eligible signals (same
    strength, same data_quality) for different symbols — the one with the
    newer `data_as_of` must rank ahead in the combined shortlist."""
    from dataclasses import replace
    config = replace(SHORTLIST_CONFIG, maximum_candidates_per_strategy=10, maximum_combined_shortlist=1)
    older = _signal("OLD", 0.7, data_as_of=NOW - timedelta(days=1))
    newer = _signal("NEW", 0.7, data_as_of=NOW)
    signals = {"momentum_breakout": (older, newer)}
    result = select_shortlist(signals, config)
    assert result.symbols == ("NEW",)


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
