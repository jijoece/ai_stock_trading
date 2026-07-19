"""Event-Driven Catalyst deterministic candidate strategy (Milestone 23, B5).

Entry thesis: a recent, point-in-time-valid, positive `MarketEvent` (see
`events.py`) with price/volume confirmation, no excessive gap, and no
conflicting severe negative event for the same symbol. The event stream is
never interpreted by an LLM here — only structured `MarketEvent` fields are
read.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .config import EventCatalystConfig
from .contracts import StrategyContext, StrategyMarketData, StrategySignal, StrategyStatus
from .factors import closes, volume_ratio
from .safety_gates import classify_safety_status

STRATEGY_ID = "event_catalyst"
STRATEGY_VERSION = "1.0.0"


class EventDrivenCatalystStrategy:
    strategy_id = STRATEGY_ID
    strategy_version = STRATEGY_VERSION

    def __init__(self, config: EventCatalystConfig) -> None:
        self._config = config

    def evaluate(
        self,
        symbol: str,
        market_data: StrategyMarketData,
        context: StrategyContext,
    ) -> StrategySignal:
        cfg = self._config
        minimum_bars = cfg.volume_lookback_days + cfg.confirmation_window_days + 1

        safety = classify_safety_status(context.screening_result, market_data.bars, minimum_bars)
        if safety is not None:
            status, reasons = safety
            return self._signal(symbol, context.now, status, 0.0, reasons, {})

        events = [e for e in market_data.events if e.symbol == symbol]
        if not events:
            return self._signal(
                symbol, context.now, StrategyStatus.NOT_ELIGIBLE, 0.0,
                ("no_event_present",), {},
            )

        for event in events:
            if event.event_timestamp > context.now:
                return self._signal(
                    symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                    (f"event_timestamp_after_decision_timestamp:{event.event_id}",), {},
                )

        max_age = timedelta(hours=cfg.maximum_event_age_hours)
        recent_events = [e for e in events if (context.now - e.event_timestamp) <= max_age]
        if not recent_events:
            return self._signal(
                symbol, context.now, StrategyStatus.NOT_ELIGIBLE, 0.0,
                ("stale_event",), {},
            )

        negative_events = [e for e in recent_events if e.positive_or_negative < 0]
        positive_events = [e for e in recent_events if e.positive_or_negative > 0]

        if negative_events:
            return self._signal(
                symbol, context.now, StrategyStatus.NOT_ELIGIBLE, 0.0,
                ("conflicting_risk_event",), {"negative_event_count": float(len(negative_events))},
            )
        if not positive_events:
            return self._signal(
                symbol, context.now, StrategyStatus.NOT_ELIGIBLE, 0.0,
                ("event_not_positive",), {},
            )

        bars = market_data.bars
        close_series = closes(bars)
        latest_close = close_series[-1]
        prior_close = close_series[-2] if len(close_series) >= 2 else None
        vol_ratio = volume_ratio(bars, cfg.volume_lookback_days)

        if vol_ratio is None or prior_close is None:
            return self._signal(
                symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                ("insufficient_factor_history",), {},
            )

        gap_percent = abs(latest_close - prior_close) / prior_close * 100.0 if prior_close > 0 else float("inf")
        volume_confirmed = vol_ratio >= cfg.minimum_volume_ratio
        gap_ok = gap_percent <= cfg.maximum_gap_percent

        reasons = (
            "recent_positive_event_confirmed",
            ("volume_confirmed" if volume_confirmed else "unconfirmed_price_response"),
            ("gap_ok" if gap_ok else "excessive_gap"),
        )
        factor_values = {
            "volume_ratio": vol_ratio,
            "gap_percent": gap_percent,
            "event_age_hours": (context.now - positive_events[0].event_timestamp).total_seconds() / 3600.0,
        }

        eligible = volume_confirmed and gap_ok
        if not eligible:
            return self._signal(symbol, context.now,
                                 StrategyStatus.NOT_ELIGIBLE, 0.0, reasons, factor_values)

        signal_strength = self._signal_strength(vol_ratio, cfg.minimum_volume_ratio, gap_percent, cfg.maximum_gap_percent)

        entry = Decimal(str(latest_close))
        return self._signal(
            symbol, context.now, StrategyStatus.ELIGIBLE, signal_strength, reasons, factor_values,
            entry_reference=entry, limit_reference=entry, invalidation_price=None,
            initial_stop_reference=None, target_reference=None,
            expected_holding_period=cfg.maximum_holding_days,
        )

    @staticmethod
    def _signal_strength(vol_ratio: float, min_vol_ratio: float, gap_percent: float, max_gap: float) -> float:
        def clip01(x: float) -> float:
            return max(0.0, min(1.0, x))

        volume_term = clip01((vol_ratio - min_vol_ratio) / min_vol_ratio) if min_vol_ratio > 0 else 0.0
        gap_term = clip01(1.0 - (gap_percent / max_gap)) if max_gap > 0 else 0.0
        return round(0.6 * volume_term + 0.4 * gap_term, 4)

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
            data_as_of=now,
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
