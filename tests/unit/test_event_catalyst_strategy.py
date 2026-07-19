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
    closes = [100.0] * MIN_BARS  # no gap
    volumes = [1_000_000] * MIN_BARS  # no volume confirmation
    bars = build_bars(closes, volumes=volumes)
    market_data = StrategyMarketData(symbol="TEST", bars=bars, events=(_event(),))
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "unconfirmed_price_response" in signal.reason_codes


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
