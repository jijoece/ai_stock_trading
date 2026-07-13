"""Milestone 6.1 Step 6/19: persistence tests for `research_attempt_failures`
(`storage/research_repositories.py`'s new failure-specific save/query methods)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from trading_research.research.failure_taxonomy import new_failure
from trading_research.research.fixtures import build_fixture_snapshot
from trading_research.storage.database import connect
from trading_research.storage.research_repositories import (
    SQLiteResearchRepository,
    list_all_attempt_failures,
    list_attempt_failures,
    list_role_failures,
    list_run_failures,
    save_evidence_snapshot,
    summarize_run_failures,
)

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


@pytest.fixture()
def conn(tmp_path):
    return connect(tmp_path / "test.sqlite3")


def _snapshot(symbol="AAPL"):
    return build_fixture_snapshot(symbol, NOW, config_hash="c" * 64, git_sha="sha1", clock=lambda: NOW)


def _seed_run(conn, research_run_id="run-1"):
    snap = _snapshot()
    save_evidence_snapshot(conn, snap)
    repo = SQLiteResearchRepository(conn)
    repo.save_run_started(research_run_id, snap.snapshot_id, "scripted", "test-model", ("bear", "manager"), "scripted", "c" * 64, NOW)
    return repo


def _failure(**overrides) -> "object":
    kwargs = dict(
        research_run_id="run-1", attempt_id="run-1-bear-1", role="bear", attempt_number=1,
        stage="CLAIM_EVIDENCE_VALIDATION", code="UNKNOWN_EVIDENCE_ID", message="claim cites unknown evidence",
        model_name="test-model", prompt_version="v1", schema_version="role-report.v1", occurred_at=NOW,
    )
    kwargs.update(overrides)
    return new_failure(**kwargs)


def test_multiple_failures_per_attempt_all_persisted(conn):
    repo = _seed_run(conn)
    f1 = _failure(claim_id="claim-1", code="UNKNOWN_EVIDENCE_ID")
    f2 = _failure(claim_id="claim-2", code="NUMERIC_VALUE_MISMATCH", message="numeric mismatch on claim-2")
    repo.save_attempt_failures((f1, f2))

    persisted = list_attempt_failures(conn, "run-1-bear-1")
    assert len(persisted) == 2
    assert {p.claim_id for p in persisted} == {"claim-1", "claim-2"}


def test_duplicate_insertion_is_idempotent(conn):
    repo = _seed_run(conn)
    f1 = _failure()
    repo.save_attempt_failure(f1)
    repo.save_attempt_failure(f1)  # identical content -> identical deterministic failure_id

    persisted = list_attempt_failures(conn, "run-1-bear-1")
    assert len(persisted) == 1


def test_failed_attempt_failure_retained_after_later_success(conn):
    """A retried role's first-attempt failure must remain visible even after the second
    attempt succeeds — append-only, never overwritten (Step 6)."""
    repo = _seed_run(conn)
    attempt1_failure = _failure(attempt_id="run-1-bear-1", attempt_number=1)
    repo.save_attempt_failure(attempt1_failure)

    persisted = list_run_failures(conn, "run-1")
    assert len(persisted) == 1
    assert persisted[0].attempt_number == 1


def test_query_by_role(conn):
    repo = _seed_run(conn)
    repo.save_attempt_failures((
        _failure(role="bear", attempt_id="run-1-bear-1"),
        _failure(role="manager", attempt_id="run-1-manager-skipped", claim_id=None, message="manager skipped"),
    ))
    bear_only = list_role_failures(conn, "run-1", "bear")
    assert len(bear_only) == 1
    assert bear_only[0].role == "bear"


def test_query_by_stage(conn):
    repo = _seed_run(conn)
    repo.save_attempt_failures((
        _failure(stage="CLAIM_EVIDENCE_VALIDATION"),
        _failure(attempt_id="run-1-bear-2", stage="RETRY_EXHAUSTED", code="RETRY_EXHAUSTED", claim_id=None, message="exhausted"),
    ))
    retry_exhausted = list_run_failures(conn, "run-1", stage="RETRY_EXHAUSTED")
    assert len(retry_exhausted) == 1
    assert retry_exhausted[0].code == "RETRY_EXHAUSTED"


def test_query_by_code(conn):
    repo = _seed_run(conn)
    repo.save_attempt_failures((
        _failure(code="UNKNOWN_EVIDENCE_ID"),
        _failure(attempt_id="run-1-bear-2", code="NUMERIC_VALUE_MISMATCH", claim_id="claim-2", message="mismatch"),
    ))
    numeric_only = list_run_failures(conn, "run-1", code="NUMERIC_VALUE_MISMATCH")
    assert len(numeric_only) == 1
    assert numeric_only[0].claim_id == "claim-2"


def test_query_by_retryable(conn):
    repo = _seed_run(conn)
    repo.save_attempt_failures((
        _failure(retryable=True),
        _failure(attempt_id="run-1-bear-2", retryable=False, code="RETRY_EXHAUSTED", stage="RETRY_EXHAUSTED", claim_id=None, message="exhausted"),
    ))
    non_retryable = list_run_failures(conn, "run-1", retryable=False)
    assert len(non_retryable) == 1
    assert non_retryable[0].retryable is False


def test_query_by_attempt_number(conn):
    repo = _seed_run(conn)
    repo.save_attempt_failures((
        _failure(attempt_number=1),
        _failure(attempt_id="run-1-bear-2", attempt_number=2, message="second attempt failure"),
    ))
    attempt_2_only = list_run_failures(conn, "run-1", attempt_number=2)
    assert len(attempt_2_only) == 1
    assert attempt_2_only[0].attempt_number == 2


def test_summarize_run_failures_counts_by_stage_and_code(conn):
    repo = _seed_run(conn)
    repo.save_attempt_failures((
        _failure(code="UNKNOWN_EVIDENCE_ID"),
        _failure(attempt_id="run-1-bear-2", code="UNKNOWN_EVIDENCE_ID", claim_id="claim-2", message="another unknown id"),
        _failure(attempt_id="run-1-bear-3", code="NUMERIC_VALUE_MISMATCH", claim_id="claim-3", message="mismatch"),
    ))
    summary = summarize_run_failures(conn, "run-1")
    assert summary["total_failures"] == 3
    assert summary["counts_by_code"]["UNKNOWN_EVIDENCE_ID"] == 2
    assert summary["counts_by_code"]["NUMERIC_VALUE_MISMATCH"] == 1
    assert summary["counts_by_stage"]["CLAIM_EVIDENCE_VALIDATION"] == 3


def test_attempt_failures_are_append_only(conn):
    repo = _seed_run(conn)
    f1 = _failure()
    repo.save_attempt_failure(f1)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE research_attempt_failures SET message = 'HACKED' WHERE failure_id = ?", (f1.failure_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM research_attempt_failures WHERE failure_id = ?", (f1.failure_id,))


def test_list_all_attempt_failures_spans_multiple_runs(conn):
    repo1 = _seed_run(conn, "run-1")
    repo1.save_attempt_failure(_failure(research_run_id="run-1", attempt_id="run-1-bear-1"))
    repo2 = _seed_run(conn, "run-2")
    repo2.save_attempt_failure(_failure(research_run_id="run-2", attempt_id="run-2-bear-1"))

    all_failures = list_all_attempt_failures(conn)
    assert {f.research_run_id for f in all_failures} == {"run-1", "run-2"}
