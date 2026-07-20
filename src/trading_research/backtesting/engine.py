"""Daily-bar backtest engine dedicated to deterministic risk controls.

Signals are generated after a close and become eligible only on the next
session. Positions that existed at a bar's open are checked against that
bar's range. If stop and favorable exits are both touched, the stop wins.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import ROUND_FLOOR, Decimal

from ..analysis.indicators import OHLCBar, average_true_range, atr_risk_levels
from ..evidence_providers.economic_calendar import evaluate_economic_event_blackout
from ..hashing import hash_config
from ..paper_books.lifecycle_state import calculate_partial_close_quantity
from .configuration import BacktestConfiguration
from .data_provider import HistoricalDataProvider
from .models import (
    BacktestDailyState, BacktestError, BacktestFill, BacktestResult,
    EntrySignal, HistoricalBar,
)
from .reports import build_report

BACKTEST_CODE_VERSION = "advanced-risk-backtest-v2-fill-sequence"


@dataclass
class _Position:
    position_id: str
    symbol: str
    quantity: Decimal
    original_quantity: Decimal
    entry_price: Decimal
    entry_date: object
    entry_session_index: int
    entry_atr: Decimal
    stop: Decimal
    target: Decimal
    highest: Decimal
    maximum_holding_sessions: int
    partial_stage: int = 0


def _signal_set_hash(signals: tuple[EntrySignal, ...]) -> str:
    """Milestone 24 Part C1: distinct signal sets must produce distinct
    backtest run IDs, so the run identity cannot collapse two different
    inputs onto one persisted row."""
    rows = sorted(
        (
            {
                "signal_id": signal.signal_id, "symbol": signal.symbol,
                "generated_after_session": signal.generated_after_session.isoformat(),
                "limit_price": str(signal.limit_price), "quantity_hint": str(signal.quantity_hint),
                "initial_stop_reference": str(signal.initial_stop_reference) if signal.initial_stop_reference is not None else None,
                "target_reference": str(signal.target_reference) if signal.target_reference is not None else None,
                "maximum_holding_sessions": signal.maximum_holding_sessions,
            }
            for signal in signals
        ),
        key=lambda row: row["signal_id"],
    )
    return hash_config({"signals": rows})


def _configuration_hash(config: BacktestConfiguration) -> str:
    return hash_config({
        "start_date": config.start_date.isoformat(), "end_date": config.end_date.isoformat(),
        "symbols": config.symbols, "initial_cash": config.initial_cash,
        "atr_period": config.atr_period, "initial_stop_multiple": config.initial_stop_multiple,
        "initial_target_multiple": config.initial_target_multiple, "risk_fraction": config.risk_fraction,
        "max_daily_loss_fraction": config.max_daily_loss_fraction,
        "max_drawdown_fraction": config.max_drawdown_fraction, "slippage_bps": config.slippage_bps,
        "fee_per_order": config.fee_per_order, "maximum_holding_market_days": config.maximum_holding_market_days,
        "ambiguity_policy": config.ambiguity_policy,
        "partial_profit": {
            "enabled": config.partial_profit.enabled,
            "minimum_remaining_quantity": config.partial_profit.minimum_remaining_quantity,
            "stages": tuple((stage.stage, stage.trigger_r_multiple, stage.close_fraction) for stage in config.partial_profit.stages),
        },
        "blackout": {
            "enabled": config.economic_event_blackout.enabled,
            "before_minutes": config.economic_event_blackout.before_minutes,
            "after_minutes": config.economic_event_blackout.after_minutes,
            "minimum_importance": config.economic_event_blackout.minimum_importance,
            "markets": config.economic_event_blackout.markets,
            "blocked_categories": config.economic_event_blackout.blocked_categories,
        },
        "benchmark_symbol": config.benchmark_symbol,
        "code_version": BACKTEST_CODE_VERSION,
    })


def _fill_id(order_id: str, side: str, market_date, quantity: Decimal) -> str:
    raw = f"{order_id}:{side}:{market_date.isoformat()}:{quantity}"
    return "bt-fill-" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def _sell(
    *, position: _Position, quantity: Decimal, price: Decimal, reason: str,
    market_date, cash: Decimal, realized_pnl: Decimal, fills: list[BacktestFill],
    order_suffix: str, config: BacktestConfiguration,
) -> tuple[Decimal, Decimal]:
    if quantity <= 0 or quantity > position.quantity:
        raise BacktestError("invalid deterministic SELL quantity")
    slipped = price * (Decimal("1") - config.slippage_bps / Decimal("10000"))
    proceeds = quantity * slipped - config.fee_per_order
    cash += proceeds
    realized_pnl += (slipped - position.entry_price) * quantity - config.fee_per_order
    order_id = f"bt-order-{position.symbol}-{position.entry_date}-{order_suffix}"
    fills.append(BacktestFill(
        fill_id=_fill_id(order_id, "SELL", market_date, quantity), order_id=order_id,
        symbol=position.symbol, side="SELL", quantity=quantity, fill_price=slipped,
        fees=config.fee_per_order, slippage=(price - slipped) * quantity,
        market_date=market_date, exit_reason=reason, fill_sequence=len(fills) + 1,
        position_id=position.position_id,
    ))
    position.quantity -= quantity
    return cash, realized_pnl


def run_backtest(
    *, configuration: BacktestConfiguration, data_provider: HistoricalDataProvider,
    signals: tuple[EntrySignal, ...], economic_events=(), conn=None,
    strategy_id: str | None = None, strategy_version: str | None = None,
    strategy_config_hash: str | None = None,
) -> BacktestResult:
    config = configuration
    config_hash = _configuration_hash(config)
    signal_set_hash = _signal_set_hash(signals)
    # Milestone 24 Part C1: run identity must include the signal set, not
    # just the configuration — two different signal sets run against the
    # same configuration must never collide onto one persisted run ID.
    input_hash = hash_config({
        "configuration_hash": config_hash, "signal_set_hash": signal_set_hash,
        "code_version": BACKTEST_CODE_VERSION,
    })
    run_id = "backtest-" + hashlib.sha256(input_hash.encode()).hexdigest()[:32]
    final_as_of = datetime.combine(config.end_date, time(23, 59, 59), tzinfo=timezone.utc)
    bars_by_symbol: dict[str, tuple[HistoricalBar, ...]] = {}
    for symbol in config.symbols:
        bars = data_provider.bars(
            symbol=symbol, start=config.start_date, end=config.end_date, as_of=final_as_of,
        )
        if not bars:
            raise BacktestError(f"empty historical dataset for {symbol}")
        if any(bar.available_at > final_as_of for bar in bars):
            raise BacktestError("historical provider returned a future-available bar")
        bars_by_symbol[symbol] = bars

    sessions = sorted({bar.session_date for bars in bars_by_symbol.values() for bar in bars})
    if not sessions:
        raise BacktestError("historical dataset has no sessions")
    bar_maps = {symbol: {bar.session_date: bar for bar in bars} for symbol, bars in bars_by_symbol.items()}
    session_index = {session: index for index, session in enumerate(sessions)}

    eligible_signals: dict[object, list[EntrySignal]] = {}
    rejected: list[dict] = []
    for signal in sorted(signals, key=lambda item: (item.generated_after_session, item.signal_id)):
        if signal.symbol not in bars_by_symbol:
            rejected.append({"signal_id": signal.signal_id, "reason": "UNKNOWN_SYMBOL"})
            continue
        next_sessions = [
            bar.session_date for bar in bars_by_symbol[signal.symbol]
            if bar.session_date > signal.generated_after_session
        ]
        if not next_sessions:
            rejected.append({"signal_id": signal.signal_id, "reason": "NO_NEXT_SESSION"})
            continue
        eligible_signals.setdefault(next_sessions[0], []).append(signal)

    cash = config.initial_cash
    realized = Decimal("0")
    peak_equity = config.initial_cash
    positions: dict[str, _Position] = {}
    fills: list[BacktestFill] = []
    daily_states: list[BacktestDailyState] = []
    unresolved: list[str] = []
    previous_closes: dict[str, Decimal] = {}

    def mark_equity(date_value, price_kind: str = "close") -> tuple[Decimal, Decimal]:
        market_value = Decimal("0")
        unrealized = Decimal("0")
        for symbol, position in positions.items():
            bar = bar_maps[symbol].get(date_value)
            price = getattr(bar, price_kind) if bar is not None else previous_closes.get(symbol)
            if price is None:
                raise BacktestError(f"missing point-in-time mark for open {symbol} position")
            market_value += position.quantity * price
            unrealized += position.quantity * (price - position.entry_price)
        return cash + market_value, unrealized

    for current_date in sessions:
        start_equity, _ = mark_equity(current_date, "open")

        # Existing positions own the entire current bar and are evaluated
        # before any next-session entries are created.
        for symbol in sorted(tuple(positions)):
            position = positions[symbol]
            bar = bar_maps[symbol].get(current_date)
            if bar is None:
                unresolved.append(f"{current_date}:{symbol}:MISSING_POSITION_BAR")
                continue
            held_sessions = session_index[current_date] - position.entry_session_index
            reason = None
            exit_price = None
            if bar.open <= position.stop:
                reason, exit_price = "STOP_GAP", bar.open
            elif bar.low <= position.stop:
                reason, exit_price = "HARD_OR_TRAILING_STOP", position.stop
            elif held_sessions >= position.maximum_holding_sessions:
                reason, exit_price = "MAXIMUM_HOLDING_PERIOD", bar.open

            if reason is not None:
                assert exit_price is not None
                cash, realized = _sell(
                    position=position, quantity=position.quantity, price=exit_price, reason=reason,
                    market_date=current_date, cash=cash, realized_pnl=realized, fills=fills,
                    order_suffix=reason, config=config,
                )
                del positions[symbol]
                continue

            risk_per_share = position.entry_price - (
                position.entry_price - position.entry_atr * config.initial_stop_multiple
            )
            high_r = (bar.high - position.entry_price) / risk_per_share
            partial = None
            if config.partial_profit.enabled:
                for stage in config.partial_profit.stages:
                    if stage.stage > position.partial_stage and high_r >= stage.trigger_r_multiple:
                        quantity = calculate_partial_close_quantity(
                            original_quantity=position.original_quantity,
                            close_fraction=stage.close_fraction,
                            available_unreserved_quantity=position.quantity,
                            current_remaining_quantity=position.quantity,
                            minimum_remaining_quantity=config.partial_profit.minimum_remaining_quantity,
                        )
                        partial = (stage, quantity)
                        break
            if partial is not None:
                stage, quantity = partial
                position.partial_stage = stage.stage
                if quantity > 0:
                    trigger_price = position.entry_price + risk_per_share * stage.trigger_r_multiple
                    cash, realized = _sell(
                        position=position, quantity=quantity, price=trigger_price,
                        reason="PARTIAL_PROFIT", market_date=current_date, cash=cash,
                        realized_pnl=realized, fills=fills, order_suffix=f"partial-{stage.stage}", config=config,
                    )
                    if position.quantity == 0:
                        del positions[symbol]
                    continue
                unresolved.append(f"{current_date}:{symbol}:PARTIAL_STAGE_ZERO_SHARES:{stage.stage}")

            if symbol not in positions:
                continue
            if bar.high >= position.target:
                cash, realized = _sell(
                    position=position, quantity=position.quantity, price=position.target,
                    reason="FINAL_TARGET", market_date=current_date, cash=cash,
                    realized_pnl=realized, fills=fills, order_suffix="target", config=config,
                )
                del positions[symbol]
                continue

            position.highest = max(position.highest, bar.high)
            history = [item for item in bars_by_symbol[symbol] if item.session_date <= current_date]
            atr = average_true_range(tuple(
                OHLCBar(item.session_date, item.high, item.low, item.close) for item in history
            ), period=config.atr_period)
            if atr is not None:
                candidate = position.highest - atr * config.initial_stop_multiple
                position.stop = max(position.stop, candidate)

        pre_entry_equity, _ = mark_equity(current_date, "open")
        daily_fraction = (pre_entry_equity - start_equity) / start_equity if start_equity > 0 else Decimal("0")
        current_drawdown = (pre_entry_equity - peak_equity) / peak_equity

        for signal in eligible_signals.get(current_date, []):
            if signal.symbol in positions:
                rejected.append({"signal_id": signal.signal_id, "reason": "POSITION_ALREADY_OPEN"})
                continue
            if daily_fraction <= -config.max_daily_loss_fraction:
                rejected.append({"signal_id": signal.signal_id, "reason": "DAILY_LOSS_LIMIT"})
                continue
            if current_drawdown <= -config.max_drawdown_fraction:
                rejected.append({"signal_id": signal.signal_id, "reason": "DRAWDOWN_LIMIT"})
                continue
            entry_as_of = datetime.combine(current_date, time(14, 30), tzinfo=timezone.utc)
            blackout = evaluate_economic_event_blackout(
                as_of=entry_as_of, events=tuple(economic_events) if economic_events is not None else None,
                configuration=config.economic_event_blackout,
            )
            if not blackout.allowed:
                rejected.append({"signal_id": signal.signal_id, "reason": "ECONOMIC_EVENT_BLACKOUT"})
                continue
            bar = bar_maps[signal.symbol].get(current_date)
            if bar is None or bar.low > signal.limit_price:
                rejected.append({"signal_id": signal.signal_id, "reason": "LIMIT_NOT_FILLED"})
                continue
            prior = [
                item for item in bars_by_symbol[signal.symbol]
                if item.session_date <= signal.generated_after_session
            ]
            atr = average_true_range(tuple(
                OHLCBar(item.session_date, item.high, item.low, item.close) for item in prior
            ), period=config.atr_period)
            if atr is None or atr <= 0:
                rejected.append({"signal_id": signal.signal_id, "reason": "ATR_UNAVAILABLE"})
                continue
            base_fill = min(bar.open, signal.limit_price)
            fill_price = min(
                signal.limit_price,
                base_fill * (Decimal("1") + config.slippage_bps / Decimal("10000")),
            )
            stop, target = atr_risk_levels(
                entry_price=fill_price, atr=atr, stop_multiple=config.initial_stop_multiple,
                target_multiple=config.initial_target_multiple,
            )
            # Milestone 24 Part C3: a strategy-specific exit always wins over
            # the engine's generic ATR default — silently substituting the
            # global default would erase the reason the strategy chose that
            # level in the first place. Invalid values reject the signal
            # rather than being coerced into something fillable.
            if signal.initial_stop_reference is not None:
                if signal.initial_stop_reference >= fill_price:
                    rejected.append({"signal_id": signal.signal_id, "reason": "INVALID_STRATEGY_STOP"})
                    continue
                stop = signal.initial_stop_reference
            if signal.target_reference is not None:
                if signal.target_reference <= fill_price:
                    rejected.append({"signal_id": signal.signal_id, "reason": "INVALID_STRATEGY_TARGET"})
                    continue
                target = signal.target_reference
            risk_qty = (pre_entry_equity * config.risk_fraction / (fill_price - stop)).to_integral_value(rounding=ROUND_FLOOR)
            cash_qty = ((cash - config.fee_per_order) / fill_price).to_integral_value(rounding=ROUND_FLOOR)
            quantity = min(signal.quantity_hint, risk_qty, cash_qty)
            if quantity <= 0:
                rejected.append({"signal_id": signal.signal_id, "reason": "ZERO_SHARES_AT_RISK_CAP"})
                continue
            cost = quantity * fill_price + config.fee_per_order
            cash -= cost
            order_id = f"bt-order-{signal.signal_id}"
            fill_id = _fill_id(order_id, "BUY", current_date, quantity)
            fills.append(BacktestFill(
                fill_id=fill_id, order_id=order_id,
                symbol=signal.symbol, side="BUY", quantity=quantity, fill_price=fill_price,
                fees=config.fee_per_order, slippage=(fill_price - base_fill) * quantity,
                market_date=current_date, fill_sequence=len(fills) + 1, position_id=fill_id,
            ))
            positions[signal.symbol] = _Position(
                position_id=fill_id, symbol=signal.symbol, quantity=quantity, original_quantity=quantity,
                entry_price=fill_price, entry_date=current_date,
                entry_session_index=session_index[current_date], entry_atr=atr,
                stop=stop, target=target, highest=fill_price,
                maximum_holding_sessions=signal.maximum_holding_sessions or config.maximum_holding_market_days,
            )

        equity, unrealized = mark_equity(current_date)
        peak_equity = max(peak_equity, equity)
        drawdown = (equity - peak_equity) / peak_equity
        daily_states.append(BacktestDailyState(
            market_date=current_date, cash=cash, equity=equity, realized_pnl=realized,
            unrealized_pnl=unrealized, drawdown_fraction=drawdown,
        ))
        for symbol, bar_map in bar_maps.items():
            if current_date in bar_map:
                previous_closes[symbol] = bar_map[current_date].close

    benchmark_return = None
    if config.benchmark_symbol:
        benchmark = bars_by_symbol.get(config.benchmark_symbol)
        if not benchmark:
            raise BacktestError("benchmark dataset is unavailable")
        benchmark_return = (benchmark[-1].close - benchmark[0].close) / benchmark[0].close
    report = build_report(
        initial_cash=config.initial_cash, daily_states=tuple(daily_states), fills=tuple(fills),
        rejected_entries=tuple(rejected), benchmark_return=benchmark_return,
        unresolved_count=len(unresolved),
    )
    report.update({
        "start_date": config.start_date.isoformat(), "end_date": config.end_date.isoformat(),
        "symbols": list(config.symbols), "ambiguity_policy": config.ambiguity_policy,
        "configuration_hash": config_hash, "code_version": BACKTEST_CODE_VERSION,
    })
    result = BacktestResult(
        backtest_run_id=run_id, configuration_hash=config_hash,
        daily_states=tuple(daily_states), fills=tuple(fills), rejected_entries=tuple(rejected),
        metrics=report, unresolved_evaluations=tuple(unresolved),
    )
    if conn is not None:
        _persist_result(
            conn, result, config, input_hash=input_hash, signal_set_hash=signal_set_hash,
            strategy_id=strategy_id, strategy_version=strategy_version,
            strategy_config_hash=strategy_config_hash,
        )
    return result


def _persist_result(
    conn, result: BacktestResult, config: BacktestConfiguration, *, input_hash: str, signal_set_hash: str,
    strategy_id: str | None, strategy_version: str | None, strategy_config_hash: str | None,
) -> None:
    import json
    from ..storage.transactions import transaction

    def encode(value):
        if isinstance(value, Decimal):
            return str(value)
        raise TypeError(type(value).__name__)

    created_at = datetime.combine(config.end_date, time(23, 59, 59), tzinfo=timezone.utc).isoformat()
    ending = result.daily_states[-1].equity if result.daily_states else config.initial_cash
    with transaction(conn):
        existing = conn.execute(
            "SELECT input_hash FROM backtest_runs WHERE backtest_run_id = ?", (result.backtest_run_id,),
        ).fetchone()
        if existing is not None and existing[0] is not None and existing[0] != input_hash:
            # Milestone 24 Part C1: a run ID collision with a different input
            # hash means two distinct signal sets or configurations hashed
            # to the same ID (or a hashing bug) — never silently overwrite
            # or keep whichever was inserted first.
            raise BacktestError(
                f"backtest_run_id {result.backtest_run_id!r} already exists with a different input hash"
            )
        if existing is not None:
            return  # idempotent replay of the identical run — nothing new to persist
        conn.execute(
            "INSERT INTO backtest_runs "
            "(backtest_run_id, started_at, start_date, end_date, symbols_json, initial_cash, ending_equity, "
            "configuration_hash, code_version, ambiguity_policy, status, report_json, created_at, "
            "input_hash, signal_set_hash, strategy_id, strategy_version, strategy_config_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'COMPLETED', ?, ?, ?, ?, ?, ?, ?)",
            (result.backtest_run_id, created_at, config.start_date.isoformat(), config.end_date.isoformat(),
             json.dumps(list(config.symbols)), str(config.initial_cash), str(ending), result.configuration_hash,
             BACKTEST_CODE_VERSION, config.ambiguity_policy,
             json.dumps(result.metrics, sort_keys=True, default=encode), created_at,
             input_hash, signal_set_hash, strategy_id, strategy_version, strategy_config_hash),
        )
        for state in result.daily_states:
            conn.execute(
                "INSERT OR IGNORE INTO backtest_daily_states "
                "(backtest_run_id, market_date, cash, equity, realized_pnl, unrealized_pnl, drawdown_fraction, state_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (result.backtest_run_id, state.market_date.isoformat(), str(state.cash), str(state.equity),
                 str(state.realized_pnl), str(state.unrealized_pnl), str(state.drawdown_fraction),
                 json.dumps(state.__dict__, sort_keys=True, default=lambda value: value.isoformat() if hasattr(value, "isoformat") else str(value))),
            )
        for fill in result.fills:
            conn.execute(
                "INSERT OR IGNORE INTO backtest_fills "
                "(backtest_run_id, fill_id, order_id, symbol, side, quantity, fill_price, fees, slippage, "
                "market_date, exit_reason, fill_sequence, position_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (result.backtest_run_id, fill.fill_id, fill.order_id, fill.symbol, fill.side,
                 str(fill.quantity), str(fill.fill_price), str(fill.fees), str(fill.slippage),
                 fill.market_date.isoformat(), fill.exit_reason, fill.fill_sequence, fill.position_id),
            )
        conn.execute(
            "INSERT OR IGNORE INTO backtest_metrics (backtest_run_id, metrics_json, created_at) VALUES (?, ?, ?)",
            (result.backtest_run_id, json.dumps(result.metrics, sort_keys=True, default=encode), created_at),
        )
