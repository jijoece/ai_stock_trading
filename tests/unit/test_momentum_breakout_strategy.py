from trading_research.models.trading_models import TechnicalFactorInput
from trading_research.strategies.config import load_strategy_config
from trading_research.strategies.contracts import StrategyContext, StrategyMarketData, StrategyStatus
from trading_research.strategies.momentum_breakout import MomentumBreakoutStrategy

from tests.unit._strategy_test_helpers import NOW, build_bars, passing_screening_result, stale_screening_result

CONFIG = load_strategy_config().momentum_breakout
STRATEGY = MomentumBreakoutStrategy(CONFIG)


def _context(screening_result=None) -> StrategyContext:
    return StrategyContext(now=NOW, screening_result=screening_result or passing_screening_result())


def _flat_then_breakout_closes() -> list[float]:
    # ~80 bars of gentle uptrend, then a clean breakout above the 20d high on the last bar.
    base = [50.0 + 0.05 * i for i in range(79)]
    base.append(base[-1] * 1.02)  # clear breakout, ~2% extension (within the 3% cap)
    return base


def test_valid_breakout_is_eligible():
    closes = _flat_then_breakout_closes()
    volumes = [1_000_000] * (len(closes) - 1) + [3_000_000]
    bars = build_bars(closes, volumes=volumes)
    market_data = StrategyMarketData(
        symbol="TEST", bars=bars,
        technical=TechnicalFactorInput(symbol="TEST", relative_strength=1.0),
    )
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.ELIGIBLE
    assert "breakout_confirmed" in signal.reason_codes
    assert 0.0 < signal.signal_strength <= 1.0
    assert signal.initial_stop_reference is not None
    assert signal.initial_stop_reference < signal.entry_reference


def test_no_breakout_is_not_eligible():
    closes = [50.0] * 80  # flat, last close does not exceed prior high
    bars = build_bars(closes, volumes=[1_000_000] * 80)
    market_data = StrategyMarketData(
        symbol="TEST", bars=bars,
        technical=TechnicalFactorInput(symbol="TEST", relative_strength=1.0),
    )
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "no_breakout" in signal.reason_codes


def test_low_volume_breakout_is_not_eligible():
    closes = _flat_then_breakout_closes()
    volumes = [1_000_000] * len(closes)  # no volume confirmation on breakout bar
    bars = build_bars(closes, volumes=volumes)
    market_data = StrategyMarketData(
        symbol="TEST", bars=bars,
        technical=TechnicalFactorInput(symbol="TEST", relative_strength=1.0),
    )
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "volume_insufficient" in signal.reason_codes


def test_excessively_extended_breakout_is_not_eligible():
    base = [50.0 + 0.05 * i for i in range(79)]
    base.append(base[-1] * 1.20)  # far beyond maximum_breakout_extension_percent
    volumes = [1_000_000] * 79 + [3_000_000]
    bars = build_bars(base, volumes=volumes)
    market_data = StrategyMarketData(
        symbol="TEST", bars=bars,
        technical=TechnicalFactorInput(symbol="TEST", relative_strength=1.0),
    )
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "extended_beyond_limit" in signal.reason_codes


def test_negative_trend_is_not_eligible():
    base = [80.0 - 0.3 * i for i in range(79)]  # declining trend
    base.append(base[-1] * 1.05)
    volumes = [1_000_000] * 79 + [3_000_000]
    bars = build_bars(base, volumes=volumes)
    market_data = StrategyMarketData(
        symbol="TEST", bars=bars,
        technical=TechnicalFactorInput(symbol="TEST", relative_strength=1.0),
    )
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "trend_negative" in signal.reason_codes


def test_missing_bars_is_incomplete():
    bars = build_bars([50.0] * 5)
    market_data = StrategyMarketData(symbol="TEST", bars=bars)
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.INCOMPLETE


def test_stale_data_is_stale():
    closes = _flat_then_breakout_closes()
    bars = build_bars(closes, volumes=[3_000_000] * 80)
    market_data = StrategyMarketData(symbol="TEST", bars=bars)
    signal = STRATEGY.evaluate("TEST", market_data, _context(stale_screening_result()))
    assert signal.status == StrategyStatus.STALE


def test_data_as_of_is_derived_from_latest_bar_availability():
    """Milestone 24 Part B1: data_as_of must reflect the actual source bar's
    availability, never the evaluation clock (context.now) substituted in
    its place."""
    closes = _flat_then_breakout_closes()
    volumes = [1_000_000] * (len(closes) - 1) + [3_000_000]
    bars = build_bars(closes, volumes=volumes)
    market_data = StrategyMarketData(
        symbol="TEST", bars=bars,
        technical=TechnicalFactorInput(symbol="TEST", relative_strength=1.0),
    )
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.ELIGIBLE
    assert signal.data_as_of == bars[-1].available_at
    assert signal.data_as_of != NOW


def test_future_available_bar_is_rejected():
    """A bar whose available_at is after context.now must never be used —
    it would leak future information into a point-in-time decision."""
    from dataclasses import replace
    from datetime import timedelta

    closes = _flat_then_breakout_closes()
    volumes = [1_000_000] * (len(closes) - 1) + [3_000_000]
    bars = build_bars(closes, volumes=volumes)
    future_bar = replace(bars[-1], available_at=NOW + timedelta(days=1))
    bars = bars[:-1] + (future_bar,)
    market_data = StrategyMarketData(
        symbol="TEST", bars=bars,
        technical=TechnicalFactorInput(symbol="TEST", relative_strength=1.0),
    )
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.INCOMPLETE
    assert any("future_bar_available_at" in r for r in signal.reason_codes)
    assert signal.data_as_of is None
