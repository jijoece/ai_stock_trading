"""Loads `config/scheduled_research.yaml` (docs/milestone-6.md Step 20).
Same fail-closed, `hash_config`-stamped pattern as every other config module
in this repository. `scheduled_research.enabled` and
`scheduled_research.submit_paper_orders` both default to `false`;
`promotion.allow_live_promotion` is validated `false` by
`research/promotion.py::PromotionGateConfig.__post_init__` at construction
time, not just here.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import REPO_ROOT
from ..hashing import hash_config
from . import experiment_policy
from .promotion import PromotionGateConfig
from .scheduled_cycle import PROVIDER_MODE_REAL, ScheduledResearchConfiguration

DEFAULT_SCHEDULED_RESEARCH_CONFIG_PATH = REPO_ROOT / "config" / "scheduled_research.yaml"


class ScheduledResearchYamlConfigError(RuntimeError):
    pass


def _strict_bool(value: object, field_name: str) -> bool:
    """Reject YAML-permissive-but-dangerous shapes for a boolean execution/
    promotion gate: `"false"`/`"true"` (truthy string), `0`/`1` (truthy
    int), and `None` all previously coerced silently via `bool(...)` —
    `bool("false")` is `True`. Only an actual YAML boolean is accepted
    (mirrors `paper_books/config.py::_strict_bool`)."""
    if type(value) is not bool:
        raise ScheduledResearchYamlConfigError(f"{field_name} must be a boolean — got {value!r}")
    return value


@dataclass(frozen=True)
class ScheduledResearchYamlConfiguration:
    version: int
    enabled: bool
    universe_id: str
    max_candidates_per_cycle: int
    experiment_policy: str
    submit_paper_orders: bool
    require_complete_evidence: bool
    require_point_in_time_safe: bool
    continue_on_symbol_failure: bool
    promotion_enabled: bool
    promotion: PromotionGateConfig
    config_hash: str
    raw: dict

    def to_cycle_configuration(self, *, provider_mode: str = PROVIDER_MODE_REAL) -> ScheduledResearchConfiguration:
        return ScheduledResearchConfiguration(
            universe_id=self.universe_id, max_candidates_per_cycle=self.max_candidates_per_cycle,
            experiment_policy=self.experiment_policy, submit_paper_orders=self.submit_paper_orders,
            require_complete_evidence=self.require_complete_evidence,
            require_point_in_time_safe=self.require_point_in_time_safe,
            continue_on_symbol_failure=self.continue_on_symbol_failure, provider_mode=provider_mode,
            config_hash=self.config_hash,
        )


def load_scheduled_research_config(path: str | Path | None = None) -> ScheduledResearchYamlConfiguration:
    config_path = Path(path) if path else DEFAULT_SCHEDULED_RESEARCH_CONFIG_PATH
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except OSError as exc:
        raise ScheduledResearchYamlConfigError(f"cannot read scheduled-research config at {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ScheduledResearchYamlConfigError(f"invalid YAML in scheduled-research config at {config_path}: {exc}") from exc

    sr = raw.get("scheduled_research")
    promo = raw.get("promotion")
    if not isinstance(sr, dict):
        raise ScheduledResearchYamlConfigError("scheduled-research config missing top-level 'scheduled_research' section")
    if not isinstance(promo, dict):
        raise ScheduledResearchYamlConfigError("scheduled-research config missing top-level 'promotion' section")

    required_sr_keys = {
        "enabled", "universe_id", "max_candidates_per_cycle", "experiment_policy", "submit_paper_orders",
        "require_complete_evidence", "require_point_in_time_safe", "continue_on_symbol_failure",
    }
    missing = required_sr_keys - sr.keys()
    if missing:
        raise ScheduledResearchYamlConfigError(f"scheduled_research config missing keys: {sorted(missing)}")

    try:
        experiment_policy.validate_experiment_policy(sr["experiment_policy"])
    except Exception as exc:
        raise ScheduledResearchYamlConfigError(str(exc)) from exc

    required_promo_keys = {
        "enabled", "policy_version", "minimum_completed_evaluations", "minimum_market_regimes",
        "max_incomplete_analysis_rate", "max_unsupported_claim_rate", "max_provider_failure_rate",
        "max_retry_rate", "min_reproducibility_rate", "preferred_excess_return_margin", "allow_live_promotion",
    }
    missing_promo = required_promo_keys - promo.keys()
    if missing_promo:
        raise ScheduledResearchYamlConfigError(f"promotion config missing keys: {sorted(missing_promo)}")

    config_hash = hash_config(raw)
    promotion_config = PromotionGateConfig(
        policy_version=str(promo["policy_version"]),
        minimum_completed_evaluations=int(promo["minimum_completed_evaluations"]),
        minimum_market_regimes=int(promo["minimum_market_regimes"]),
        max_incomplete_analysis_rate=float(promo["max_incomplete_analysis_rate"]),
        max_unsupported_claim_rate=float(promo["max_unsupported_claim_rate"]),
        max_provider_failure_rate=float(promo["max_provider_failure_rate"]),
        max_retry_rate=float(promo["max_retry_rate"]),
        min_reproducibility_rate=float(promo["min_reproducibility_rate"]),
        preferred_excess_return_margin=float(promo["preferred_excess_return_margin"]),
        allow_live_promotion=_strict_bool(promo["allow_live_promotion"], "promotion.allow_live_promotion"),
    )

    return ScheduledResearchYamlConfiguration(
        version=raw.get("version", 1), enabled=_strict_bool(sr["enabled"], "scheduled_research.enabled"),
        universe_id=str(sr["universe_id"]),
        max_candidates_per_cycle=int(sr["max_candidates_per_cycle"]), experiment_policy=str(sr["experiment_policy"]),
        submit_paper_orders=_strict_bool(sr["submit_paper_orders"], "scheduled_research.submit_paper_orders"),
        require_complete_evidence=_strict_bool(sr["require_complete_evidence"], "scheduled_research.require_complete_evidence"),
        require_point_in_time_safe=_strict_bool(sr["require_point_in_time_safe"], "scheduled_research.require_point_in_time_safe"),
        continue_on_symbol_failure=_strict_bool(sr["continue_on_symbol_failure"], "scheduled_research.continue_on_symbol_failure"),
        promotion_enabled=_strict_bool(promo["enabled"], "promotion.enabled"),
        promotion=promotion_config, config_hash=config_hash, raw=raw,
    )
