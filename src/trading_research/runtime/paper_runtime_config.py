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
ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"


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
    broker_environment: str
    broker_base_url: str
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
        if self.protocol_version != "paper-runtime.v2":
            raise PaperRuntimeConfigError("paper_runtime.protocol_version must be exactly 'paper-runtime.v2'")
        if self.transport not in VALID_TRANSPORTS:
            raise PaperRuntimeConfigError(f"paper_runtime.transport {self.transport!r} is not supported")
        if not self.command:
            raise PaperRuntimeConfigError("paper_runtime.command must be a non-empty list")
        if self.broker_mode not in VALID_BROKER_MODES:
            raise PaperRuntimeConfigError(
                f"paper_broker.mode {self.broker_mode!r} is not recognized (fail closed — expected "
                f"one of {VALID_BROKER_MODES})"
            )
        if self.broker_provider != "alpaca":
            raise PaperRuntimeConfigError("paper_broker.provider must be 'alpaca'")
        if self.broker_environment != "paper":
            raise PaperRuntimeConfigError("paper_broker.environment must be exactly 'paper'")
        if self.broker_base_url != ALPACA_PAPER_BASE_URL:
            raise PaperRuntimeConfigError(
                f"paper_broker.base_url must be exactly {ALPACA_PAPER_BASE_URL!r} — live/HTTP/unknown hosts rejected"
            )
        if self.real_money_enabled:
            raise PaperRuntimeConfigError("paper_broker.real_money_enabled=true is not permitted — fail closed")
        if self.allow_fractional or self.allow_shorting or self.allow_margin or self.allow_extended_hours:
            raise PaperRuntimeConfigError(
                "fractional shares / shorting / margin / extended-hours trading are not permitted "
                "in this milestone — fail closed"
            )
        if self.allowed_sides != ("BUY", "SELL"):
            raise PaperRuntimeConfigError("paper_broker.allowed_sides must be exactly [BUY, SELL]")
        if self.allowed_order_types != ("LIMIT",):
            raise PaperRuntimeConfigError("paper_broker.allowed_order_types must be exactly [LIMIT]")
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
    extra = raw.keys() - required_top
    if extra:
        raise PaperRuntimeConfigError(f"paper runtime config has unknown keys: {sorted(extra)}")

    pr = raw["paper_runtime"] or {}
    pb = raw["paper_broker"] or {}
    om = raw["order_monitoring"] or {}
    ev = raw["evaluation"] or {}

    known_pr = {"protocol_version", "transport", "command", "startup_timeout_seconds", "request_timeout_seconds"}
    known_pb = {
        "provider", "mode", "environment", "base_url", "real_money_enabled", "asset_types",
        "allowed_sides", "allowed_order_types", "allow_fractional", "allow_shorting", "allow_margin",
        "allow_extended_hours",
    }
    known_om = {"poll_interval_seconds", "max_poll_attempts", "stale_order_minutes"}
    known_ev = {"benchmark", "horizons_trading_days"}
    for name, section, known in (
        ("paper_runtime", pr, known_pr), ("paper_broker", pb, known_pb),
        ("order_monitoring", om, known_om), ("evaluation", ev, known_ev),
    ):
        if not isinstance(section, dict):
            raise PaperRuntimeConfigError(f"{name} must be a mapping")
        unknown = section.keys() - known
        if unknown:
            raise PaperRuntimeConfigError(f"{name} has unknown keys: {sorted(unknown)}")

    for name in ("real_money_enabled", "allow_fractional", "allow_shorting", "allow_margin", "allow_extended_hours"):
        if type(pb.get(name)) is not bool:
            raise PaperRuntimeConfigError(f"paper_broker.{name} must be a strict boolean")
    if pb.get("asset_types") != ["equity"]:
        raise PaperRuntimeConfigError("paper_broker.asset_types must be exactly [equity]")

    try:
        return PaperRuntimeConfig(
            protocol_version=str(pr["protocol_version"]),
            transport=str(pr["transport"]),
            command=tuple(pr["command"]),
            startup_timeout_seconds=float(pr["startup_timeout_seconds"]),
            request_timeout_seconds=float(pr["request_timeout_seconds"]),
            broker_provider=str(pb["provider"]),
            broker_mode=str(pb["mode"]).strip().lower(),
            broker_environment=str(pb.get("environment", "paper")).strip().lower(),
            broker_base_url=str(pb.get("base_url", ALPACA_PAPER_BASE_URL)).strip(),
            real_money_enabled=pb["real_money_enabled"],
            allowed_sides=tuple(pb["allowed_sides"]),
            allowed_order_types=tuple(pb["allowed_order_types"]),
            allow_fractional=pb["allow_fractional"],
            allow_shorting=pb["allow_shorting"],
            allow_margin=pb["allow_margin"],
            allow_extended_hours=pb["allow_extended_hours"],
            poll_interval_seconds=float(om["poll_interval_seconds"]),
            max_poll_attempts=int(om["max_poll_attempts"]),
            stale_order_minutes=float(om["stale_order_minutes"]),
            evaluation_benchmark=str(ev["benchmark"]),
            evaluation_horizons_trading_days=tuple(ev["horizons_trading_days"]),
        )
    except KeyError as exc:
        raise PaperRuntimeConfigError(f"paper runtime config missing required key: {exc}") from exc
