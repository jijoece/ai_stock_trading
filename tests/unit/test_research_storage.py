"""Persistence tests for the Milestone 5 research schema/repositories
(docs/milestone-5.md Step 15: immutable snapshots, append-only attempts,
no overwrite of a completed run, queryable linkages)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading_research.research.experiment import build_experiment_assignments
from trading_research.research.fixtures import build_fixture_snapshot
from trading_research.research.models import ResearchOverlayDecision
from trading_research.storage.database import connect
from trading_research.storage.research_repositories import (
    SQLiteResearchRepository,
    list_experiment_assignments,
    load_evidence_snapshot,
    save_evidence_snapshot,
    save_experiment_assignment,
    save_overlay_decision,
)

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


@pytest.fixture()
def conn(tmp_path):
    return connect(tmp_path / "test.sqlite3")


def _snapshot(symbol="AAPL"):
    return build_fixture_snapshot(symbol, NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)


def test_snapshot_round_trip(conn):
    snap = _snapshot()
    assert save_evidence_snapshot(conn, snap) is True
    loaded = load_evidence_snapshot(conn, snap.snapshot_id)
    assert loaded == snap


def test_snapshot_save_is_idempotent(conn):
    snap = _snapshot()
    assert save_evidence_snapshot(conn, snap) is True
    assert save_evidence_snapshot(conn, snap) is False  # no-op, not a conflict


def test_snapshot_immutable_update_rejected(conn):
    snap = _snapshot()
    save_evidence_snapshot(conn, snap)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE research_evidence_snapshots SET symbol = 'HACKED' WHERE snapshot_id = ?", (snap.snapshot_id,))


def test_snapshot_immutable_delete_rejected(conn):
    snap = _snapshot()
    save_evidence_snapshot(conn, snap)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM research_evidence_snapshots WHERE snapshot_id = ?", (snap.snapshot_id,))


def test_research_attempts_append_only(conn):
    snap = _snapshot()
    save_evidence_snapshot(conn, snap)
    conn.execute(
        "INSERT INTO research_committee_runs (research_run_id, snapshot_id, provider, model_name, roles_json, "
        "run_mode, status, config_hash, created_at, completed_at) VALUES "
        "('run-1', ?, 'scripted', 'm', '[]', 'scripted', 'RUNNING', 'c', ?, NULL)", (snap.snapshot_id, NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO research_attempts (attempt_id, research_run_id, role, attempt_number, prompt_name, "
        "prompt_version, prompt_hash, system_prompt_hash, schema_version, provider, model_name, success, "
        "failure_reason, raw_response_json, validated_payload_json, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_tokens, latency_ms, provider_request_id, retry_count, pricing_version, "
        "estimated_cost, cost_status, created_at) VALUES "
        "('att-1', 'run-1', 'fundamental', 1, 'p', 'v1', 'h', 'sh', 'sv', 'scripted', 'm', 1, NULL, NULL, NULL, "
        "NULL, NULL, NULL, NULL, NULL, NULL, 0, NULL, NULL, 'NOT_APPLICABLE', ?)", (NOW.isoformat(),),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE research_attempts SET success = 0 WHERE attempt_id = 'att-1'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM research_attempts WHERE attempt_id = 'att-1'")


def test_research_committee_run_status_can_transition(conn):
    repo = SQLiteResearchRepository(conn)
    snap = _snapshot()
    save_evidence_snapshot(conn, snap)
    repo.save_run_started("run-2", snap.snapshot_id, "scripted", "m", ("fundamental",), "scripted", "c", NOW)
    assert repo.get_run_status("run-2") == "RUNNING"
    repo.mark_run_finished("run-2", "COMPLETED", NOW)
    assert repo.get_run_status("run-2") == "COMPLETED"


def test_overlay_decision_persisted_and_idempotent(conn):
    overlay = ResearchOverlayDecision(
        overlay_id="overlay-1", research_decision_id="d1", baseline_score=Decimal("80"),
        action="ALLOW_BASELINE", reasons=("supportive",), critical_risks=(), policy_version="test.v1",
    )
    assert save_overlay_decision(conn, overlay, NOW) is True
    assert save_overlay_decision(conn, overlay, NOW) is False


def test_experiment_assignments_queryable_by_experiment_id(conn):
    baseline, enhanced = build_experiment_assignments(
        candidate_run_id="cand-1", symbol="AAPL", as_of=NOW,
        baseline_recommendation_id="rec-a", enhanced_recommendation_id="rec-b",
    )
    save_experiment_assignment(conn, baseline, NOW)
    save_experiment_assignment(conn, enhanced, NOW)
    loaded = list_experiment_assignments(conn, baseline.experiment_id)
    assert {a.arm for a in loaded} == {"BASELINE", "ENHANCED"}


def test_recommendation_to_research_linkage_queryable(conn):
    """A research_role_reports row carries research_run_id and symbol —
    queryable without joining back through anything execution-related."""
    repo = SQLiteResearchRepository(conn)
    snap = _snapshot()
    save_evidence_snapshot(conn, snap)
    repo.save_run_started("run-3", snap.snapshot_id, "scripted", "m", ("fundamental",), "scripted", "c", NOW)

    from trading_research.research.output_validation import build_role_report

    report = build_role_report(
        {"stance": "BULLISH", "summary": "s", "claims": [], "catalysts": [], "risks": [], "uncertainties": [],
         "missing_data_reasons": []},
        report_id="run-3-fundamental-1", research_run_id="run-3", role="fundamental", symbol=snap.symbol,
        snapshot_id=snap.snapshot_id, model_name="m", prompt_version="v1",
    )
    repo.save_role_report(report, "run-3-fundamental-1", NOW)
    row = conn.execute(
        "SELECT research_run_id, symbol, snapshot_id FROM research_role_reports WHERE role = 'fundamental'"
    ).fetchone()
    assert row["research_run_id"] == "run-3"
    assert row["symbol"] == snap.symbol
    assert row["snapshot_id"] == snap.snapshot_id
