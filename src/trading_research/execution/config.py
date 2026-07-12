"""Trading-mode and paper-execution policy configuration (Milestone 3).

Same load-and-validate-strictly pattern as `analysis/screener.py` and
`analysis/scorer.py`: a dataclass with `__post_init__` checks, a
`config_hash` for audit/reproducibility, and a domain-specific error raised
at load time rather than a silent default. `trading_mode` and
`live_trading_enabled` are deliberately never read from an environment
variable here — repository policy (this file) is the only source, so an
operator cannot silently flip live trading on via the shell.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..hashing import hash_config

DEFAULT_EXECUTION_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "execution.yaml"

VALID_TRADING_MODES = ("paper",)  # "live" is intentionally not a recognized value yet


class ExecutionConfigError(RuntimeError):
    """The execution configuration is missing, malformed, or out of range."""


@dataclass(frozen=True)
class ExecutionConfig:
    version: int
    trading_mode: str
    live_trading_enabled: bool
    human_approval_required: bool
    kill_switch_enabled: bool
    policy_version: str
    execution_version: str
    recommendation_ttl_minutes: float
    max_price_staleness_seconds: float
    config_hash: str
    raw: dict

    def __post_init__(self) -> None:
        if self.trading_mode not in VALID_TRADING_MODES:
            raise ExecutionConfigError(
                f"trading_mode {self.trading_mode!r} is not recognized "
                f"(fail closed — expected one of {VALID_TRADING_MODES})"
            )
        if self.live_trading_enabled:
            raise ExecutionConfigError(
                "live_trading_enabled=true is not permitted in this milestone — fail closed"
            )
        if self.recommendation_ttl_minutes <= 0:
            raise ExecutionConfigError("paper_execution.recommendation_ttl_minutes must be > 0")
        if self.max_price_staleness_seconds <= 0:
            raise ExecutionConfigError("paper_execution.max_price_staleness_seconds must be > 0")
        if not self.policy_version:
            raise ExecutionConfigError("paper_execution.policy_version must be non-empty")
        if not self.execution_version:
            raise ExecutionConfigError("paper_execution.execution_version must be non-empty")


def load_execution_config(path: str | Path | None = None) -> ExecutionConfig:
    """Load and validate config/execution.yaml. Fails closed on any problem."""
    config_path = Path(path) if path else DEFAULT_EXECUTION_CONFIG_PATH
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except OSError as exc:
        raise ExecutionConfigError(f"cannot read execution config at {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ExecutionConfigError(f"invalid YAML in execution config at {config_path}: {exc}") from exc

    required_top = {"version", "trading_mode", "live_trading_enabled", "human_approval_required",
                     "kill_switch_enabled", "paper_execution"}
    missing = required_top - raw.keys()
    if missing:
        raise ExecutionConfigError(f"execution config missing keys: {sorted(missing)}")

    pe = raw["paper_execution"] or {}
    required_pe = {"policy_version", "execution_version", "recommendation_ttl_minutes",
                    "max_price_staleness_seconds"}
    missing_pe = required_pe - pe.keys()
    if missing_pe:
        raise ExecutionConfigError(f"execution config paper_execution missing keys: {sorted(missing_pe)}")

    return ExecutionConfig(
        version=raw["version"],
        trading_mode=str(raw["trading_mode"]).strip().lower(),
        live_trading_enabled=bool(raw["live_trading_enabled"]),
        human_approval_required=bool(raw["human_approval_required"]),
        kill_switch_enabled=bool(raw["kill_switch_enabled"]),
        policy_version=str(pe["policy_version"]),
        execution_version=str(pe["execution_version"]),
        recommendation_ttl_minutes=float(pe["recommendation_ttl_minutes"]),
        max_price_staleness_seconds=float(pe["max_price_staleness_seconds"]),
        config_hash=hash_config(raw),
        raw=raw,
    )
