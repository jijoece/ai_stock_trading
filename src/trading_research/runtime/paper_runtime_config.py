"""Loads and validates `config/paper_runtime.yaml` (docs/milestone-4.md Step
14). Same load-and-validate-strictly, fail-closed pattern as
`execution/config.py::load_execution_config` — an absent or unrecognized
`paper_broker.mode` is never interpreted as `"paper"`, and
`real_money_enabled` cannot be set to anything other than `false` here.
Never reads `os.environ` for either field — an operator cannot silently
enable a new capability via the shell; environment variables supply
credentials only, checked separately by the isolated runtime process
itself (see `paper_runtime/src/trading_paper_runtime/configuration.py`).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_PAPER_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "paper_runtime.yaml"

VALID_BROKER_MODES = ("paper",)
VALID_TRANSPORTS = ("stdio",)


class PaperRuntimeConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaperRuntimeConfig:
    protocol_version: str
    transport: str
    command: tuple[str, ...]
    startup_timeout_seconds: float
    request_timeout_seconds: float

    broker_provider: str
    broker_mode: str
    real_money_enabled: bool
    allowed_sides: tuple[str, ...]
    allowed_order_types: tuple[str, ...]
    allow_fractional: bool
    allow_shorting: bool
    allow_margin: bool
    allow_extended_hours: bool

    poll_interval_seconds: float
    max_poll_attempts: int
    stale_order_minutes: float

    evaluation_benchmark: str
    evaluation_horizons_trading_days: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.transport not in VALID_TRANSPORTS:
            raise PaperRuntimeConfigError(f"paper_runtime.transport {self.transport!r} is not supported")
        if not self.command:
            raise PaperRuntimeConfigError("paper_runtime.command must be a non-empty list")
        if self.broker_mode not in VALID_BROKER_MODES:
            raise PaperRuntimeConfigError(
                f"paper_broker.mode {self.broker_mode!r} is not recognized (fail closed — expected "
                f"one of {VALID_BROKER_MODES})"
            )
        if self.real_money_enabled:
            raise PaperRuntimeConfigError("paper_broker.real_money_enabled=true is not permitted — fail closed")
        if self.allow_fractional or self.allow_shorting or self.allow_margin or self.allow_extended_hours:
            raise PaperRuntimeConfigError(
                "fractional shares / shorting / margin / extended-hours trading are not permitted "
                "in this milestone — fail closed"
            )
        if self.startup_timeout_seconds <= 0 or self.request_timeout_seconds <= 0:
            raise PaperRuntimeConfigError("timeouts must be positive")
        if self.poll_interval_seconds <= 0 or self.max_poll_attempts <= 0:
            raise PaperRuntimeConfigError("order_monitoring polling settings must be positive")
        if not self.evaluation_benchmark:
            raise PaperRuntimeConfigError("evaluation.benchmark must be non-empty")
        if not self.evaluation_horizons_trading_days:
            raise PaperRuntimeConfigError("evaluation.horizons_trading_days must be non-empty")


def load_paper_runtime_config(path: str | Path | None = None) -> PaperRuntimeConfig:
    config_path = Path(path) if path else DEFAULT_PAPER_RUNTIME_CONFIG_PATH
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except OSError as exc:
        raise PaperRuntimeConfigError(f"cannot read paper runtime config at {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PaperRuntimeConfigError(f"invalid YAML in paper runtime config at {config_path}: {exc}") from exc

    required_top = {"version", "paper_runtime", "paper_broker", "order_monitoring", "evaluation"}
    missing = required_top - raw.keys()
    if missing:
        raise PaperRuntimeConfigError(f"paper runtime config missing keys: {sorted(missing)}")

    pr = raw["paper_runtime"] or {}
    pb = raw["paper_broker"] or {}
    om = raw["order_monitoring"] or {}
    ev = raw["evaluation"] or {}

    try:
        return PaperRuntimeConfig(
            protocol_version=str(pr["protocol_version"]),
            transport=str(pr["transport"]),
            command=tuple(pr["command"]),
            startup_timeout_seconds=float(pr["startup_timeout_seconds"]),
            request_timeout_seconds=float(pr["request_timeout_seconds"]),
            broker_provider=str(pb["provider"]),
            broker_mode=str(pb["mode"]).strip().lower(),
            real_money_enabled=bool(pb["real_money_enabled"]),
            allowed_sides=tuple(pb["allowed_sides"]),
            allowed_order_types=tuple(pb["allowed_order_types"]),
            allow_fractional=bool(pb["allow_fractional"]),
            allow_shorting=bool(pb["allow_shorting"]),
            allow_margin=bool(pb["allow_margin"]),
            allow_extended_hours=bool(pb["allow_extended_hours"]),
            poll_interval_seconds=float(om["poll_interval_seconds"]),
            max_poll_attempts=int(om["max_poll_attempts"]),
            stale_order_minutes=float(om["stale_order_minutes"]),
            evaluation_benchmark=str(ev["benchmark"]),
            evaluation_horizons_trading_days=tuple(ev["horizons_trading_days"]),
        )
    except KeyError as exc:
        raise PaperRuntimeConfigError(f"paper runtime config missing required key: {exc}") from exc
