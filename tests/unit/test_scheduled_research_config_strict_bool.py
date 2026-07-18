"""Milestone 11.3 Part 26: scheduled-research config booleans must be
strict — reject permissive truthy shapes (`"false"`, `0`, `1`, `None`)
instead of coercing them with `bool(...)`."""
from __future__ import annotations

import pytest
import yaml

from trading_research.research.scheduled_research_config import (
    ScheduledResearchYamlConfigError,
    load_scheduled_research_config,
)

_VALID_RAW = {
    "version": 1,
    "scheduled_research": {
        "enabled": False,
        "universe_id": "default_seed_universe",
        "max_candidates_per_cycle": 10,
        "experiment_policy": "SHADOW_ENHANCED",
        "submit_paper_orders": False,
        "require_complete_evidence": True,
        "require_point_in_time_safe": True,
        "continue_on_symbol_failure": True,
    },
    "promotion": {
        "enabled": False,
        "policy_version": "research-promotion.v1",
        "minimum_completed_evaluations": 100,
        "minimum_market_regimes": 2,
        "max_incomplete_analysis_rate": 0.3,
        "max_unsupported_claim_rate": 0.05,
        "max_provider_failure_rate": 0.2,
        "max_retry_rate": 0.5,
        "min_reproducibility_rate": 0.95,
        "preferred_excess_return_margin": 0.02,
        "allow_live_promotion": False,
    },
}


def _write(tmp_path, raw: dict):
    path = tmp_path / "scheduled_research.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def test_real_booleans_load_successfully(tmp_path):
    cfg = load_scheduled_research_config(_write(tmp_path, _VALID_RAW))
    assert cfg.enabled is False
    assert cfg.submit_paper_orders is False
    assert cfg.promotion.allow_live_promotion is False


@pytest.mark.parametrize("field", ["enabled", "submit_paper_orders", "require_complete_evidence",
                                    "require_point_in_time_safe", "continue_on_symbol_failure"])
@pytest.mark.parametrize("bad_value", ["false", "true", 0, 1, None])
def test_permissive_scheduled_research_bool_rejected(tmp_path, field, bad_value):
    raw = {**_VALID_RAW, "scheduled_research": {**_VALID_RAW["scheduled_research"], field: bad_value}}
    with pytest.raises(ScheduledResearchYamlConfigError):
        load_scheduled_research_config(_write(tmp_path, raw))


@pytest.mark.parametrize("field", ["enabled", "allow_live_promotion"])
@pytest.mark.parametrize("bad_value", ["false", "true", 0, 1, None])
def test_permissive_promotion_bool_rejected(tmp_path, field, bad_value):
    raw = {**_VALID_RAW, "promotion": {**_VALID_RAW["promotion"], field: bad_value}}
    with pytest.raises(ScheduledResearchYamlConfigError):
        load_scheduled_research_config(_write(tmp_path, raw))


def test_live_promotion_and_execution_remain_disabled_by_default():
    cfg = load_scheduled_research_config()
    assert cfg.enabled is False
    assert cfg.submit_paper_orders is False
    assert cfg.promotion.allow_live_promotion is False
