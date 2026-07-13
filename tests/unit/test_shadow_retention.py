"""Tests for `shadow/retention.py` (docs/milestone-7.md Step 26, ADR 0005
Decision 11). Proves: the plan is read-only; dry-run is read-only (no row
count changes before/after); calling `apply_retention` without `dry_run=True`
raises `NotImplementedError` unconditionally.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading_research.shadow import pause as pause_mod
from trading_research.shadow.retention import (
    ALL_TIERS,
    RETENTION_PLAN,
    TIER_PERMANENT_AUDIT,
    apply_retention,
    build_retention_dry_run,
    build_retention_plan,
)
from trading_research.storage.database import connect

NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


@pytest.fixture()
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "retention_test.db")
        yield c
        c.close()


def _seed_some_rows(conn):
    pause_mod.request_pause(conn, "operator maintenance", pause_mod.SOURCE_OPERATOR, clock=lambda: NOW)
    pause_mod.kill(conn, "test kill", "jijo", clock=lambda: NOW)


def _all_row_counts(conn) -> dict[str, int]:
    counts = {}
    for rule in RETENTION_PLAN:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (rule.table_name,)
        ).fetchone()
        if row is None:
            continue
        counts[rule.table_name] = conn.execute(f"SELECT COUNT(*) AS n FROM {rule.table_name}").fetchone()["n"]
    return counts


# --- Vocabulary sanity ---------------------------------------------------------


def test_every_rule_has_a_valid_tier():
    for rule in RETENTION_PLAN:
        assert rule.tier in ALL_TIERS


def test_plan_covers_named_milestone_tables():
    table_names = {r.table_name for r in RETENTION_PLAN}
    # Named categories from docs/milestone-7.md Step 26: immutable evidence,
    # raw SEC filing documents, provider request metadata, account-linked
    # normalized market data, research attempts, structured failures,
    # scheduler runs, alerts, budget reservations.
    assert "research_evidence_snapshots" in table_names  # immutable evidence
    assert "sec_filings" in table_names  # raw SEC filing documents
    assert "evidence_provider_requests" in table_names  # provider request metadata
    assert "price_bars" in table_names  # account-linked normalized market data
    assert "research_attempts" in table_names  # research attempts
    assert "research_attempt_failures" in table_names  # structured failures
    assert "shadow_scheduler_runs" in table_names  # scheduler runs
    assert "shadow_alerts" in table_names  # alerts
    assert "shadow_budget_reservations" in table_names  # budget reservations


def test_permanent_audit_tables_have_no_retention_days():
    for rule in RETENTION_PLAN:
        if rule.tier == TIER_PERMANENT_AUDIT:
            assert rule.retention_days is None


# --- retention-plan: read-only -------------------------------------------------


def test_build_retention_plan_is_read_only(conn):
    _seed_some_rows(conn)
    before = _all_row_counts(conn)
    build_retention_plan(conn, NOW)
    after = _all_row_counts(conn)
    assert before == after


def test_build_retention_plan_reports_existing_and_missing_tables(conn):
    report = build_retention_plan(conn, NOW)
    exists_flags = {e.rule.table_name: e.table_exists for e in report.entries}
    # shadow_pause_state exists (schema always applied); a Milestone-1/2
    # table like sec_filings also exists (schema always applied).
    assert exists_flags["shadow_pause_state"] is True
    assert exists_flags["sec_filings"] is True


def test_build_retention_plan_row_counts_reflect_seeded_data(conn):
    _seed_some_rows(conn)
    report = build_retention_plan(conn, NOW)
    pause_entry = next(e for e in report.entries if e.rule.table_name == "shadow_pause_state")
    assert pause_entry.current_row_count == 2  # request_pause + kill


# --- retention-apply --dry-run: read-only --------------------------------------


def test_dry_run_is_read_only_no_row_count_changes(conn):
    _seed_some_rows(conn)
    before = _all_row_counts(conn)
    build_retention_dry_run(conn, NOW)
    after = _all_row_counts(conn)
    assert before == after


def test_dry_run_via_apply_retention_is_read_only(conn):
    _seed_some_rows(conn)
    before = _all_row_counts(conn)
    apply_retention(conn, NOW, dry_run=True)
    after = _all_row_counts(conn)
    assert before == after


def test_dry_run_reports_never_eligible_for_permanent_audit_tables(conn):
    report = build_retention_dry_run(conn, NOW)
    pause_diff = next(d for d in report.diffs if d.table_name == "shadow_pause_state")
    assert "NEVER ELIGIBLE" in pause_diff.action_if_applied
    assert pause_diff.eligible_row_count is None


def test_dry_run_repeated_invocation_stable(conn):
    _seed_some_rows(conn)
    first = build_retention_dry_run(conn, NOW)
    second = build_retention_dry_run(conn, NOW)
    assert [d.current_row_count for d in first.diffs] == [d.current_row_count for d in second.diffs]


# --- retention-apply without --dry-run: raises ---------------------------------


def test_apply_without_dry_run_raises_not_implemented(conn):
    with pytest.raises(NotImplementedError):
        apply_retention(conn, NOW, dry_run=False)


def test_apply_without_dry_run_does_not_modify_any_row_before_raising(conn):
    _seed_some_rows(conn)
    before = _all_row_counts(conn)
    with pytest.raises(NotImplementedError):
        apply_retention(conn, NOW, dry_run=False)
    after = _all_row_counts(conn)
    assert before == after
