from pathlib import Path

import pytest
import yaml

from trading_research.strategies.config import (
    ACTIVATION_STAGES,
    StrategyConfigError,
    load_strategy_config,
)

_BASE = {
    "version": 1,
    "momentum_breakout": {
        "enabled": True, "breakout_lookback_days": 20, "trend_sma_days": 50,
        "trend_slope_lookback_days": 5, "volume_lookback_days": 20, "minimum_volume_ratio": 1.5,
        "minimum_relative_strength": 0.0, "maximum_breakout_extension_percent": 3.0,
        "atr_period": 14, "atr_stop_multiple": 2.0, "maximum_holding_days": 20,
    },
    "mean_reversion": {
        "enabled": True, "mean_lookback_days": 20, "zscore_entry_threshold": -2.0, "rsi_period": 14,
        "maximum_entry_rsi": 30.0, "long_trend_sma_days": 200, "require_price_above_long_trend": True,
        "atr_period": 14, "atr_stop_multiple": 1.5, "maximum_holding_days": 10,
    },
    "event_catalyst": {
        "enabled": True, "maximum_event_age_hours": 72.0, "minimum_volume_ratio": 1.5,
        "volume_lookback_days": 20, "maximum_gap_percent": 8.0, "confirmation_window_days": 3,
        "maximum_holding_days": 15,
    },
    "shortlist": {
        "maximum_candidates_per_strategy": 5, "maximum_combined_shortlist": 10,
        "maximum_symbols_to_research_per_day": 3, "maximum_fresh_research_cycles_per_day": 3,
        "minimum_signal_strength_for_research": 0.0,
    },
}


def _write_config(tmp_path: Path, selection: dict) -> Path:
    payload = dict(_BASE)
    payload["strategy_candidate_selection"] = selection
    path = tmp_path / "strategies.yaml"
    path.write_text(yaml.safe_dump(payload))
    return path


def test_tracked_default_stays_at_historical_backtest_stage_with_selection_disabled():
    config = load_strategy_config()
    assert config.strategy_candidate_selection_enabled is False
    assert config.activation_stage in ACTIVATION_STAGES


def test_missing_activation_stage_fails_closed(tmp_path):
    path = _write_config(tmp_path, {"enabled": False})
    with pytest.raises(StrategyConfigError):
        load_strategy_config(path)


def test_unknown_activation_stage_fails_closed(tmp_path):
    path = _write_config(tmp_path, {"enabled": False, "activation_stage": "STAGE_99_MADE_UP"})
    with pytest.raises(StrategyConfigError):
        load_strategy_config(path)


def test_enabling_selection_at_stage_one_fails_closed(tmp_path):
    path = _write_config(tmp_path, {"enabled": True, "activation_stage": "STAGE_1_OFFLINE_FIXTURES"})
    with pytest.raises(StrategyConfigError):
        load_strategy_config(path)


def test_enabling_selection_at_stage_two_fails_closed(tmp_path):
    path = _write_config(tmp_path, {"enabled": True, "activation_stage": "STAGE_2_HISTORICAL_BACKTEST"})
    with pytest.raises(StrategyConfigError):
        load_strategy_config(path)


def test_enabling_selection_at_stage_three_is_allowed(tmp_path):
    path = _write_config(
        tmp_path, {"enabled": True, "activation_stage": "STAGE_3_DAILY_READ_ONLY_CANDIDATE_LIST"},
    )
    config = load_strategy_config(path)
    assert config.strategy_candidate_selection_enabled is True
    assert config.activation_stage == "STAGE_3_DAILY_READ_ONLY_CANDIDATE_LIST"
