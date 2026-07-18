"""Loads `config/shadow_operations.yaml` (docs/milestone-7.md Step 12).

Same fail-closed, `hash_config`-stamped pattern as every other config module
in this repository (see `research/scheduled_research_config.py` and
`evidence_providers/config.py`). `shadow_operations.enabled` and
`schedule.enabled` both default to `false`. `allow_enhanced_submission` is
structurally impossible to be `true` — `ShadowOperationsSection.__post_init__`
raises if it is ever `true`, exactly like
`research/promotion.py::PromotionGateConfig.allow_live_promotion`. No
environment variable is read anywhere in this module to decide a capability
— `.env` only ever supplies a credential, never a capability decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import REPO_ROOT
from ..hashing import hash_config

DEFAULT_SHADOW_OPERATIONS_CONFIG_PATH = REPO_ROOT / "config" / "shadow_operations.yaml"

KNOWN_MODES = ("SHADOW_ENHANCED",)
KNOWN_CADENCES = ("DAILY_MARKET_DAY",)


class ShadowOperationsConfigError(RuntimeError):
    pass


def _strict_bool(value: object, field_name: str) -> bool:
    """Accept only real YAML booleans (mirrors `paper_books/config.py::_strict_bool`
    and `research/scheduled_research_config.py::_strict_bool`). `bool(...)` on a raw
    YAML scalar is not sufficient — `bool("false")` and `bool("no")` are both `True`,
    so a permissive coercion here could silently enable a shadow-operations capability
    from a config value the operator wrote as disabled."""
    if type(value) is not bool:
        raise ShadowOperationsConfigError(f"{field_name} must be a boolean — got {value!r}")
    return value


def _strict_positive_int(value: object, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ShadowOperationsConfigError(f"{field_name} must be a positive integer")
    return value


def _strict_positive_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ShadowOperationsConfigError(f"{field_name} must be a positive number")
    return float(value)


@dataclass(frozen=True)
class ShadowOperationsSection:
    enabled: bool
    mode: str
    allow_baseline_paper_submission: bool
    allow_enhanced_submission: bool
    require_market_open_day: bool
    run_window_timezone: str
    run_window_start: str
    run_window_end: str
    max_catch_up_cycles: int
    lease_ttl_seconds: int
    stale_run_timeout_seconds: int
    continue_on_symbol_failure: bool

    def __post_init__(self) -> None:
        if self.allow_enhanced_submission:
            raise ShadowOperationsConfigError(
                "shadow_operations.allow_enhanced_submission=true is not permitted — fails closed"
            )
        if self.mode not in KNOWN_MODES:
            raise ShadowOperationsConfigError(
                f"shadow_operations.mode {self.mode!r} is not one of {KNOWN_MODES} — fails closed"
            )
        if self.max_catch_up_cycles <= 0:
            raise ShadowOperationsConfigError("shadow_operations.max_catch_up_cycles must be > 0")
        if self.lease_ttl_seconds <= 0:
            raise ShadowOperationsConfigError("shadow_operations.lease_ttl_seconds must be > 0")
        if self.stale_run_timeout_seconds <= 0:
            raise ShadowOperationsConfigError("shadow_operations.stale_run_timeout_seconds must be > 0")


@dataclass(frozen=True)
class ScheduleSection:
    enabled: bool
    cadence: str
    intended_local_time: str

    def __post_init__(self) -> None:
        if self.cadence not in KNOWN_CADENCES:
            raise ShadowOperationsConfigError(
                f"schedule.cadence {self.cadence!r} is not one of {KNOWN_CADENCES} — fails closed"
            )


@dataclass(frozen=True)
class BudgetsSection:
    require_pricing_for_real_claude: bool
    max_symbols_per_cycle: int
    max_roles_per_symbol: int
    max_attempts_per_role: int
    max_input_tokens_per_cycle: int
    max_output_tokens_per_cycle: int
    max_latency_seconds_per_cycle: int
    max_estimated_cost_per_cycle_usd: float
    max_actual_cost_per_day_usd: float
    max_actual_cost_per_month_usd: float
    emergency_margin_fraction: float
    max_claude_code_calls_per_cycle: int = 10
    max_claude_code_calls_per_day: int = 30
    max_claude_code_calls_per_month: int = 300
    max_claude_code_input_tokens_per_cycle: int = 100000
    max_claude_code_output_tokens_per_cycle: int = 50000
    max_claude_code_latency_seconds_per_role: int = 120
    max_claude_code_latency_seconds_per_cycle: int = 900
    max_claude_code_api_equivalent_cost_per_call_usd: float = 0.50
    max_claude_code_api_equivalent_cost_per_cycle_usd: float = 5.00
    max_claude_code_api_equivalent_cost_per_day_usd: float = 10.00
    max_claude_code_api_equivalent_cost_per_month_usd: float = 100.00

    def __post_init__(self) -> None:
        positive_int_fields = (
            "max_symbols_per_cycle", "max_roles_per_symbol", "max_attempts_per_role",
            "max_input_tokens_per_cycle", "max_output_tokens_per_cycle", "max_latency_seconds_per_cycle",
        )
        for field_name in positive_int_fields:
            if getattr(self, field_name) <= 0:
                raise ShadowOperationsConfigError(f"budgets.{field_name} must be > 0")
        for field_name in (
            "max_claude_code_calls_per_cycle", "max_claude_code_calls_per_day",
            "max_claude_code_calls_per_month", "max_claude_code_input_tokens_per_cycle",
            "max_claude_code_output_tokens_per_cycle", "max_claude_code_latency_seconds_per_role",
            "max_claude_code_latency_seconds_per_cycle",
        ):
            if type(getattr(self, field_name)) is not int or getattr(self, field_name) <= 0:
                raise ShadowOperationsConfigError(f"budgets.{field_name} must be a positive integer")
        positive_cost_fields = (
            "max_estimated_cost_per_cycle_usd", "max_actual_cost_per_day_usd", "max_actual_cost_per_month_usd",
        )
        for field_name in positive_cost_fields:
            if getattr(self, field_name) <= 0:
                raise ShadowOperationsConfigError(f"budgets.{field_name} must be > 0")
        for field_name in (
            "max_claude_code_api_equivalent_cost_per_call_usd",
            "max_claude_code_api_equivalent_cost_per_cycle_usd",
            "max_claude_code_api_equivalent_cost_per_day_usd",
            "max_claude_code_api_equivalent_cost_per_month_usd",
        ):
            if getattr(self, field_name) <= 0:
                raise ShadowOperationsConfigError(f"budgets.{field_name} must be > 0")
        if not (0.0 <= self.emergency_margin_fraction <= 1.0):
            raise ShadowOperationsConfigError("budgets.emergency_margin_fraction must be in [0,1]")


@dataclass(frozen=True)
class SafetySection:
    pause_on_provider_failure_rate: float
    pause_on_retry_exhaustion_rate: float
    pause_on_unsupported_claim_rate: float
    pause_on_reconciliation_mismatch: bool
    pause_on_budget_breach: bool
    # Milestone 11.3 Part 23: below this many provider requests in a cycle,
    # provider_failure_rate is INSUFFICIENT_DATA, not a computed pass/fail —
    # not a required key (defaults to 1, preserving prior "evaluate every
    # non-empty sample" behavior for configs written before this field
    # existed).
    minimum_requests_for_failure_rate: int = 1

    def __post_init__(self) -> None:
        rate_fields = (
            "pause_on_provider_failure_rate", "pause_on_retry_exhaustion_rate", "pause_on_unsupported_claim_rate",
        )
        for field_name in rate_fields:
            value = getattr(self, field_name)
            if not (0.0 <= value <= 1.0):
                raise ShadowOperationsConfigError(f"safety.{field_name} must be in [0,1]")
        if type(self.minimum_requests_for_failure_rate) is not int or self.minimum_requests_for_failure_rate < 1:
            raise ShadowOperationsConfigError("safety.minimum_requests_for_failure_rate must be an integer >= 1")


@dataclass(frozen=True)
class ShadowOperationsConfiguration:
    version: int
    shadow_operations: ShadowOperationsSection
    schedule: ScheduleSection
    budgets: BudgetsSection
    safety: SafetySection
    config_hash: str
    raw: dict


def load_shadow_operations_config(path: str | Path | None = None) -> ShadowOperationsConfiguration:
    config_path = Path(path) if path else DEFAULT_SHADOW_OPERATIONS_CONFIG_PATH
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except OSError as exc:
        raise ShadowOperationsConfigError(f"cannot read shadow-operations config at {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ShadowOperationsConfigError(f"invalid YAML in shadow-operations config at {config_path}: {exc}") from exc

    so = raw.get("shadow_operations")
    sched = raw.get("schedule")
    budgets = raw.get("budgets")
    safety = raw.get("safety")
    if not isinstance(so, dict):
        raise ShadowOperationsConfigError("shadow-operations config missing top-level 'shadow_operations' section")
    if not isinstance(sched, dict):
        raise ShadowOperationsConfigError("shadow-operations config missing top-level 'schedule' section")
    if not isinstance(budgets, dict):
        raise ShadowOperationsConfigError("shadow-operations config missing top-level 'budgets' section")
    if not isinstance(safety, dict):
        raise ShadowOperationsConfigError("shadow-operations config missing top-level 'safety' section")

    required_so_keys = {
        "enabled", "mode", "allow_baseline_paper_submission", "allow_enhanced_submission",
        "require_market_open_day", "run_window_timezone", "run_window_start", "run_window_end",
        "max_catch_up_cycles", "lease_ttl_seconds", "stale_run_timeout_seconds", "continue_on_symbol_failure",
    }
    missing = required_so_keys - so.keys()
    if missing:
        raise ShadowOperationsConfigError(f"shadow_operations config missing keys: {sorted(missing)}")

    required_sched_keys = {"enabled", "cadence", "intended_local_time"}
    missing = required_sched_keys - sched.keys()
    if missing:
        raise ShadowOperationsConfigError(f"schedule config missing keys: {sorted(missing)}")

    required_budget_keys = {
        "require_pricing_for_real_claude", "max_symbols_per_cycle", "max_roles_per_symbol",
        "max_attempts_per_role", "max_input_tokens_per_cycle", "max_output_tokens_per_cycle",
        "max_latency_seconds_per_cycle", "max_estimated_cost_per_cycle_usd", "max_actual_cost_per_day_usd",
        "max_actual_cost_per_month_usd", "emergency_margin_fraction",
    }
    missing = required_budget_keys - budgets.keys()
    if missing:
        raise ShadowOperationsConfigError(f"budgets config missing keys: {sorted(missing)}")

    required_safety_keys = {
        "pause_on_provider_failure_rate", "pause_on_retry_exhaustion_rate", "pause_on_unsupported_claim_rate",
        "pause_on_reconciliation_mismatch", "pause_on_budget_breach",
    }
    missing = required_safety_keys - safety.keys()
    if missing:
        raise ShadowOperationsConfigError(f"safety config missing keys: {sorted(missing)}")

    try:
        shadow_operations_section = ShadowOperationsSection(
            enabled=_strict_bool(so["enabled"], "shadow_operations.enabled"), mode=str(so["mode"]),
            allow_baseline_paper_submission=_strict_bool(
                so["allow_baseline_paper_submission"], "shadow_operations.allow_baseline_paper_submission"
            ),
            allow_enhanced_submission=_strict_bool(
                so["allow_enhanced_submission"], "shadow_operations.allow_enhanced_submission"
            ),
            require_market_open_day=_strict_bool(
                so["require_market_open_day"], "shadow_operations.require_market_open_day"
            ),
            run_window_timezone=str(so["run_window_timezone"]), run_window_start=str(so["run_window_start"]),
            run_window_end=str(so["run_window_end"]), max_catch_up_cycles=int(so["max_catch_up_cycles"]),
            lease_ttl_seconds=int(so["lease_ttl_seconds"]),
            stale_run_timeout_seconds=int(so["stale_run_timeout_seconds"]),
            continue_on_symbol_failure=_strict_bool(
                so["continue_on_symbol_failure"], "shadow_operations.continue_on_symbol_failure"
            ),
        )
        schedule_section = ScheduleSection(
            enabled=_strict_bool(sched["enabled"], "schedule.enabled"), cadence=str(sched["cadence"]),
            intended_local_time=str(sched["intended_local_time"]),
        )
        budgets_section = BudgetsSection(
            require_pricing_for_real_claude=_strict_bool(
                budgets["require_pricing_for_real_claude"], "budgets.require_pricing_for_real_claude"
            ),
            max_symbols_per_cycle=int(budgets["max_symbols_per_cycle"]),
            max_roles_per_symbol=int(budgets["max_roles_per_symbol"]),
            max_attempts_per_role=int(budgets["max_attempts_per_role"]),
            max_input_tokens_per_cycle=int(budgets["max_input_tokens_per_cycle"]),
            max_output_tokens_per_cycle=int(budgets["max_output_tokens_per_cycle"]),
            max_latency_seconds_per_cycle=int(budgets["max_latency_seconds_per_cycle"]),
            max_estimated_cost_per_cycle_usd=float(budgets["max_estimated_cost_per_cycle_usd"]),
            max_actual_cost_per_day_usd=float(budgets["max_actual_cost_per_day_usd"]),
            max_actual_cost_per_month_usd=float(budgets["max_actual_cost_per_month_usd"]),
            emergency_margin_fraction=float(budgets["emergency_margin_fraction"]),
            max_claude_code_calls_per_cycle=_strict_positive_int(budgets.get("max_claude_code_calls_per_cycle", 10), "budgets.max_claude_code_calls_per_cycle"),
            max_claude_code_calls_per_day=_strict_positive_int(budgets.get("max_claude_code_calls_per_day", 30), "budgets.max_claude_code_calls_per_day"),
            max_claude_code_calls_per_month=_strict_positive_int(budgets.get("max_claude_code_calls_per_month", 300), "budgets.max_claude_code_calls_per_month"),
            max_claude_code_input_tokens_per_cycle=_strict_positive_int(budgets.get("max_claude_code_input_tokens_per_cycle", 100000), "budgets.max_claude_code_input_tokens_per_cycle"),
            max_claude_code_output_tokens_per_cycle=_strict_positive_int(budgets.get("max_claude_code_output_tokens_per_cycle", 50000), "budgets.max_claude_code_output_tokens_per_cycle"),
            max_claude_code_latency_seconds_per_role=_strict_positive_int(budgets.get("max_claude_code_latency_seconds_per_role", 120), "budgets.max_claude_code_latency_seconds_per_role"),
            max_claude_code_latency_seconds_per_cycle=_strict_positive_int(budgets.get("max_claude_code_latency_seconds_per_cycle", 900), "budgets.max_claude_code_latency_seconds_per_cycle"),
            max_claude_code_api_equivalent_cost_per_call_usd=_strict_positive_number(budgets.get("max_claude_code_api_equivalent_cost_per_call_usd", 0.50), "budgets.max_claude_code_api_equivalent_cost_per_call_usd"),
            max_claude_code_api_equivalent_cost_per_cycle_usd=_strict_positive_number(budgets.get("max_claude_code_api_equivalent_cost_per_cycle_usd", 5.00), "budgets.max_claude_code_api_equivalent_cost_per_cycle_usd"),
            max_claude_code_api_equivalent_cost_per_day_usd=_strict_positive_number(budgets.get("max_claude_code_api_equivalent_cost_per_day_usd", 10.00), "budgets.max_claude_code_api_equivalent_cost_per_day_usd"),
            max_claude_code_api_equivalent_cost_per_month_usd=_strict_positive_number(budgets.get("max_claude_code_api_equivalent_cost_per_month_usd", 100.00), "budgets.max_claude_code_api_equivalent_cost_per_month_usd"),
        )
        safety_section = SafetySection(
            pause_on_provider_failure_rate=float(safety["pause_on_provider_failure_rate"]),
            pause_on_retry_exhaustion_rate=float(safety["pause_on_retry_exhaustion_rate"]),
            pause_on_unsupported_claim_rate=float(safety["pause_on_unsupported_claim_rate"]),
            pause_on_reconciliation_mismatch=_strict_bool(
                safety["pause_on_reconciliation_mismatch"], "safety.pause_on_reconciliation_mismatch"
            ),
            pause_on_budget_breach=_strict_bool(safety["pause_on_budget_breach"], "safety.pause_on_budget_breach"),
            minimum_requests_for_failure_rate=int(safety.get("minimum_requests_for_failure_rate", 1)),
        )
    except ShadowOperationsConfigError:
        raise
    except Exception as exc:
        raise ShadowOperationsConfigError(f"invalid shadow-operations config value: {exc}") from exc

    return ShadowOperationsConfiguration(
        version=raw.get("version", 1), shadow_operations=shadow_operations_section, schedule=schedule_section,
        budgets=budgets_section, safety=safety_section, config_hash=hash_config(raw), raw=raw,
    )
