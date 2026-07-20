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
from .factors import average_true_range
from .safety_gates import classify_safety_status
from .timestamps import bar_series_data_as_of

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
        minimum_bars = max(
            cfg.volume_lookback_days + cfg.confirmation_window_days + 1, cfg.atr_period + 1,
        )

        safety = classify_safety_status(context.screening_result, market_data.bars, minimum_bars)
        if safety is not None:
            status, reasons = safety
            return self._signal(symbol, context.now, status, 0.0, reasons, {})

        bars = market_data.bars
        confirming_bar_data_as_of, future_reason = bar_series_data_as_of(bars, context.now)
        if future_reason is not None:
            return self._signal(symbol, context.now, StrategyStatus.INCOMPLETE, 0.0, (future_reason,), {})

        events = [e for e in market_data.events if e.symbol == symbol]
        if not events:
            return self._signal(
                symbol, context.now, StrategyStatus.NOT_ELIGIBLE, 0.0,
                ("no_event_present",), {},
            )

        # Milestone 24 Part B1: neither an event that has not yet happened
        # nor one that has not yet been published may be treated as known
        # at this decision point — both would leak future information into
        # a point-in-time evaluation.
        for event in events:
            if event.event_timestamp > context.now:
                return self._signal(
                    symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                    (f"event_timestamp_after_decision_timestamp:{event.event_id}",), {},
                )
            if event.published_timestamp > context.now:
                return self._signal(
                    symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                    (f"published_timestamp_after_decision_timestamp:{event.event_id}",), {},
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
        # Milestone 25 Part B5: select the most recent qualifying positive
        # event, not the oldest — an older event must never mask a newer
        # material one. Ordered by published_timestamp desc (when the
        # information became knowable to the market), then
        # effective_timestamp desc, then event_timestamp desc, with
        # event_id ascending as the final deterministic tie-break.
        selected_event = min(
            positive_events,
            key=lambda e: (
                -e.published_timestamp.timestamp(),
                -e.effective_timestamp.timestamp(),
                -e.event_timestamp.timestamp(),
                e.event_id,
            ),
        )

        atr = average_true_range(bars, cfg.atr_period)
        if atr is None:
            return self._signal(
                symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                ("missing_atr_history",), {},
            )

        # Milestone 25 Part B4/B6: align the reference/confirmation bars to
        # the selected event's actual market timing rather than always
        # comparing the latest close to the immediately previous close.
        reference_index, first_tradable_index = self._reference_and_first_tradable_index(
            bars, selected_event.published_timestamp,
        )
        if reference_index is None:
            return self._signal(
                symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                ("no_pre_event_reference_bar",), {},
            )
        if first_tradable_index is None:
            return self._signal(
                symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                ("no_first_post_event_session",), {},
            )
        last_bar_index = len(bars) - 1
        if first_tradable_index > last_bar_index:
            return self._signal(
                symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                ("no_confirmation_bar",), {},
            )
        if first_tradable_index < cfg.volume_lookback_days:
            return self._signal(
                symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                ("missing_volume_history",), {},
            )

        pre_event_reference_close = float(bars[reference_index].close)
        confirmation_bar = bars[last_bar_index]
        confirmation_close = float(confirmation_bar.close)
        # Milestone 25 Part B7: confirmation_window_days is interpreted as a
        # maximum count of market *sessions* after the first tradable
        # session, not calendar days.
        sessions_since_first_tradable_session = last_bar_index - first_tradable_index

        window_bars = bars[first_tradable_index:last_bar_index + 1]
        baseline_bars = bars[first_tradable_index - cfg.volume_lookback_days:first_tradable_index]
        baseline_avg_volume = sum(b.volume for b in baseline_bars) / cfg.volume_lookback_days
        if baseline_avg_volume <= 0:
            return self._signal(
                symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                ("missing_volume_history",), {},
            )
        window_avg_volume = sum(b.volume for b in window_bars) / len(window_bars)
        post_event_volume_ratio = window_avg_volume / baseline_avg_volume

        # Milestone 25 Part B1: data_as_of covers every bar actually used
        # (via the already-verified full-series bound) plus the selected
        # event's own knowability timestamps.
        data_as_of_candidates = [selected_event.published_timestamp, confirming_bar_data_as_of]
        if selected_event.effective_timestamp <= context.now:
            data_as_of_candidates.append(selected_event.effective_timestamp)
        data_as_of = max(data_as_of_candidates)

        if pre_event_reference_close <= 0:
            return self._signal(
                symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                ("no_pre_event_reference_bar",), {}, data_as_of=data_as_of,
            )

        # Milestone 25 Part B8: the event-aligned response, not the latest
        # one-day return.
        event_response_percent = (confirmation_close - pre_event_reference_close) / pre_event_reference_close * 100.0
        maximum_post_event_extension_percent = max(
            abs(float(b.close) - pre_event_reference_close) / pre_event_reference_close * 100.0
            for b in window_bars
        )

        within_window = sessions_since_first_tradable_session < cfg.confirmation_window_days
        volume_confirmed = post_event_volume_ratio >= cfg.minimum_volume_ratio
        response_confirmed = event_response_percent >= cfg.minimum_positive_response_percent
        extension_ok = maximum_post_event_extension_percent <= cfg.maximum_gap_percent

        reasons = (
            "recent_positive_event_confirmed",
            ("volume_confirmed" if volume_confirmed else "volume_insufficient"),
            ("response_confirmed" if response_confirmed else "insufficient_price_response"),
            ("gap_ok" if extension_ok else "excessive_gap"),
            ("within_confirmation_window" if within_window else "confirmation_window_expired"),
        )
        factor_values = {
            "volume_ratio": post_event_volume_ratio,
            "gap_percent": maximum_post_event_extension_percent,
            "signed_response_percent": event_response_percent,
            "atr": atr,
            "event_age_hours": (context.now - selected_event.event_timestamp).total_seconds() / 3600.0,
            "pre_event_reference_close": pre_event_reference_close,
            "confirmation_close": confirmation_close,
            "event_response_percent": event_response_percent,
            "maximum_post_event_extension_percent": maximum_post_event_extension_percent,
            "post_event_volume_ratio": post_event_volume_ratio,
            "sessions_since_first_tradable_session": float(sessions_since_first_tradable_session),
            "selected_event_age_hours": (context.now - selected_event.event_timestamp).total_seconds() / 3600.0,
        }

        eligible = volume_confirmed and response_confirmed and extension_ok and within_window
        if not eligible:
            return self._signal(symbol, context.now,
                                 StrategyStatus.NOT_ELIGIBLE, 0.0, reasons, factor_values,
                                 data_as_of=data_as_of)

        signal_strength = self._signal_strength(
            post_event_volume_ratio, cfg.minimum_volume_ratio,
            maximum_post_event_extension_percent, cfg.maximum_gap_percent,
        )

        entry = Decimal(str(confirmation_close))
        # Milestone 24 Part B4: event-catalyst signals previously had no
        # invalidation price or initial stop at all, so an eligible signal
        # could never pass `build_strategy_order_intent_context`. Derive a
        # deterministic positive stop from ATR, same convention as the
        # other two strategies; never fabricate one when ATR/the resulting
        # stop is not usable.
        stop = Decimal(str(round(confirmation_close - cfg.atr_stop_multiple * atr, 4)))
        if stop <= 0 or stop >= entry:
            return self._signal(
                symbol, context.now, StrategyStatus.INCOMPLETE, 0.0,
                ("invalid_atr_stop",), factor_values, data_as_of=data_as_of,
            )
        return self._signal(
            symbol, context.now, StrategyStatus.ELIGIBLE, signal_strength, reasons, factor_values,
            entry_reference=entry, limit_reference=entry, invalidation_price=stop,
            initial_stop_reference=stop, target_reference=None,
            expected_holding_period=cfg.maximum_holding_days, data_as_of=data_as_of,
        )

    @staticmethod
    def _reference_and_first_tradable_index(
        bars: tuple, published_timestamp: datetime,
    ) -> tuple[int | None, int | None]:
        """Milestone 25 Part B6: deterministic, daily-bars-only market-timing
        alignment for one event's `published_timestamp`.

        - A bar exists for the published calendar date and publication
          happened before that bar's `available_at` (i.e. during market
          hours, before the session closed): the reference is the last
          completed bar *before* that session, and — because a still-daily
          bar cannot prove whether the still-open session already reflects
          the news — the first tradable confirmation session is
          conservatively the *next* session after the published day.
        - A bar exists for the published date and publication happened at
          or after that bar's `available_at` (published after market
          close): the reference is that session's own completed close, and
          the first tradable session is the next one.
        - No bar exists for the published date (weekend/holiday): the
          reference is the last completed session strictly before
          publication, and the first tradable session is the next
          available session on or after publication.

        Returns `(reference_index, first_tradable_index)`; either may be
        `None` when it cannot be determined from the available bars.
        """
        published_date = published_timestamp.date()
        same_day_index = next((i for i, b in enumerate(bars) if b.session_date == published_date), None)
        if same_day_index is not None:
            bar = bars[same_day_index]
            if published_timestamp < bar.available_at:
                reference_index = same_day_index - 1 if same_day_index > 0 else None
                first_tradable_index = same_day_index + 1 if same_day_index + 1 < len(bars) else None
            else:
                reference_index = same_day_index
                first_tradable_index = same_day_index + 1 if same_day_index + 1 < len(bars) else None
        else:
            prior_indices = [i for i, b in enumerate(bars) if b.session_date < published_date]
            if not prior_indices:
                return None, None
            reference_index = prior_indices[-1]
            after_indices = [i for i, b in enumerate(bars) if b.session_date > published_date]
            first_tradable_index = after_indices[0] if after_indices else None
        return reference_index, first_tradable_index

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
