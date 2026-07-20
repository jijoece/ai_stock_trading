"""Momentum Breakout deterministic candidate strategy (Milestone 23, B3).

Entry thesis: close exceeds a prior N-day high on confirming volume, with a
positive trend filter, non-negative relative strength, and the breakout not
excessively extended above the breakout level. Every check is evaluated and
reason-coded; missing bars/volume/relative-strength data fails closed to
`INCOMPLETE` rather than defaulting favorably.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .config import MomentumBreakoutConfig
from .contracts import StrategyContext, StrategyMarketData, StrategySignal, StrategyStatus
from .factors import average_true_range, closes, prior_high, sma_slope, volume_ratio
from .safety_gates import classify_safety_status
from .timestamps import bar_series_data_as_of

STRATEGY_ID = "momentum_breakout"
STRATEGY_VERSION = "1.0.0"


class MomentumBreakoutStrategy:
    strategy_id = STRATEGY_ID
    strategy_version = STRATEGY_VERSION

    def __init__(self, config: MomentumBreakoutConfig) -> None:
        self._config = config

    def evaluate(
        self,
        symbol: str,
        market_data: StrategyMarketData,
        context: StrategyContext,
    ) -> StrategySignal:
        cfg = self._config
        minimum_bars = max(cfg.breakout_lookback_days, cfg.trend_sma_days + cfg.trend_slope_lookback_days,
                            cfg.volume_lookback_days, cfg.atr_period) + 1

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

        breakout_level = prior_high(bars, cfg.breakout_lookback_days)
        vol_ratio = volume_ratio(bars, cfg.volume_lookback_days)
        trend_slope = sma_slope(close_series, cfg.trend_sma_days, cfg.trend_slope_lookback_days)
        atr = average_true_range(bars, cfg.atr_period)
        relative_strength = market_data.technical.relative_strength if market_data.technical else None

        if breakout_level is None or vol_ratio is None or trend_slope is None or atr is None:
            return self._signal(
                symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                ("insufficient_factor_history",), {}, data_as_of=data_as_of,
            )
        if relative_strength is None:
            return self._signal(
                symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                ("missing_relative_strength",), {}, data_as_of=data_as_of,
            )
        # Milestone 25 Part B2: relative_strength has no timestamp of its
        # own — an untimestamped value can never be treated as point-in-time
        # safe, so its freshness snapshot must exist and not be from the
        # future.
        technical_freshness = market_data.technical.freshness if market_data.technical else None
        if technical_freshness is None:
            return self._signal(
                symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                ("missing_technical_freshness",), {}, data_as_of=data_as_of,
            )
        if technical_freshness.as_of > context.now:
            return self._signal(
                symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                ("future_technical_freshness",), {}, data_as_of=data_as_of,
            )
        assert data_as_of is not None
        data_as_of = max(data_as_of, technical_freshness.as_of)

        breakout_confirmed = latest_close > breakout_level
        volume_confirmed = vol_ratio >= cfg.minimum_volume_ratio
        trend_positive = trend_slope > 0.0
        relative_strength_ok = relative_strength >= cfg.minimum_relative_strength
        extension_percent = (
            (latest_close - breakout_level) / breakout_level * 100.0 if breakout_level > 0 else float("inf")
        )
        extension_ok = extension_percent <= cfg.maximum_breakout_extension_percent

        reasons = (
            ("breakout_confirmed" if breakout_confirmed else "no_breakout"),
            ("volume_confirmed" if volume_confirmed else "volume_insufficient"),
            ("trend_positive" if trend_positive else "trend_negative"),
            ("relative_strength_ok" if relative_strength_ok else "relative_strength_below_minimum"),
            ("extension_ok" if extension_ok else "extended_beyond_limit"),
        )

        factor_values = {
            "breakout_level": breakout_level,
            "volume_ratio": vol_ratio,
            "trend_slope": trend_slope,
            "relative_strength": relative_strength,
            "extension_percent": extension_percent,
            "atr": atr,
        }

        eligible = (
            breakout_confirmed and volume_confirmed and trend_positive
            and relative_strength_ok and extension_ok
        )
        if not eligible:
            return self._signal(symbol, context.now,
                                 StrategyStatus.NOT_ELIGIBLE, 0.0, reasons, factor_values,
                                 data_as_of=data_as_of)

        signal_strength = self._signal_strength(
            extension_percent, cfg.maximum_breakout_extension_percent,
            vol_ratio, cfg.minimum_volume_ratio,
            trend_slope, relative_strength,
        )

        entry = Decimal(str(latest_close))
        stop = Decimal(str(round(latest_close - cfg.atr_stop_multiple * atr, 4)))
        return self._signal(
            symbol, context.now, StrategyStatus.ELIGIBLE, signal_strength, reasons, factor_values,
            entry_reference=entry, limit_reference=entry, invalidation_price=stop,
            initial_stop_reference=stop, target_reference=None,
            expected_holding_period=cfg.maximum_holding_days, data_as_of=data_as_of,
        )

    @staticmethod
    def _signal_strength(
        extension_percent: float, max_extension: float,
        vol_ratio: float, min_vol_ratio: float,
        trend_slope: float, relative_strength: float,
    ) -> float:
        def clip01(x: float) -> float:
            return max(0.0, min(1.0, x))

        extension_term = clip01(1.0 - (extension_percent / max_extension)) if max_extension > 0 else 0.0
        volume_term = clip01((vol_ratio - min_vol_ratio) / min_vol_ratio) if min_vol_ratio > 0 else 0.0
        trend_term = clip01(trend_slope / (abs(trend_slope) + 1.0))
        rs_term = clip01((relative_strength + 2.0) / 4.0)  # relative_strength is on an arbitrary scale; center-clip

        weights = (0.30, 0.30, 0.25, 0.15)
        return round(
            weights[0] * extension_term + weights[1] * volume_term
            + weights[2] * trend_term + weights[3] * rs_term,
            4,
        )

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
        data_quality = "complete" if status == StrategyStatus.ELIGIBLE or status == StrategyStatus.NOT_ELIGIBLE else status.value.lower()
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
