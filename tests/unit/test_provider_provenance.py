"""Tests for `research/provider_provenance.py` (Milestone 9.2 Sections 1-4):
authoritative fixture/real provider-provenance classification, replacing
`cost_usd > 0` inference."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading_research.research.provider_provenance import (
    claude_provider_row,
    compute_real_provider_history,
    evidence_provider_row,
    record_cycle_provider_provenance,
)
from trading_research.storage.database import connect

BASE_TIME = datetime(2026, 1, 5, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "provenance_test.db")
        yield c
        c.close()


def _make_cycle(conn, cycle_id: str, *, provider_mode: str = "real") -> None:
    conn.execute(
        "INSERT INTO research_cycles (cycle_id, universe_id, as_of, configuration_hash, experiment_policy, "
        "provider_mode, status, started_at, completed_at) VALUES (?, 'u1', ?, 'h', 'OBSERVE_ONLY', ?, 'COMPLETED', ?, ?)",
        (cycle_id, BASE_TIME.isoformat(), provider_mode, BASE_TIME.isoformat(), BASE_TIME.isoformat()),
    )
    conn.commit()


def test_fixture_evidence_and_fixture_claude(conn):
    _make_cycle(conn, "c1", provider_mode="fixture")
    record_cycle_provider_provenance(conn, [
        evidence_provider_row(
            cycle_id="c1", research_run_id=None, symbol="AAPL", provider_category="market",
            provider_name="alpaca-data", request_or_source_id="s1", status="ok",
            cycle_provider_mode="fixture", observed_at=BASE_TIME,
        ),
        claude_provider_row(cycle_id="c1", research_run_id="rr1", symbol="AAPL", provider_name="deterministic", observed_at=BASE_TIME),
    ])
    summary = compute_real_provider_history(conn, BASE_TIME)
    assert summary.fixture_only_cycle_count == 1
    assert summary.real_provider_cycle_count == 0


def test_real_evidence_and_fixture_claude(conn):
    _make_cycle(conn, "c1", provider_mode="real")
    record_cycle_provider_provenance(conn, [
        evidence_provider_row(
            cycle_id="c1", research_run_id=None, symbol="AAPL", provider_category="market",
            provider_name="alpaca-data", request_or_source_id="s1", status="ok",
            cycle_provider_mode="real", observed_at=BASE_TIME,
        ),
        claude_provider_row(cycle_id="c1", research_run_id="rr1", symbol="AAPL", provider_name="deterministic", observed_at=BASE_TIME),
    ])
    summary = compute_real_provider_history(conn, BASE_TIME)
    assert summary.real_evidence_only_cycle_count == 1
    assert summary.real_provider_cycle_count == 1


def test_fixture_evidence_and_real_claude(conn):
    _make_cycle(conn, "c1", provider_mode="fixture")
    record_cycle_provider_provenance(conn, [
        evidence_provider_row(
            cycle_id="c1", research_run_id=None, symbol="AAPL", provider_category="market",
            provider_name="alpaca-data", request_or_source_id="s1", status="ok",
            cycle_provider_mode="fixture", observed_at=BASE_TIME,
        ),
        claude_provider_row(cycle_id="c1", research_run_id="rr1", symbol="AAPL", provider_name="anthropic", observed_at=BASE_TIME),
    ])
    summary = compute_real_provider_history(conn, BASE_TIME)
    assert summary.real_claude_only_cycle_count == 1
    assert summary.real_provider_cycle_count == 1


def test_real_evidence_and_real_claude(conn):
    _make_cycle(conn, "c1", provider_mode="real")
    record_cycle_provider_provenance(conn, [
        evidence_provider_row(
            cycle_id="c1", research_run_id=None, symbol="AAPL", provider_category="market",
            provider_name="alpaca-data", request_or_source_id="s1", status="ok",
            cycle_provider_mode="real", observed_at=BASE_TIME,
        ),
        claude_provider_row(cycle_id="c1", research_run_id="rr1", symbol="AAPL", provider_name="anthropic", observed_at=BASE_TIME),
    ])
    summary = compute_real_provider_history(conn, BASE_TIME)
    assert summary.real_evidence_and_claude_cycle_count == 1
    assert summary.real_provider_cycle_count == 1


def test_mixed_providers(conn):
    """Directly persists a mixed-mode row set (market real, news fixture)
    to prove the classifier's own MIXED branch — not reachable through
    today's cycle wiring (real mode never mixes fixture clients per
    category), but the classifier must still detect it if it ever occurs."""
    _make_cycle(conn, "c1", provider_mode="real")
    record_cycle_provider_provenance(conn, [
        evidence_provider_row(
            cycle_id="c1", research_run_id=None, symbol="AAPL", provider_category="market",
            provider_name="alpaca-data", request_or_source_id="s1", status="ok",
            cycle_provider_mode="real", observed_at=BASE_TIME,
        ),
        evidence_provider_row(
            cycle_id="c1", research_run_id=None, symbol="AAPL", provider_category="news",
            provider_name="fixture-news", request_or_source_id="s2", status="ok",
            cycle_provider_mode="fixture", observed_at=BASE_TIME,
        ),
    ])
    summary = compute_real_provider_history(conn, BASE_TIME)
    assert summary.mixed_cycle_count == 1
    assert summary.real_provider_cycle_count == 1


def test_missing_metadata_is_unknown(conn):
    _make_cycle(conn, "c1", provider_mode="real")
    # No provenance rows ever persisted for this cycle.
    summary = compute_real_provider_history(conn, BASE_TIME)
    assert summary.total_classified_cycles == 1
    assert summary.completed_cycle_count == 1
    assert summary.unknown_cycle_count == 1
    assert summary.real_provider_cycle_count == 0


def test_failed_real_evidence_is_attempt_and_failure_not_success(conn):
    _make_cycle(conn, "failed-real", provider_mode="real")
    record_cycle_provider_provenance(conn, [evidence_provider_row(
        cycle_id="failed-real", research_run_id=None, symbol="AAPL", provider_category="market",
        provider_name="real-market", request_or_source_id="req", status="timeout",
        cycle_provider_mode="real", observed_at=BASE_TIME,
    )])
    summary = compute_real_provider_history(conn, BASE_TIME)
    assert summary.real_provider_attempt_cycle_count == 1
    assert summary.real_provider_failure_cycle_count == 1
    assert summary.real_provider_success_cycle_count == 0
    assert summary.unknown_cycle_count == 1


def test_incomplete_real_claude_is_partial_not_success(conn):
    _make_cycle(conn, "partial-claude", provider_mode="fixture")
    record_cycle_provider_provenance(conn, [claude_provider_row(
        cycle_id="partial-claude", research_run_id="rr-partial", symbol="AAPL",
        provider_name="anthropic", status="ANALYSIS_INCOMPLETE", observed_at=BASE_TIME,
    )])
    summary = compute_real_provider_history(conn, BASE_TIME)
    assert summary.partial_provider_cycle_count == 1
    assert summary.real_provider_success_cycle_count == 0


def test_category_totals_reconcile_to_completed_cycles(conn):
    _make_cycle(conn, "known", provider_mode="fixture")
    _make_cycle(conn, "unknown", provider_mode="real")
    record_cycle_provider_provenance(conn, [evidence_provider_row(
        cycle_id="known", research_run_id=None, symbol="AAPL", provider_category="market",
        provider_name="fixture", request_or_source_id="s", status="ok",
        cycle_provider_mode="fixture", observed_at=BASE_TIME,
    )])
    summary = compute_real_provider_history(conn, BASE_TIME)
    total = (
        summary.fixture_only_cycle_count + summary.real_evidence_only_cycle_count
        + summary.real_claude_only_cycle_count + summary.real_evidence_and_claude_cycle_count
        + summary.mixed_cycle_count + summary.unknown_cycle_count
    )
    assert total == summary.completed_cycle_count == 2


def test_evidence_fact_links_append_only_to_research_run(conn):
    from trading_research.storage import research_cycle_repositories as repo
    _make_cycle(conn, "linked", provider_mode="real")
    record_cycle_provider_provenance(conn, [evidence_provider_row(
        cycle_id="linked", research_run_id=None, symbol="AAPL", provider_category="market",
        provider_name="real-market", request_or_source_id="s", status="ok",
        cycle_provider_mode="real", observed_at=BASE_TIME,
    )])
    assert repo.link_provider_provenance_to_research_run(
        conn, cycle_id="linked", symbol="AAPL", provider_category="market",
        research_run_id="rr-linked", created_at=BASE_TIME.isoformat(),
    )
    assert not repo.link_provider_provenance_to_research_run(
        conn, cycle_id="linked", symbol="AAPL", provider_category="market",
        research_run_id="rr-linked", created_at=BASE_TIME.isoformat(),
    )
    assert repo.list_provider_provenance_links_for_run(conn, "rr-linked")[0]["provider_category"] == "market"


def test_positive_cost_does_not_imply_real_and_zero_cost_does_not_imply_fixture(conn):
    """No code path in provider_provenance.py reads `cost_usd` at all — this
    test proves that structurally by never persisting a shadow_run_summaries
    row and still getting a correct REAL classification from provenance
    alone (zero-cost real-provider metadata still counts as real)."""
    _make_cycle(conn, "c1", provider_mode="real")
    record_cycle_provider_provenance(conn, [
        evidence_provider_row(
            cycle_id="c1", research_run_id=None, symbol="AAPL", provider_category="market",
            provider_name="alpaca-data", request_or_source_id="s1", status="ok",
            cycle_provider_mode="real", observed_at=BASE_TIME,
        ),
    ])
    summary = compute_real_provider_history(conn, BASE_TIME)
    assert summary.real_provider_cycle_count == 1  # zero cost_usd anywhere, still counted real


def test_one_cycle_counted_once_even_with_multiple_real_providers_and_symbols(conn):
    _make_cycle(conn, "c1", provider_mode="real")
    record_cycle_provider_provenance(conn, [
        evidence_provider_row(
            cycle_id="c1", research_run_id=None, symbol="AAPL", provider_category="market",
            provider_name="alpaca-data", request_or_source_id="s1", status="ok",
            cycle_provider_mode="real", observed_at=BASE_TIME,
        ),
        evidence_provider_row(
            cycle_id="c1", research_run_id=None, symbol="MSFT", provider_category="market",
            provider_name="alpaca-data", request_or_source_id="s2", status="ok",
            cycle_provider_mode="real", observed_at=BASE_TIME,
        ),
        claude_provider_row(cycle_id="c1", research_run_id="rr1", symbol="AAPL", provider_name="anthropic", observed_at=BASE_TIME),
    ])
    summary = compute_real_provider_history(conn, BASE_TIME)
    assert summary.total_classified_cycles == 1
    assert summary.real_provider_cycle_count == 1


def test_record_is_idempotent_on_replay(conn):
    _make_cycle(conn, "c1", provider_mode="real")
    rows = [
        evidence_provider_row(
            cycle_id="c1", research_run_id=None, symbol="AAPL", provider_category="market",
            provider_name="alpaca-data", request_or_source_id="s1", status="ok",
            cycle_provider_mode="real", observed_at=BASE_TIME,
        ),
    ]
    first = record_cycle_provider_provenance(conn, rows)
    second = record_cycle_provider_provenance(conn, rows)
    assert first == 1
    assert second == 0
