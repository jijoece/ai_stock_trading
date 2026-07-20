from datetime import time, timedelta, timezone

from trading_research.models.trading_models import DataFreshness
from trading_research.strategies.config import load_strategy_config
from trading_research.strategies.contracts import StrategyContext, StrategyMarketData, StrategyStatus
from trading_research.strategies.event_catalyst import EventDrivenCatalystStrategy
from trading_research.strategies.events import MarketEvent

from tests.unit._strategy_test_helpers import NOW, build_bars, passing_screening_result, stale_screening_result

CONFIG = load_strategy_config().event_catalyst
STRATEGY = EventDrivenCatalystStrategy(CONFIG)

# Milestone 25 Part B4/B6: the strategy now aligns confirmation to the
# selected event's actual published date against the bar series, so bars
# and the event must share a real calendar timeline (not an arbitrary fixed
# bars date range next to a NOW-relative event, as before).
_SESSIONS_BEFORE_LAST = 3          # last bar stays inside the 3-session confirmation window
_TOTAL_BARS = CONFIG.volume_lookback_days + 5  # first_tradable_index (= total-3) stays >= volume_lookback_days


def _context(screening_result=None) -> StrategyContext:
    return StrategyContext(now=NOW, screening_result=screening_result or passing_screening_result())


def _bars_and_event(
    *, response_close: float = 102.0, base_close: float = 100.0,
    positive_or_negative: int = 1, volume_spike: bool = True,
) -> tuple:
    """Builds a bar series ending on `NOW`'s calendar date with a positive
    event published (after that session's close) 3 sessions before the last
    bar, and the confirmation-window bars (from the day after publication
    through the last bar) moved to `response_close` with an optional volume
    spike."""
    total_bars = _TOTAL_BARS
    last_date = NOW.date()
    start = last_date - timedelta(days=total_bars - 1)
    event_day_index = total_bars - 1 - _SESSIONS_BEFORE_LAST

    closes = [base_close] * total_bars
    volumes = [1_000_000] * total_bars
    for i in range(event_day_index + 1, total_bars):
        closes[i] = response_close
        if volume_spike:
            volumes[i] = 3_000_000

    bars = build_bars(closes, volumes=volumes, start=start)
    published_timestamp = datetime_combine_utc(bars[event_day_index].session_date)
    event = _event(
        event_timestamp=published_timestamp, published_timestamp=published_timestamp,
        effective_timestamp=published_timestamp, positive_or_negative=positive_or_negative,
    )
    return bars, event


def datetime_combine_utc(session_date):
    from datetime import datetime
    return datetime.combine(session_date, time(22, 0), tzinfo=timezone.utc)  # 1h after that session's close


def _event(**overrides) -> MarketEvent:
    defaults = dict(
        event_id="evt-1",
        symbol="TEST",
        event_type="earnings_result",
        event_timestamp=NOW - timedelta(hours=2),
        published_timestamp=NOW - timedelta(hours=2),
        effective_timestamp=NOW - timedelta(hours=2),
        source="alpaca_news",
        source_reference="ref-1",
        confidence_source="provider_verified",
        positive_or_negative=1,
        freshness=DataFreshness(source="alpaca_news", as_of=NOW - timedelta(hours=2)),
    )
    defaults.update(overrides)
    return MarketEvent(**defaults)


def test_valid_recent_positive_event_is_eligible():
    bars, event = _bars_and_event()
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(event,))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.ELIGIBLE
    assert "recent_positive_event_confirmed" in signal.reason_codes


def test_stale_event_is_not_eligible():
    bars, _ = _bars_and_event()
    old_event = _event(
        event_timestamp=NOW - timedelta(hours=200),
        published_timestamp=NOW - timedelta(hours=200),
        effective_timestamp=NOW - timedelta(hours=200),
        freshness=DataFreshness(source="alpaca_news", as_of=NOW - timedelta(hours=200)),
    )
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(old_event,))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "stale_event" in signal.reason_codes


def test_negative_event_is_not_eligible():
    bars, _ = _bars_and_event()
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(_event(positive_or_negative=-1),))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "conflicting_risk_event" in signal.reason_codes


def test_conflicting_risk_event_blocks_positive_event():
    bars, event = _bars_and_event()
    events = (event, _event(event_id="evt-neg", event_timestamp=event.event_timestamp,
                             published_timestamp=event.published_timestamp,
                             effective_timestamp=event.effective_timestamp, positive_or_negative=-1))
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=events)
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "conflicting_risk_event" in signal.reason_codes


def test_unconfirmed_price_response_is_not_eligible():
    bars, event = _bars_and_event(response_close=100.0, volume_spike=False)  # no move, no volume confirmation
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(event,))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "volume_insufficient" in signal.reason_codes
    assert "insufficient_price_response" in signal.reason_codes


def test_negative_response_to_positive_event_is_not_eligible():
    """Milestone 24 Part B3: a positive event with a *negative* signed
    close-to-close response must never pass just because volume confirmed
    and the (absolute) gap is within the ceiling."""
    bars, event = _bars_and_event(response_close=98.0)  # -2% move, within the gap ceiling
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(event,))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "insufficient_price_response" in signal.reason_codes
    assert signal.factor_values["signed_response_percent"] < 0


def test_insufficient_positive_response_to_positive_event_is_not_eligible():
    bars, event = _bars_and_event(response_close=100.3)  # +0.3%, below minimum_positive_response_percent
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(event,))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "insufficient_price_response" in signal.reason_codes


def test_confirmed_positive_response_to_positive_event_is_eligible():
    bars, event = _bars_and_event()
    signal = STRATEGY.evaluate(
        "TEST", StrategyMarketData(symbol="TEST", bars=bars, events=(event,)),
        _context(),
    )
    assert signal.status == StrategyStatus.ELIGIBLE
    assert "response_confirmed" in signal.reason_codes
    assert signal.factor_values["signed_response_percent"] > 0


def test_excessive_positive_gap_is_not_eligible():
    bars, event = _bars_and_event(response_close=115.0)  # +15%, past maximum_gap_percent
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(event,))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "excessive_gap" in signal.reason_codes


def test_eligible_event_signal_has_a_valid_deterministic_stop():
    """Milestone 24 Part B4: an eligible event-catalyst signal must carry a
    positive stop below entry, and must pass execution-boundary
    construction (previously impossible — no stop was ever set)."""
    from trading_research.strategies.execution_boundary import build_strategy_order_intent_context

    bars, event = _bars_and_event()
    signal = STRATEGY.evaluate(
        "TEST", StrategyMarketData(symbol="TEST", bars=bars, events=(event,)),
        _context(),
    )
    assert signal.status == StrategyStatus.ELIGIBLE
    assert signal.initial_stop_reference is not None
    assert signal.invalidation_price is not None
    assert 0 < signal.initial_stop_reference < signal.entry_reference
    context = build_strategy_order_intent_context(signal)
    assert context.strategy_stop == signal.initial_stop_reference


def test_published_timestamp_after_decision_timestamp_is_incomplete():
    bars, _ = _bars_and_event()
    future_published = _event(published_timestamp=NOW + timedelta(hours=1), event_timestamp=NOW - timedelta(hours=2))
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(future_published,))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.INCOMPLETE
    assert any("published_timestamp_after_decision_timestamp" in r for r in signal.reason_codes)


def test_data_as_of_uses_source_availability_not_evaluation_clock():
    """Milestone 24 Part B1: data_as_of must be derived from source
    availability (event/bar timestamps), never simply `context.now`."""
    bars, event = _bars_and_event()
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(event,))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.ELIGIBLE
    assert signal.data_as_of is not None
    # data_as_of must equal the actual source-derived maximum — never
    # blindly substituted with context.now regardless of source timestamps
    # (it may legitimately coincide with `now` when the latest bar's own
    # availability happens to be the evaluation instant).
    assert signal.data_as_of == max(event.published_timestamp, event.effective_timestamp, bars[-1].available_at)
    assert signal.data_as_of <= NOW


def test_event_timestamp_after_decision_timestamp_is_incomplete():
    bars, _ = _bars_and_event()
    future_event = _event(
        event_timestamp=NOW + timedelta(hours=1),
        published_timestamp=NOW + timedelta(hours=1),
        effective_timestamp=NOW + timedelta(hours=1),
        freshness=DataFreshness(source="alpaca_news", as_of=NOW + timedelta(hours=1)),
    )
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(future_event,))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.INCOMPLETE
    assert any("event_timestamp_after_decision_timestamp" in r for r in signal.reason_codes)


def test_missing_provenance_is_rejected_at_construction():
    import pytest
    from trading_research.strategies.events import MarketEventError

    with pytest.raises(MarketEventError):
        _event(source="", source_reference="")


def test_stale_screener_data_is_stale():
    bars, _ = _bars_and_event()
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(_event(),))
    signal = STRATEGY.evaluate("TEST", market_data, _context(stale_screening_result()))
    assert signal.status == StrategyStatus.STALE


# --- Milestone 25 Part B: point-in-time event-alignment correctness ---


def test_newest_qualifying_event_is_selected_over_an_older_one():
    """Part B5: an older qualifying event must never mask a newer material
    one — the strategy must select by published_timestamp descending."""
    bars, newer_event = _bars_and_event()
    # An older positive event, published well before the selected one and
    # therefore not the one the confirmation window is aligned to.
    older_event = _event(
        event_id="evt-older",
        event_timestamp=newer_event.event_timestamp - timedelta(hours=1),
        published_timestamp=newer_event.published_timestamp - timedelta(hours=1),
        effective_timestamp=newer_event.effective_timestamp - timedelta(hours=1),
        positive_or_negative=1,
    )
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(older_event, newer_event))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.ELIGIBLE
    # event_age_hours reflects the newer (selected) event, not the older one.
    older_age_hours = (NOW - older_event.event_timestamp).total_seconds() / 3600.0
    assert signal.factor_values["event_age_hours"] < older_age_hours


def test_event_id_is_final_deterministic_tiebreak():
    """Part B5: when published/effective/event timestamps are fully tied,
    the lexicographically smallest event_id is selected deterministically."""
    bars, event_a = _bars_and_event()
    event_b = _event(
        event_id="evt-0-before",  # sorts before "evt-1" (the default event_id)
        event_timestamp=event_a.event_timestamp, published_timestamp=event_a.published_timestamp,
        effective_timestamp=event_a.effective_timestamp, positive_or_negative=1,
    )
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(event_a, event_b))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.ELIGIBLE


def test_confirmation_window_expired_is_not_eligible():
    """Part B7: confirmation_window_days is a session count — once the
    evaluation's confirmation bar falls outside that many sessions after
    the first tradable session, the signal is NOT_ELIGIBLE, not silently
    confirmed off a stale window. Under the production config,
    maximum_event_age_hours (72h) is tighter than what's needed for a
    3-session window to actually elapse, so a wider event-age allowance is
    used here to isolate the window-expiry path itself."""
    from dataclasses import replace

    wide_age_cfg = replace(CONFIG, maximum_event_age_hours=400.0)
    strategy = EventDrivenCatalystStrategy(wide_age_cfg)

    total_bars = CONFIG.volume_lookback_days + 10
    last_date = NOW.date()
    start = last_date - timedelta(days=total_bars - 1)
    # Publish far enough back that the confirmation window (3 sessions) has
    # long since elapsed by the time evaluation reaches the last bar.
    event_day_index = total_bars - 1 - (CONFIG.confirmation_window_days + 5)
    closes = [100.0] * total_bars
    volumes = [1_000_000] * total_bars
    for i in range(event_day_index + 1, total_bars):
        closes[i] = 102.0
        volumes[i] = 3_000_000
    bars = build_bars(closes, volumes=volumes, start=start)
    published_timestamp = datetime_combine_utc(bars[event_day_index].session_date)
    event = _event(
        event_timestamp=published_timestamp, published_timestamp=published_timestamp,
        effective_timestamp=published_timestamp, positive_or_negative=1,
    )
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(event,))
    signal = strategy.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "confirmation_window_expired" in signal.reason_codes


def test_no_pre_event_reference_bar_when_published_before_all_history():
    """Part B9: an event published during market hours on the very first
    bar of the supplied history (so no completed prior session exists) must
    fail closed rather than fabricate a reference price. Exercises the
    alignment helper directly — a realistic full evaluate() case (enough
    bar history for the safety gate, but genuinely no data before the
    event) cannot be constructed from a single contiguous daily-bar series
    that also satisfies the strategy's own maximum_event_age_hours."""
    bars, _ = _bars_and_event()
    published_during_first_session = datetime_combine_utc(bars[0].session_date) - timedelta(hours=20)
    reference_index, first_tradable_index = STRATEGY._reference_and_first_tradable_index(
        bars, published_during_first_session,
    )
    assert reference_index is None
    assert first_tradable_index == 1


def test_weekend_event_uses_last_prior_session_and_next_session_confirmation():
    """Part B6: an event published on a weekend/holiday (no bar for that
    calendar date) uses the last completed session strictly before it as
    the reference and the next available session as the first tradable
    one. Exercises the alignment helper directly against a bar series with
    a genuine gap (a removed weekday bar, simulating a holiday)."""
    bars, _ = _bars_and_event()
    gap_index = len(bars) // 2
    gap_date = bars[gap_index].session_date
    bars_with_gap = bars[:gap_index] + bars[gap_index + 1:]

    published_timestamp = datetime_combine_utc(gap_date)
    reference_index, first_tradable_index = STRATEGY._reference_and_first_tradable_index(
        bars_with_gap, published_timestamp,
    )
    assert reference_index is not None and bars_with_gap[reference_index].session_date < gap_date
    assert first_tradable_index is not None and bars_with_gap[first_tradable_index].session_date > gap_date
    # No bar in the gapped series sits exactly at the reference/first-tradable boundary.
    assert reference_index == first_tradable_index - 1
