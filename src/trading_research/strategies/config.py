"""Load and validate config/strategies.yaml (Milestone 23, B1-B6).

Same load-and-validate-strictly pattern as `analysis/screener.py` and
`execution/config.py`: a frozen dataclass with `__post_init__` checks, a
`config_hash` over the whole file for audit/reproducibility, and a
per-strategy `configuration_hash` (hash of just that strategy's sub-dict)
so a `StrategySignal.configuration_hash` is traceable without recomputing
the whole file's hash.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..hashing import hash_config

DEFAULT_STRATEGY_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "strategies.yaml"

# B9 activation progression. Each stage must be passed, in order, before the
# next is entered; `strategy_candidate_selection.activation_stage` in
# config/strategies.yaml records where the system currently stands.
ACTIVATION_STAGES = (
    "STAGE_1_OFFLINE_FIXTURES",
    "STAGE_2_HISTORICAL_BACKTEST",
    "STAGE_3_DAILY_READ_ONLY_CANDIDATE_LIST",
    "STAGE_4_SHADOW_RECOMMENDATION_TRACKING",
    "STAGE_5_LOCAL_PAPER_BOOK",
    "STAGE_6_SUPERVISED_ALPACA_PAPER",
    "STAGE_7_MULTI_DAY_PAPER_SOAK",
)


class StrategyConfigError(RuntimeError):
    """The strategy configuration is missing, malformed, or out of range."""


@dataclass(frozen=True)
class MomentumBreakoutConfig:
    enabled: bool
    breakout_lookback_days: int
    trend_sma_days: int
    trend_slope_lookback_days: int
    volume_lookback_days: int
    minimum_volume_ratio: float
    minimum_relative_strength: float
    maximum_breakout_extension_percent: float
    atr_period: int
    atr_stop_multiple: float
    maximum_holding_days: int
    configuration_hash: str

    def __post_init__(self) -> None:
        for name in (
            "breakout_lookback_days", "trend_sma_days", "trend_slope_lookback_days",
            "volume_lookback_days", "minimum_volume_ratio", "maximum_breakout_extension_percent",
            "atr_period", "atr_stop_multiple", "maximum_holding_days",
        ):
            if getattr(self, name) <= 0:
                raise StrategyConfigError(f"momentum_breakout.{name} must be > 0")


@dataclass(frozen=True)
class MeanReversionConfig:
    enabled: bool
    mean_lookback_days: int
    zscore_entry_threshold: float
    rsi_period: int
    maximum_entry_rsi: float
    long_trend_sma_days: int
    require_price_above_long_trend: bool
    atr_period: int
    atr_stop_multiple: float
    maximum_holding_days: int
    configuration_hash: str

    def __post_init__(self) -> None:
        for name in ("mean_lookback_days", "rsi_period", "maximum_entry_rsi",
                     "long_trend_sma_days", "atr_period", "atr_stop_multiple", "maximum_holding_days"):
            if getattr(self, name) <= 0:
                raise StrategyConfigError(f"mean_reversion.{name} must be > 0")
        if self.zscore_entry_threshold >= 0:
            raise StrategyConfigError("mean_reversion.zscore_entry_threshold must be negative")


@dataclass(frozen=True)
class EventCatalystConfig:
    enabled: bool
    maximum_event_age_hours: float
    minimum_volume_ratio: float
    volume_lookback_days: int
    maximum_gap_percent: float
    confirmation_window_days: int
    maximum_holding_days: int
    configuration_hash: str

    def __post_init__(self) -> None:
        for name in ("maximum_event_age_hours", "minimum_volume_ratio", "volume_lookback_days",
                     "maximum_gap_percent", "confirmation_window_days", "maximum_holding_days"):
            if getattr(self, name) <= 0:
                raise StrategyConfigError(f"event_catalyst.{name} must be > 0")


@dataclass(frozen=True)
class ShortlistConfig:
    maximum_candidates_per_strategy: int
    maximum_combined_shortlist: int
    maximum_symbols_to_research_per_day: int
    maximum_fresh_research_cycles_per_day: int
    minimum_signal_strength_for_research: float
    configuration_hash: str

    def __post_init__(self) -> None:
        for name in ("maximum_candidates_per_strategy", "maximum_combined_shortlist",
                     "maximum_symbols_to_research_per_day", "maximum_fresh_research_cycles_per_day"):
            if getattr(self, name) <= 0:
                raise StrategyConfigError(f"shortlist.{name} must be > 0")
        if not (0.0 <= self.minimum_signal_strength_for_research <= 1.0):
            raise StrategyConfigError("shortlist.minimum_signal_strength_for_research must be within [0.0, 1.0]")


@dataclass(frozen=True)
class StrategyConfig:
    version: int
    strategy_candidate_selection_enabled: bool
    activation_stage: str
    momentum_breakout: MomentumBreakoutConfig
    mean_reversion: MeanReversionConfig
    event_catalyst: EventCatalystConfig
    shortlist: ShortlistConfig
    config_hash: str
    raw: dict


def _sub_hash(raw: dict, key: str) -> str:
    return hash_config({key: raw[key]})


def load_strategy_config(path: str | Path | None = None) -> StrategyConfig:
    """Load and validate config/strategies.yaml. Fails closed on any problem."""
    config_path = Path(path) if path else DEFAULT_STRATEGY_CONFIG_PATH
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except OSError as exc:
        raise StrategyConfigError(f"cannot read strategy config at {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise StrategyConfigError(f"invalid YAML in strategy config at {config_path}: {exc}") from exc

    required_top = {"version", "strategy_candidate_selection", "momentum_breakout",
                     "mean_reversion", "event_catalyst", "shortlist"}
    missing = required_top - raw.keys()
    if missing:
        raise StrategyConfigError(f"strategy config missing keys: {sorted(missing)}")

    selection = raw["strategy_candidate_selection"] or {}
    if "enabled" not in selection:
        raise StrategyConfigError("strategy config strategy_candidate_selection missing key: 'enabled'")
    if "activation_stage" not in selection:
        raise StrategyConfigError("strategy config strategy_candidate_selection missing key: 'activation_stage'")
    if selection["activation_stage"] not in ACTIVATION_STAGES:
        raise StrategyConfigError(
            f"strategy config activation_stage {selection['activation_stage']!r} not one of {ACTIVATION_STAGES}"
        )
    if bool(selection["enabled"]) and selection["activation_stage"] in (
        "STAGE_1_OFFLINE_FIXTURES", "STAGE_2_HISTORICAL_BACKTEST",
    ):
        raise StrategyConfigError(
            "strategy_candidate_selection cannot be enabled while activation_stage is still "
            "offline-fixtures or historical-backtest only"
        )

    mb = raw["momentum_breakout"] or {}
    required_mb = {"enabled", "breakout_lookback_days", "trend_sma_days", "trend_slope_lookback_days",
                   "volume_lookback_days", "minimum_volume_ratio", "minimum_relative_strength",
                   "maximum_breakout_extension_percent", "atr_period", "atr_stop_multiple",
                   "maximum_holding_days"}
    missing_mb = required_mb - mb.keys()
    if missing_mb:
        raise StrategyConfigError(f"strategy config momentum_breakout missing keys: {sorted(missing_mb)}")

    mr = raw["mean_reversion"] or {}
    required_mr = {"enabled", "mean_lookback_days", "zscore_entry_threshold", "rsi_period",
                   "maximum_entry_rsi", "long_trend_sma_days", "require_price_above_long_trend",
                   "atr_period", "atr_stop_multiple", "maximum_holding_days"}
    missing_mr = required_mr - mr.keys()
    if missing_mr:
        raise StrategyConfigError(f"strategy config mean_reversion missing keys: {sorted(missing_mr)}")

    ec = raw["event_catalyst"] or {}
    required_ec = {"enabled", "maximum_event_age_hours", "minimum_volume_ratio", "volume_lookback_days",
                   "maximum_gap_percent", "confirmation_window_days", "maximum_holding_days"}
    missing_ec = required_ec - ec.keys()
    if missing_ec:
        raise StrategyConfigError(f"strategy config event_catalyst missing keys: {sorted(missing_ec)}")

    sl = raw["shortlist"] or {}
    required_sl = {"maximum_candidates_per_strategy", "maximum_combined_shortlist",
                   "maximum_symbols_to_research_per_day", "maximum_fresh_research_cycles_per_day",
                   "minimum_signal_strength_for_research"}
    missing_sl = required_sl - sl.keys()
    if missing_sl:
        raise StrategyConfigError(f"strategy config shortlist missing keys: {sorted(missing_sl)}")

    return StrategyConfig(
        version=raw["version"],
        strategy_candidate_selection_enabled=bool(selection["enabled"]),
        activation_stage=selection["activation_stage"],
        momentum_breakout=MomentumBreakoutConfig(
            enabled=bool(mb["enabled"]),
            breakout_lookback_days=int(mb["breakout_lookback_days"]),
            trend_sma_days=int(mb["trend_sma_days"]),
            trend_slope_lookback_days=int(mb["trend_slope_lookback_days"]),
            volume_lookback_days=int(mb["volume_lookback_days"]),
            minimum_volume_ratio=float(mb["minimum_volume_ratio"]),
            minimum_relative_strength=float(mb["minimum_relative_strength"]),
            maximum_breakout_extension_percent=float(mb["maximum_breakout_extension_percent"]),
            atr_period=int(mb["atr_period"]),
            atr_stop_multiple=float(mb["atr_stop_multiple"]),
            maximum_holding_days=int(mb["maximum_holding_days"]),
            configuration_hash=_sub_hash(raw, "momentum_breakout"),
        ),
        mean_reversion=MeanReversionConfig(
            enabled=bool(mr["enabled"]),
            mean_lookback_days=int(mr["mean_lookback_days"]),
            zscore_entry_threshold=float(mr["zscore_entry_threshold"]),
            rsi_period=int(mr["rsi_period"]),
            maximum_entry_rsi=float(mr["maximum_entry_rsi"]),
            long_trend_sma_days=int(mr["long_trend_sma_days"]),
            require_price_above_long_trend=bool(mr["require_price_above_long_trend"]),
            atr_period=int(mr["atr_period"]),
            atr_stop_multiple=float(mr["atr_stop_multiple"]),
            maximum_holding_days=int(mr["maximum_holding_days"]),
            configuration_hash=_sub_hash(raw, "mean_reversion"),
        ),
        event_catalyst=EventCatalystConfig(
            enabled=bool(ec["enabled"]),
            maximum_event_age_hours=float(ec["maximum_event_age_hours"]),
            minimum_volume_ratio=float(ec["minimum_volume_ratio"]),
            volume_lookback_days=int(ec["volume_lookback_days"]),
            maximum_gap_percent=float(ec["maximum_gap_percent"]),
            confirmation_window_days=int(ec["confirmation_window_days"]),
            maximum_holding_days=int(ec["maximum_holding_days"]),
            configuration_hash=_sub_hash(raw, "event_catalyst"),
        ),
        shortlist=ShortlistConfig(
            maximum_candidates_per_strategy=int(sl["maximum_candidates_per_strategy"]),
            maximum_combined_shortlist=int(sl["maximum_combined_shortlist"]),
            maximum_symbols_to_research_per_day=int(sl["maximum_symbols_to_research_per_day"]),
            maximum_fresh_research_cycles_per_day=int(sl["maximum_fresh_research_cycles_per_day"]),
            minimum_signal_strength_for_research=float(sl["minimum_signal_strength_for_research"]),
            configuration_hash=_sub_hash(raw, "shortlist"),
        ),
        config_hash=hash_config(raw),
        raw=raw,
    )
