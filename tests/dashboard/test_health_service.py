from pathlib import Path

from dashboard.services.health_service import HealthService


def test_loads_scheduler_pause_budget_policy_and_hysteresis(dashboard_database: Path):
    status = HealthService(dashboard_database).load().status

    assert status.shadow_pause_state == "RUNNING"
    assert status.recurring_activation_state == "ENABLED"
    assert status.latest_shadow_scheduler_status == "COMPLETED"
    assert status.latest_recurring_scheduler_status == "COMPLETED"
    assert status.latest_successful_run_at.isoformat() == "2026-07-17T15:05:00+00:00"
    assert status.health_status == "HEALTHY"
    assert status.hysteresis_status == "HEALTHY"
    assert status.budget_status == "SETTLED"
    assert status.active_policy_hash == "policy-hash"


def test_preserves_evidence_and_model_provider_partitions(dashboard_database: Path):
    providers = HealthService(dashboard_database).load().providers

    evidence = next(item for item in providers if item.provider_kind == "EVIDENCE")
    fixture_model = next(item for item in providers if item.provider == "fixture")
    codex = next(item for item in providers if item.provider == "codex")
    assert evidence.provider == "sec_edgar"
    assert evidence.timeout_failures == 1
    assert evidence.is_production is True
    assert fixture_model.provider == "fixture"
    assert fixture_model.model == "fixture-model"
    assert fixture_model.status == "NON_PRODUCTION"
    assert fixture_model.recovery_streak == 0
    assert codex.model == "gpt-5.4"
    assert codex.authentication_failures == 1
    assert codex.failure_streak == 1
