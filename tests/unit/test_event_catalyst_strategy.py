from datetime import timedelta

from trading_research.models.trading_models import DataFreshness
from trading_research.strategies.config import load_strategy_config
from trading_research.strategies.contracts import StrategyContext, StrategyMarketData, StrategyStatus
from trading_research.strategies.event_catalyst import EventDrivenCatalystStrategy
from trading_research.strategies.events import MarketEvent

from tests.unit._strategy_test_helpers import NOW, build_bars, passing_screening_result, stale_screening_result

CONFIG = load_strategy_config().event_catalyst
STRATEGY = EventDrivenCatalystStrategy(CONFIG)
MIN_BARS = CONFIG.volume_lookback_days + CONFIG.confirmation_window_days + 1


def _context(screening_result=None) -> StrategyContext:
    return StrategyContext(now=NOW, screening_result=screening_result or passing_screening_result())


def _bars_with_confirmation() -> tuple:
    closes = [100.0] * (MIN_BARS - 1) + [102.0]  # small, non-excessive gap on the event day
    volumes = [1_000_000] * (MIN_BARS - 1) + [3_000_000]  # confirming volume spike
    return build_bars(closes, volumes=volumes)


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
    bars = _bars_with_confirmation()
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(_event(),))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.ELIGIBLE
    assert "recent_positive_event_confirmed" in signal.reason_codes


def test_stale_event_is_not_eligible():
    bars = _bars_with_confirmation()
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
    bars = _bars_with_confirmation()
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(_event(positive_or_negative=-1),))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "conflicting_risk_event" in signal.reason_codes


def test_conflicting_risk_event_blocks_positive_event():
    bars = _bars_with_confirmation()
    events = (_event(event_id="evt-pos", positive_or_negative=1),
              _event(event_id="evt-neg", positive_or_negative=-1))
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=events)
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "conflicting_risk_event" in signal.reason_codes


def test_unconfirmed_price_response_is_not_eligible():
    closes = [100.0] * MIN_BARS  # no gap, no move, no volume confirmation
    volumes = [1_000_000] * MIN_BARS
    bars = build_bars(closes, volumes=volumes)
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(_event(),))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "volume_insufficient" in signal.reason_codes
    assert "insufficient_price_response" in signal.reason_codes


def test_negative_response_to_positive_event_is_not_eligible():
    """Milestone 24 Part B3: a positive event with a *negative* signed
    close-to-close response must never pass just because volume confirmed
    and the (absolute) gap is within the ceiling."""
    closes = [100.0] * (MIN_BARS - 1) + [98.0]  # -2% move, within the gap ceiling
    volumes = [1_000_000] * (MIN_BARS - 1) + [3_000_000]  # confirming volume
    bars = build_bars(closes, volumes=volumes)
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(_event(),))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "insufficient_price_response" in signal.reason_codes
    assert signal.factor_values["signed_response_percent"] < 0


def test_insufficient_positive_response_to_positive_event_is_not_eligible():
    closes = [100.0] * (MIN_BARS - 1) + [100.3]  # +0.3%, below minimum_positive_response_percent
    volumes = [1_000_000] * (MIN_BARS - 1) + [3_000_000]
    bars = build_bars(closes, volumes=volumes)
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(_event(),))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "insufficient_price_response" in signal.reason_codes


def test_confirmed_positive_response_to_positive_event_is_eligible():
    signal = STRATEGY.evaluate(
        "TEST", StrategyMarketData(symbol="TEST", bars=_bars_with_confirmation(), events=(_event(),)),
        _context(),
    )
    assert signal.status == StrategyStatus.ELIGIBLE
    assert "response_confirmed" in signal.reason_codes
    assert signal.factor_values["signed_response_percent"] > 0


def test_excessive_positive_gap_is_not_eligible():
    closes = [100.0] * (MIN_BARS - 1) + [115.0]  # +15%, past maximum_gap_percent
    volumes = [1_000_000] * (MIN_BARS - 1) + [3_000_000]
    bars = build_bars(closes, volumes=volumes)
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(_event(),))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "excessive_gap" in signal.reason_codes


def test_eligible_event_signal_has_a_valid_deterministic_stop():
    """Milestone 24 Part B4: an eligible event-catalyst signal must carry a
    positive stop below entry, and must pass execution-boundary
    construction (previously impossible — no stop was ever set)."""
    from trading_research.strategies.execution_boundary import build_strategy_order_intent_context

    signal = STRATEGY.evaluate(
        "TEST", StrategyMarketData(symbol="TEST", bars=_bars_with_confirmation(), events=(_event(),)),
        _context(),
    )
    assert signal.status == StrategyStatus.ELIGIBLE
    assert signal.initial_stop_reference is not None
    assert signal.invalidation_price is not None
    assert 0 < signal.initial_stop_reference < signal.entry_reference
    context = build_strategy_order_intent_context(signal)
    assert context.strategy_stop == signal.initial_stop_reference


def test_published_timestamp_after_decision_timestamp_is_incomplete():
    bars = _bars_with_confirmation()
    future_published = _event(published_timestamp=NOW + timedelta(hours=1), event_timestamp=NOW - timedelta(hours=2))
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(future_published,))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.INCOMPLETE
    assert any("published_timestamp_after_decision_timestamp" in r for r in signal.reason_codes)


def test_data_as_of_uses_source_availability_not_evaluation_clock():
    """Milestone 24 Part B1: data_as_of must be derived from source
    availability (event/bar timestamps), never simply `context.now`."""
    bars = _bars_with_confirmation()
    event = _event()
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(event,))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.ELIGIBLE
    assert signal.data_as_of is not None
    assert signal.data_as_of != NOW
    assert signal.data_as_of == max(event.published_timestamp, event.effective_timestamp, bars[-1].available_at)
    assert signal.data_as_of <= NOW


def test_event_timestamp_after_decision_timestamp_is_incomplete():
    bars = _bars_with_confirmation()
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
    bars = _bars_with_confirmation()
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(_event(),))
    signal = STRATEGY.evaluate("TEST", market_data, _context(stale_screening_result()))
    assert signal.status == StrategyStatus.STALE
