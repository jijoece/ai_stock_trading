"""Forward-performance evaluation (docs/milestone-4.md Step 11).

Pure functions over already-known recommendation/execution facts plus a
`PriceProvider` — no broker access, no LumiBot, no database I/O (persistence
lives in `storage/evaluation_repositories.py`). Idempotent: identical
inputs always produce a byte-identical `RecommendationEvaluation` (aside
from `evaluated_at`, which is the caller-supplied clock reading, never
`datetime.now()` read internally) — safe to recompute.

No look-ahead: a horizon whose target trading date has not yet occurred
(`target_date > now.date()`) is reported `PENDING` and no price lookup for
that date is ever attempted — `price_provider.get_close` is never called
for a date beyond `now`.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Callable

from .market_calendar import add_trading_days, next_trading_session
from .models import DEFAULT_BENCHMARK_SYMBOL, EVALUATION_HORIZONS, RecommendationEvaluation
from .price_provider import PriceProvider


def evaluate_recommendation(
    *,
    recommendation_id: str,
    symbol: str,
    recommendation_price: Decimal | None,
    execution_price: Decimal | None,
    filled_quantity: int,
    requested_quantity: int,
    execution_completed_at: datetime | None,
    horizon_trading_days: int,
    price_provider: PriceProvider,
    now: datetime,
    benchmark_symbol: str = DEFAULT_BENCHMARK_SYMBOL,
    fees: Decimal = Decimal("0"),
    model_version: str | None = None,
    prompt_version: str | None = None,
    config_hash: str | None = None,
    market_regime: str | None = None,
) -> RecommendationEvaluation:
    if execution_completed_at is None or filled_quantity <= 0:
        return RecommendationEvaluation(
            recommendation_id=recommendation_id, horizon_trading_days=horizon_trading_days,
            status="NEVER_EXECUTED", evaluation_date=now.date(), benchmark_symbol=benchmark_symbol,
            recommendation_price=recommendation_price, fees=fees, model_version=model_version,
            prompt_version=prompt_version, config_hash=config_hash, market_regime=market_regime,
            evaluated_at=now,
        )

    execution_date = execution_completed_at.date()
    anchor = next_trading_session(execution_date, inclusive=True)
    target_date = add_trading_days(anchor, horizon_trading_days)

    common = dict(
        recommendation_id=recommendation_id, horizon_trading_days=horizon_trading_days,
        benchmark_symbol=benchmark_symbol, recommendation_price=recommendation_price,
        execution_price=execution_price, fees=fees, model_version=model_version,
        prompt_version=prompt_version, config_hash=config_hash, market_regime=market_regime,
        evaluated_at=now,
    )

    if target_date > now.date():
        return RecommendationEvaluation(status="PENDING", evaluation_date=target_date, **common)

    ending_point = price_provider.get_close(symbol, target_date)
    benchmark_start_point = price_provider.get_close(benchmark_symbol, anchor)
    benchmark_end_point = price_provider.get_close(benchmark_symbol, target_date)

    if ending_point is None:
        return RecommendationEvaluation(
            status="DELISTED_OR_UNAVAILABLE", evaluation_date=target_date,
            missing_data_reasons=(f"no closing price for {symbol} on {target_date.isoformat()}",), **common,
        )
    if benchmark_start_point is None or benchmark_end_point is None:
        reasons = []
        if benchmark_start_point is None:
            reasons.append(f"no closing price for benchmark {benchmark_symbol} on {anchor.isoformat()}")
        if benchmark_end_point is None:
            reasons.append(f"no closing price for benchmark {benchmark_symbol} on {target_date.isoformat()}")
        return RecommendationEvaluation(
            status="BENCHMARK_MISSING", evaluation_date=target_date, missing_data_reasons=tuple(reasons), **common,
        )

    gross_return = (ending_point.close - execution_price) / execution_price
    fee_drag = (fees / (execution_price * filled_quantity)) if fees > 0 else Decimal("0")
    net_return = gross_return - fee_drag
    benchmark_return = (benchmark_end_point.close - benchmark_start_point.close) / benchmark_start_point.close
    excess_return = net_return - benchmark_return
    slippage = (execution_price - recommendation_price) if recommendation_price is not None else None
    status = "COMPLETED" if filled_quantity == requested_quantity else "PARTIALLY_FILLED"

    return RecommendationEvaluation(
        status=status, evaluation_date=target_date, ending_symbol_price=ending_point.close,
        ending_benchmark_price=benchmark_end_point.close, benchmark_price_at_execution=benchmark_start_point.close,
        gross_return=gross_return, net_return=net_return, benchmark_return=benchmark_return,
        excess_return=excess_return, slippage=slippage, price_source_as_of=target_date.isoformat(), **common,
    )


def evaluate_recommendation_all_horizons(
    *, horizons: tuple[int, ...] = EVALUATION_HORIZONS, clock: Callable[[], datetime] | None = None, **kwargs,
) -> list[RecommendationEvaluation]:
    now = kwargs.pop("now", None) or (clock() if clock else None)
    if now is None:
        raise ValueError("either now= or clock= must be supplied")
    return [evaluate_recommendation(horizon_trading_days=h, now=now, **kwargs) for h in horizons]
