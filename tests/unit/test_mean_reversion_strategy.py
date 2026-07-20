from trading_research.models.trading_models import CatalystRiskFlags, DataFreshness
from trading_research.strategies.config import load_strategy_config
from trading_research.strategies.contracts import StrategyContext, StrategyMarketData, StrategyStatus
from trading_research.strategies.mean_reversion import MeanReversionStrategy

from tests.unit._strategy_test_helpers import NOW, build_bars, passing_screening_result, stale_screening_result

CONFIG = load_strategy_config().mean_reversion
STRATEGY = MeanReversionStrategy(CONFIG)


def _context(screening_result=None) -> StrategyContext:
    return StrategyContext(now=NOW, screening_result=screening_result or passing_screening_result())


def _catalyst(**overrides) -> CatalystRiskFlags:
    defaults = dict(symbol="TEST", freshness=DataFreshness(source="fixture", as_of=NOW))
    defaults.update(overrides)
    return CatalystRiskFlags(**defaults)


def _oversold_closes() -> list[float]:
    # 214-day uptrend (keeps SMA200 well below the recent price), then a sharp
    # 5-day pullback so the latest close is well below the short-term mean and
    # RSI < 30 while still staying above the long-term SMA (structural trend intact).
    uptrend = [50.0 + 0.5 * i for i in range(214)]
    drop = [156.5, 148.0, 139.0, 130.0, 120.0]
    return uptrend + drop


def test_valid_oversold_setup_is_eligible():
    closes = _oversold_closes()
    bars = build_bars(closes)
    market_data = StrategyMarketData(symbol="TEST", bars=bars, catalyst=_catalyst())
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.ELIGIBLE
    assert "zscore_oversold" in signal.reason_codes
    assert "rsi_oversold" in signal.reason_codes


def test_mean_reversion_target_is_above_entry():
    """Milestone 24 Part B5: the target must be the short-term mean, not the
    long-term SMA (eligibility already requires latest_close > long-term
    SMA, so using it as target always put the target below entry)."""
    closes = _oversold_closes()
    bars = build_bars(closes)
    market_data = StrategyMarketData(symbol="TEST", bars=bars, catalyst=_catalyst())
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.ELIGIBLE
    assert signal.target_reference is not None
    assert signal.target_reference > signal.entry_reference
    assert float(signal.target_reference) == round(signal.factor_values["short_term_mean"], 4)


def test_insufficient_deviation_is_not_eligible():
    # 224 bars of mild noise around 100 — a real (non-zero) but shallow stretch,
    # nowhere near the zscore_entry_threshold.
    closes = [100.0 + (1.0 if i % 2 == 0 else -1.0) for i in range(223)] + [99.0]
    bars = build_bars(closes)
    market_data = StrategyMarketData(symbol="TEST", bars=bars, catalyst=_catalyst())
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "insufficient_deviation" in signal.reason_codes


def test_oversold_but_long_term_trend_broken_is_not_eligible():
    # Structural decline: long-term downtrend so close stays below the 200d SMA
    # even after the short-term stretch — must not look "cheap" and pass.
    decline = [100.0 - 0.4 * i for i in range(219)]  # ends near 12.4, SMA200 much higher than latest
    drop = [d * 0.9 for d in decline[-5:]]
    closes = decline[:-5] + drop
    bars = build_bars(closes)
    market_data = StrategyMarketData(symbol="TEST", bars=bars, catalyst=_catalyst())
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "long_trend_broken" in signal.reason_codes


def test_severe_risk_flag_blocks_eligibility():
    closes = _oversold_closes()
    bars = build_bars(closes)
    market_data = StrategyMarketData(
        symbol="TEST", bars=bars,
        catalyst=_catalyst(sec_filing_risk_flags=("going_concern",)),
    )
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.NOT_ELIGIBLE
    assert "severe_risk_flag_present" in signal.reason_codes


def test_missing_catalyst_snapshot_is_incomplete():
    closes = _oversold_closes()
    bars = build_bars(closes)
    market_data = StrategyMarketData(symbol="TEST", bars=bars)
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.INCOMPLETE
    assert "missing_catalyst_risk_data" in signal.reason_codes


def test_missing_catalyst_freshness_is_incomplete():
    closes = _oversold_closes()
    bars = build_bars(closes)
    market_data = StrategyMarketData(
        symbol="TEST", bars=bars, catalyst=CatalystRiskFlags(symbol="TEST"),
    )
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.INCOMPLETE
    assert "missing_catalyst_risk_freshness" in signal.reason_codes


def test_future_catalyst_freshness_is_incomplete():
    closes = _oversold_closes()
    bars = build_bars(closes)
    from datetime import timedelta
    market_data = StrategyMarketData(
        symbol="TEST", bars=bars,
        catalyst=_catalyst(freshness=DataFreshness(source="fixture", as_of=NOW + timedelta(hours=1))),
    )
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.INCOMPLETE
    assert "future_catalyst_risk_data" in signal.reason_codes


def test_missing_rsi_history_is_incomplete():
    bars = build_bars([100.0] * 10)
    market_data = StrategyMarketData(symbol="TEST", bars=bars)
    signal = STRATEGY.evaluate("TEST", market_data, _context())
    assert signal.status == StrategyStatus.INCOMPLETE


def test_stale_data_is_stale():
    closes = _oversold_closes()
    bars = build_bars(closes)
    market_data = StrategyMarketData(symbol="TEST", bars=bars)
    signal = STRATEGY.evaluate("TEST", market_data, _context(stale_screening_result()))
    assert signal.status == StrategyStatus.STALE
