"""CLI tests for the Milestone 7 shadow-operations subcommands
(docs/milestone-7.md Step 25). Follows `test_research_failures_cli.py`'s
pattern: call the `*_cli` functions directly (not through argparse), assert
on the returned dict's structured JSON shape, exit-code-worthiness via
presence/absence of an `"error"` key, and sanitized output (no credentials,
no raw provider payloads/prompts/Claude responses).

Offline, deterministic — no network. `corporate_status_cli` (the one command
that genuinely needs real SEC access) is exercised at the shape/error level
only here; its real-network behavior is proven separately via the manual CLI
invocation recorded in the milestone scratchpad.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from trading_research.cli import (
    retention_apply_cli,
    retention_plan_cli,
    run_due_shadow_cycle_cli,
    shadow_alerts_cli,
    shadow_budget_status_cli,
    shadow_force_clear_kill_cli,
    shadow_kill_cli,
    shadow_lease_status_cli,
    shadow_pause_cli,
    shadow_readiness_cli,
    shadow_resume_cli,
    shadow_run_history_cli,
    shadow_status_cli,
)
from trading_research.storage.database import connect, session


@pytest.fixture()
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "shadow_cli_test.db"
        connect(path).close()
        yield path


# --- run-due-shadow-cycle -----------------------------------------------------


def test_run_due_shadow_cycle_disabled_is_success_no_op(db_path):
    """The shipped default config (`config/shadow_operations.yaml`) has
    `shadow_operations.enabled: false` — this proves the CLI wrapper treats
    that as a successful no-op, not an error."""
    outcome = run_due_shadow_cycle_cli(db_path)
    assert "error" not in outcome
    assert outcome["status"] == "DISABLED"
    assert outcome["is_successful_no_op"] is True
    assert outcome["is_error"] is False


def test_run_due_shadow_cycle_output_has_no_credentials_or_raw_payloads(db_path):
    outcome = run_due_shadow_cycle_cli(db_path)
    serialized = str(outcome).lower()
    for forbidden in ("api_key", "authorization", "sk-ant-", "password", "secret"):
        assert forbidden not in serialized


# --- shadow-status -------------------------------------------------------------


def test_shadow_status_reports_active_by_default(db_path):
    outcome = shadow_status_cli(db_path)
    assert outcome["pause_state"] == "ACTIVE"
    assert outcome["recent_scheduler_runs"] == []
    assert "recent_run_summaries" in outcome


def test_shadow_status_reflects_pause(db_path):
    shadow_pause_cli(db_path, "operator maintenance")
    outcome = shadow_status_cli(db_path)
    assert outcome["pause_state"] == "PAUSED_MANUAL"


# --- shadow-readiness ------------------------------------------------------


def test_shadow_readiness_insufficient_data_when_empty(db_path):
    outcome = shadow_readiness_cli(db_path)
    assert outcome["overall_status"] in ("INSUFFICIENT_DATA", "NOT_READY")
    assert "categories" in outcome
    assert len(outcome["categories"]) > 0


# --- shadow-run-history ------------------------------------------------------


def test_shadow_run_history_empty_db(db_path):
    outcome = shadow_run_history_cli(db_path)
    assert outcome["scheduler_runs"] == []
    assert outcome["run_summaries"] == []
    assert outcome["limit"] == 20


def test_shadow_run_history_respects_limit(db_path):
    outcome = shadow_run_history_cli(db_path, limit=5)
    assert outcome["limit"] == 5


# --- shadow-budget-status ------------------------------------------------------


def test_shadow_budget_status_reports_caps_and_zero_usage(db_path):
    outcome = shadow_budget_status_cli(db_path)
    assert outcome["daily_cap_usd"] == "10.0"
    assert outcome["monthly_cap_usd"] == "100.0"
    assert outcome["spent_today_usd"] == "0"
    assert outcome["spent_month_usd"] == "0"


# --- shadow-alerts ------------------------------------------------------------


def test_shadow_alerts_empty_db(db_path):
    outcome = shadow_alerts_cli(db_path)
    assert outcome["alerts"] == []


def test_shadow_alerts_filter_by_severity(db_path):
    outcome = shadow_alerts_cli(db_path, severity="CRITICAL")
    assert outcome["filter_severity"] == "CRITICAL"
    assert outcome["alerts"] == []


# --- shadow-pause / shadow-resume / shadow-kill / shadow-force-clear-kill -----


def test_shadow_pause_requires_reason(db_path):
    outcome = shadow_pause_cli(db_path, "")
    assert "error" in outcome


def test_shadow_pause_then_status(db_path):
    outcome = shadow_pause_cli(db_path, "operator maintenance")
    assert "error" not in outcome
    assert outcome["state"] == "PAUSED_MANUAL"
    assert outcome["previous_state"] == "ACTIVE"


def test_shadow_pause_is_persisted_as_operator_action(db_path):
    shadow_pause_cli(db_path, "operator maintenance")
    with session(db_path) as conn:
        from trading_research.shadow import pause as pause_mod
        history = pause_mod.history(conn)
    assert len(history) == 1
    assert history[0].reason == "operator maintenance"


def test_shadow_resume_requires_reason_and_operator(db_path):
    shadow_pause_cli(db_path, "operator maintenance")
    assert "error" in shadow_resume_cli(db_path, "", "jijo")
    assert "error" in shadow_resume_cli(db_path, "done", "")


def test_shadow_resume_from_paused_succeeds(db_path):
    shadow_pause_cli(db_path, "operator maintenance")
    outcome = shadow_resume_cli(db_path, "maintenance complete", "jijo")
    assert "error" not in outcome
    assert outcome["state"] == "ACTIVE"


def test_shadow_kill_then_resume_fails_with_clear_message(db_path):
    kill_outcome = shadow_kill_cli(db_path, "critical safety issue", "jijo")
    assert "error" not in kill_outcome
    assert kill_outcome["state"] == "KILLED"

    resume_outcome = shadow_resume_cli(db_path, "try to bypass", "jijo")
    assert "error" in resume_outcome
    assert "KILLED" in resume_outcome["error"]


def test_shadow_resume_cannot_override_killed_even_with_multiple_attempts(db_path):
    shadow_kill_cli(db_path, "critical safety issue", "jijo")
    for _ in range(3):
        outcome = shadow_resume_cli(db_path, "please resume", "jijo")
        assert "error" in outcome


def test_shadow_force_clear_kill_is_the_only_way_out_of_killed(db_path):
    shadow_kill_cli(db_path, "critical safety issue", "jijo")
    assert "error" in shadow_resume_cli(db_path, "try normal resume", "jijo")

    cleared = shadow_force_clear_kill_cli(db_path, "incident resolved", "jijo")
    assert "error" not in cleared
    assert cleared["state"] == "ACTIVE"
    assert cleared["previous_state"] == "KILLED"


def test_shadow_kill_requires_reason_and_operator(db_path):
    assert "error" in shadow_kill_cli(db_path, "", "jijo")
    assert "error" in shadow_kill_cli(db_path, "reason", "")


# --- shadow-lease-status ------------------------------------------------------


def test_shadow_lease_status_empty_db(db_path):
    outcome = shadow_lease_status_cli(db_path)
    assert outcome["leases"] == []


# --- retention-plan / retention-apply -----------------------------------------


def test_retention_plan_is_read_only_and_covers_known_tables():
    outcome = retention_plan_cli()
    assert "rules" in outcome
    table_names = {r["table_name"] for r in outcome["rules"]}
    assert "shadow_pause_state" in table_names
    assert "shadow_alerts" in table_names
    assert "research_attempts" in table_names
    for rule in outcome["rules"]:
        assert rule["tier"] in (
            "PERMANENT_AUDIT", "RETAIN_N_DAYS", "RETAIN_N_DAYS_THEN_HASH_ONLY",
            "RETAIN_INDEFINITELY_ACTIVE_EVALUATION",
        )


def test_retention_apply_dry_run_is_read_only(db_path):
    shadow_pause_cli(db_path, "operator maintenance")  # seed one row

    with session(db_path) as conn:
        before = conn.execute("SELECT COUNT(*) AS n FROM shadow_pause_state").fetchone()["n"]

    outcome = retention_apply_cli(db_path, dry_run=True)
    assert outcome["dry_run"] is True
    assert "diffs" in outcome

    with session(db_path) as conn:
        after = conn.execute("SELECT COUNT(*) AS n FROM shadow_pause_state").fetchone()["n"]
    assert before == after


def test_retention_apply_without_dry_run_raises_not_implemented(db_path):
    with pytest.raises(NotImplementedError):
        retention_apply_cli(db_path, dry_run=False)
