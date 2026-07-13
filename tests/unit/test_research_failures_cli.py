"""Milestone 6.1 Step 15/19: CLI tests for `research_failures_cli` /
`research_failure_metrics_cli` — sanitized structured output, filters, safe failure on an
unknown run, no raw prompt/secret exposure."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_research.cli import research_failure_metrics_cli, research_failures_cli
from trading_research.research.configuration import ResearchConfiguration
from trading_research.research.deterministic_provider import ScriptedResearchProvider, ScriptedStep
from trading_research.research.fixtures import build_fixture_snapshot
from trading_research.research.orchestration import analyze_with_research_committee
from trading_research.research.prompt_registry import PromptRegistry
from trading_research.storage.database import connect
from trading_research.storage.research_repositories import SQLiteResearchRepository, save_evidence_snapshot

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)

ANALYST_REPORT_PAYLOAD = {
    "stance": "BULLISH", "summary": "growth", "claims": [], "catalysts": [], "risks": ["some risk"],
    "uncertainties": [], "missing_data_reasons": [],
}
BEAR_REPORT_WITH_INVENTED_DOWNSIDE = {
    "stance": "BEARISH", "summary": "material downside risk", "catalysts": [],
    "risks": ["revenue deceleration"], "uncertainties": [], "missing_data_reasons": [],
    "claims": [
        {
            "claim_id": "bear-claim-1", "claim_type": "downside_estimate",
            "statement": "Expected downside of -35% based on decelerating growth.",
            "evidence_ids": ["fixture-fundamentals-AAPL-revenue_growth_yoy"],
            "numeric_value": -0.35, "unit": "percent", "importance": "high",
        }
    ],
}


def _config() -> ResearchConfiguration:
    return ResearchConfiguration(
        version=1, enabled=True, provider="scripted", model="test-model", max_attempts_per_role=2,
        request_timeout_seconds=30, max_input_characters=100_000, max_evidence_items=100,
        max_items_per_source_category=25, max_claims_per_role=20, max_output_tokens=2000,
        require_point_in_time_safe=True, require_evidence_for_material_claims=True,
        fail_on_stale_required_evidence=True, allow_parallel_roles=False, roles=("fundamental", "bear", "manager"),
        overlay_policy_version="test.v1", overlay_allow_score_increase=False,
        overlay_allow_position_size_increase=False, overlay_incomplete_action="ANALYSIS_INCOMPLETE",
        overlay_critical_risk_action="FORCE_NO_ACTION", config_hash="c" * 64, raw={},
    )


@pytest.fixture()
def db_with_bear_failure(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    conn = connect(db_path)
    snapshot = build_fixture_snapshot("AAPL", NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)
    save_evidence_snapshot(conn, snapshot)
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload=ANALYST_REPORT_PAYLOAD),
        ("bear", 1): ScriptedStep(kind="response", payload=BEAR_REPORT_WITH_INVENTED_DOWNSIDE),
        ("bear", 2): ScriptedStep(kind="response", payload=BEAR_REPORT_WITH_INVENTED_DOWNSIDE),
    })
    repo = SQLiteResearchRepository(conn)
    result = analyze_with_research_committee(
        snapshot, provider=provider, provider_name="scripted", model_name="test-model",
        prompt_registry=PromptRegistry(), research_repository=repo, configuration=_config(), clock=lambda: NOW,
        run_mode="scripted",
    )
    conn.close()
    return db_path, result.research_run_id


def test_full_run_query_returns_all_failures(db_with_bear_failure):
    db_path, run_id = db_with_bear_failure
    outcome = research_failures_cli(run_id, db_path)
    assert outcome["research_run_id"] == run_id
    assert outcome["total_failures"] >= 4  # 2 numeric + retry-exhausted + required-role-failed (+ manager-skipped)
    assert "counts_by_stage" in outcome
    assert "counts_by_code" in outcome


def test_prompt_version_visible_in_diagnostics(db_with_bear_failure):
    """The hardened bear/v2.txt prompt's version is visible per-failure through the CLI,
    not just held in memory — proves persistence, not just construction."""
    db_path, run_id = db_with_bear_failure
    outcome = research_failures_cli(run_id, db_path, role="bear")
    assert outcome["total_failures"] > 0
    assert all(f["prompt_version"] == "v2" for f in outcome["failures"])


def test_role_filter(db_with_bear_failure):
    db_path, run_id = db_with_bear_failure
    outcome = research_failures_cli(run_id, db_path, role="bear")
    assert outcome["total_failures"] > 0
    assert all(f["role"] == "bear" for f in outcome["failures"])


def test_stage_filter(db_with_bear_failure):
    db_path, run_id = db_with_bear_failure
    outcome = research_failures_cli(run_id, db_path, stage="MANAGER_SKIPPED")
    assert outcome["total_failures"] == 1
    assert outcome["failures"][0]["stage"] == "MANAGER_SKIPPED"


def test_code_filter(db_with_bear_failure):
    db_path, run_id = db_with_bear_failure
    outcome = research_failures_cli(run_id, db_path, code="NUMERIC_VALUE_MISMATCH")
    assert outcome["total_failures"] == 2
    assert all(f["code"] == "NUMERIC_VALUE_MISMATCH" for f in outcome["failures"])


def test_attempt_filter(db_with_bear_failure):
    db_path, run_id = db_with_bear_failure
    outcome = research_failures_cli(run_id, db_path, attempt_number=1)
    assert all(f["attempt_number"] == 1 for f in outcome["failures"])


def test_missing_run_fails_safely(db_with_bear_failure):
    db_path, _run_id = db_with_bear_failure
    outcome = research_failures_cli("run-does-not-exist", db_path)
    assert outcome == {"error": "no persisted research run 'run-does-not-exist'"}


def test_sanitized_output_contains_no_raw_prompt_or_secret(db_with_bear_failure):
    db_path, run_id = db_with_bear_failure
    outcome = research_failures_cli(run_id, db_path)
    serialized = str(outcome).lower()
    assert "system_prompt" not in serialized
    assert "user_prompt" not in serialized
    assert "api_key" not in serialized
    assert "sk-ant-" not in serialized
    assert "authorization" not in serialized


def test_output_only_contains_documented_fields(db_with_bear_failure):
    db_path, run_id = db_with_bear_failure
    outcome = research_failures_cli(run_id, db_path)
    expected_keys = {
        "attempt_id", "role", "attempt_number", "stage", "code", "field_path", "claim_id", "evidence_ids",
        "sanitized_message", "retryable", "stop_reason", "input_tokens", "output_tokens", "prompt_version",
        "schema_version", "occurred_at",
    }
    for failure in outcome["failures"]:
        assert set(failure.keys()) == expected_keys


def test_failure_metrics_cli_reports_ok_status_with_data(db_with_bear_failure):
    db_path, _run_id = db_with_bear_failure
    metrics = research_failure_metrics_cli(db_path)
    assert metrics["total_failures"] > 0
    assert metrics["failures_by_role"]["bear"] > 0
    assert metrics["retry_exhaustion_rate"]["status"] == "OK"


def test_failure_metrics_cli_reports_insufficient_data_when_empty(tmp_path):
    db_path = tmp_path / "empty.sqlite3"
    connect(db_path).close()
    metrics = research_failure_metrics_cli(db_path)
    assert metrics["total_failures"] == 0
    assert metrics["retry_exhaustion_rate"]["status"] == "INSUFFICIENT_DATA"
    assert metrics["average_failed_attempt_latency_ms"]["status"] == "INSUFFICIENT_DATA"
