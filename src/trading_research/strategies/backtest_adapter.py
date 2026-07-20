"""Per-strategy backtests built on the existing point-in-time engine
(Milestone 23, B8).

Reuses `backtesting.engine.run_backtest` rather than building a second
execution engine: a `StrategySignal` is translated into the same
`EntrySignal` contract the engine already enforces (next-session entry,
limit-order fill, ATR stop/target, transaction costs, maximum holding
period, no future data). Zero LLM calls — this module only imports other
deterministic modules.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..backtesting.configuration import BacktestConfiguration
from ..backtesting.data_provider import HistoricalDataProvider
from ..backtesting.engine import run_backtest
from ..backtesting.models import BacktestError, BacktestResult, EntrySignal
from .contracts import StrategySignal, StrategyStatus, derive_strategy_signal_content_id
from .strategy_metrics import StrategyBacktestMetrics, compute_strategy_metrics

# The shared engine caps quantity by risk-fraction and available cash
# (`min(signal.quantity_hint, risk_qty, cash_qty)`); this ceiling only
# exists so a strategy backtest is never itself the binding constraint.
DEFAULT_QUANTITY_HINT = Decimal("1000000")


def strategy_signal_to_entry_signal(
    signal: StrategySignal, *, quantity_hint: Decimal = DEFAULT_QUANTITY_HINT,
) -> EntrySignal:
    if signal.status != StrategyStatus.ELIGIBLE:
        raise BacktestError(f"only ELIGIBLE signals can be backtested, got {signal.status}")
    if signal.data_as_of is None:
        raise BacktestError("signal is missing data_as_of — cannot anchor a point-in-time entry session")
    limit_price = signal.limit_reference or signal.entry_reference
    if limit_price is None:
        raise BacktestError("signal is missing both limit_reference and entry_reference")
    # Milestone 25 Part B10: the same content-identity ID used by the
    # execution boundary — never a second, independently-derived ID.
    signal_id = derive_strategy_signal_content_id(signal)
    return EntrySignal(
        signal_id=signal_id, symbol=signal.symbol,
        generated_after_session=signal.data_as_of.date(),
        limit_price=limit_price, quantity_hint=quantity_hint,
        # Milestone 24 Part C3: carry the strategy's own exit levels through
        # to the shared engine so it never silently substitutes its generic
        # ATR default for what the strategy actually decided.
        initial_stop_reference=signal.initial_stop_reference,
        target_reference=signal.target_reference,
        maximum_holding_sessions=signal.expected_holding_period,
        strategy_signal_id=signal_id,
    )


@dataclass(frozen=True)
class StrategyBacktestResult:
    strategy_id: str
    backtest_result: BacktestResult
    metrics: StrategyBacktestMetrics


def run_strategy_backtest(
    *, strategy_id: str, signals: tuple[StrategySignal, ...], configuration: BacktestConfiguration,
    data_provider: HistoricalDataProvider, regime_by_date: dict | None = None, conn=None,
) -> StrategyBacktestResult:
    strategy_signals = tuple(
        s for s in signals if s.strategy_id == strategy_id and s.status == StrategyStatus.ELIGIBLE
    )
    # Milestone 24 Part C2: one strategy backtest must not silently blend
    # signals produced under different strategy versions/configurations —
    # a mixed set would make the resulting metrics unattributable to any
    # single, reproducible strategy definition.
    versions = {s.strategy_version for s in strategy_signals}
    config_hashes = {s.configuration_hash for s in strategy_signals}
    if len(versions) > 1 or len(config_hashes) > 1:
        raise BacktestError(
            f"strategy backtest for {strategy_id!r} received mixed strategy definitions: "
            f"versions={sorted(versions)} configuration_hashes={sorted(config_hashes)}"
        )
    strategy_version = next(iter(versions), None)
    strategy_config_hash = next(iter(config_hashes), None)
    entry_signals = tuple(strategy_signal_to_entry_signal(s) for s in strategy_signals)
    result = run_backtest(
        configuration=configuration, data_provider=data_provider, signals=entry_signals, conn=conn,
        strategy_id=strategy_id, strategy_version=strategy_version, strategy_config_hash=strategy_config_hash,
    )
    metrics = compute_strategy_metrics(result, entry_signals, regime_by_date=regime_by_date)
    return StrategyBacktestResult(strategy_id=strategy_id, backtest_result=result, metrics=metrics)
