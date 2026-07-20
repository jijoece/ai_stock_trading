"""Mean Reversion deterministic candidate strategy (Milestone 23, B4).

Entry thesis: price is statistically stretched below its short-term mean
(negative z-score, oversold RSI) while the long-term trend and fundamentals
remain structurally intact — never averaged down, one initial entry intent
per signal (enforced at the order-intent layer, out of scope here). The
strategy must distinguish a temporary stretch from structural deterioration:
it requires the shared screener's distress/going-concern gates to have
already passed and rejects candidates carrying a severe SEC filing risk
flag, on top of the price-above-long-trend requirement.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .config import MeanReversionConfig
from .contracts import StrategyContext, StrategyMarketData, StrategySignal, StrategyStatus
from .factors import average_true_range, closes, rolling_zscore, rsi_wilder, simple_moving_average
from .safety_gates import classify_safety_status
from .timestamps import bar_series_data_as_of

STRATEGY_ID = "mean_reversion"
STRATEGY_VERSION = "1.0.0"


class MeanReversionStrategy:
    strategy_id = STRATEGY_ID
    strategy_version = STRATEGY_VERSION

    def __init__(self, config: MeanReversionConfig) -> None:
        self._config = config

    def evaluate(
        self,
        symbol: str,
        market_data: StrategyMarketData,
        context: StrategyContext,
    ) -> StrategySignal:
        cfg = self._config
        minimum_bars = max(cfg.mean_lookback_days, cfg.rsi_period + 1,
                            cfg.long_trend_sma_days if cfg.require_price_above_long_trend else 0,
                            cfg.atr_period) + 1

        safety = classify_safety_status(context.screening_result, market_data.bars, minimum_bars)
        if safety is not None:
            status, reasons = safety
            return self._signal(symbol, context.now, status, 0.0, reasons, {})

        bars = market_data.bars
        data_as_of, future_reason = bar_series_data_as_of(bars, context.now)
        if future_reason is not None:
            return self._signal(symbol, context.now, StrategyStatus.INCOMPLETE, 0.0, (future_reason,), {})

        close_series = closes(bars)
        latest_close = close_series[-1]

        zscore = rolling_zscore(close_series, cfg.mean_lookback_days)
        rsi = rsi_wilder(close_series, cfg.rsi_period)
        short_term_mean = simple_moving_average(close_series, cfg.mean_lookback_days)
        long_trend_sma = simple_moving_average(close_series, cfg.long_trend_sma_days) \
            if cfg.require_price_above_long_trend else None
        atr = average_true_range(bars, cfg.atr_period)

        if zscore is None or rsi is None or atr is None or short_term_mean is None:
            return self._signal(
                symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                ("insufficient_factor_history",), {}, data_as_of=data_as_of,
            )
        if cfg.require_price_above_long_trend and long_trend_sma is None:
            return self._signal(
                symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                ("missing_long_trend_history",), {}, data_as_of=data_as_of,
            )

        # Milestone 25 Part B3: a missing catalyst snapshot must never be
        # interpreted as zero SEC risk flags — that silently treats unknown
        # risk as no risk. Require the snapshot and its freshness to exist
        # and not be from the future; only then may its (possibly empty)
        # flags be trusted.
        if market_data.catalyst is None:
            return self._signal(
                symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                ("missing_catalyst_risk_data",), {}, data_as_of=data_as_of,
            )
        catalyst_freshness = market_data.catalyst.freshness
        if catalyst_freshness is None:
            return self._signal(
                symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                ("missing_catalyst_risk_freshness",), {}, data_as_of=data_as_of,
            )
        if catalyst_freshness.as_of > context.now:
            return self._signal(
                symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                ("future_catalyst_risk_data",), {}, data_as_of=data_as_of,
            )
        assert data_as_of is not None
        data_as_of = max(data_as_of, catalyst_freshness.as_of)
        severe_risk_flags = market_data.catalyst.sec_filing_risk_flags

        zscore_ok = zscore <= cfg.zscore_entry_threshold
        rsi_ok = rsi <= cfg.maximum_entry_rsi
        long_trend_intact = (
            not cfg.require_price_above_long_trend
            or (long_trend_sma is not None and latest_close > long_trend_sma)
        )
        no_severe_risk = len(severe_risk_flags) == 0

        reasons = (
            ("zscore_oversold" if zscore_ok else "insufficient_deviation"),
            ("rsi_oversold" if rsi_ok else "rsi_above_threshold"),
            ("long_trend_intact" if long_trend_intact else "long_trend_broken"),
            ("no_severe_risk_flags" if no_severe_risk else "severe_risk_flag_present"),
        )

        factor_values = {
            "zscore": zscore,
            "rsi": rsi,
            "atr": atr,
            "short_term_mean": short_term_mean,
            **({"long_trend_sma": long_trend_sma} if long_trend_sma is not None else {}),
        }

        eligible = zscore_ok and rsi_ok and long_trend_intact and no_severe_risk
        if not eligible:
            return self._signal(symbol, context.now,
                                 StrategyStatus.NOT_ELIGIBLE, 0.0, reasons, factor_values,
                                 data_as_of=data_as_of)

        signal_strength = self._signal_strength(zscore, cfg.zscore_entry_threshold, rsi, cfg.maximum_entry_rsi)

        entry = Decimal(str(latest_close))
        stop = Decimal(str(round(latest_close - cfg.atr_stop_multiple * atr, 4)))
        # Milestone 24 Part B5: eligibility already requires
        # `latest_close > long_trend_sma` (the long-term SMA is a structural
        # trend gate, not a reversion target) — using it as the target put
        # the target below entry on every eligible long signal. The
        # short-term mean is the actual reversion target, and only when it
        # sits above entry; a not-yet-reverted short-term mean produces no
        # target rather than an inverted one (fail closed at the execution
        # boundary, which requires a stop but treats target as optional).
        target = (
            Decimal(str(round(short_term_mean, 4)))
            if short_term_mean > latest_close else None
        )
        return self._signal(
            symbol, context.now, StrategyStatus.ELIGIBLE, signal_strength, reasons, factor_values,
            entry_reference=entry, limit_reference=entry, invalidation_price=stop,
            initial_stop_reference=stop, target_reference=target,
            expected_holding_period=cfg.maximum_holding_days, data_as_of=data_as_of,
        )

    @staticmethod
    def _signal_strength(zscore: float, zscore_threshold: float, rsi: float, max_rsi: float) -> float:
        def clip01(x: float) -> float:
            return max(0.0, min(1.0, x))

        # more negative than the threshold -> stronger signal
        zscore_term = clip01((zscore_threshold - zscore) / abs(zscore_threshold)) if zscore_threshold != 0 else 0.0
        rsi_term = clip01((max_rsi - rsi) / max_rsi) if max_rsi > 0 else 0.0
        return round(0.6 * zscore_term + 0.4 * rsi_term, 4)

    def _signal(
        self,
        symbol: str,
        now: datetime,
        status: StrategyStatus,
        signal_strength: float,
        reasons: tuple[str, ...],
        factor_values: dict[str, float],
        entry_reference: Decimal | None = None,
        limit_reference: Decimal | None = None,
        invalidation_price: Decimal | None = None,
        initial_stop_reference: Decimal | None = None,
        target_reference: Decimal | None = None,
        expected_holding_period: int | None = None,
        data_as_of: datetime | None = None,
    ) -> StrategySignal:
        data_quality = (
            "complete" if status in (StrategyStatus.ELIGIBLE, StrategyStatus.NOT_ELIGIBLE)
            else status.value.lower()
        )
        return StrategySignal(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            symbol=symbol,
            signal_timestamp=now if now.tzinfo else now.replace(tzinfo=timezone.utc),
            data_as_of=data_as_of,
            status=status,
            signal_strength=signal_strength,
            entry_reference=entry_reference,
            limit_reference=limit_reference,
            invalidation_price=invalidation_price,
            initial_stop_reference=initial_stop_reference,
            target_reference=target_reference,
            expected_holding_period=expected_holding_period,
            reason_codes=reasons,
            factor_values=factor_values,
            data_quality=data_quality,
            configuration_hash=self._config.configuration_hash,
        )
