"""Unit tests for storage/corporate_status_schema.py and
storage/corporate_status_repositories.py (Milestone 7 persistence
requirement)."""
from __future__ import annotations

import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.evidence_providers.corporate_status import (
    COMPLETENESS_COMPLETE,
    REPORTING_STATUS_ACTIVE,
    STATUS_NOT_FOUND_IN_SEARCHED_SOURCES,
    CorporateRiskSignal,
    CorporateStatusEvidence,
    FilingReference,
    SourceRecord,
)
from trading_research.research.evidence_completeness import (
    POLICY_VERSION,
    STATUS_COMPLETE_FOR_RESEARCH,
    STATUS_COMPLETE_FOR_SCREENING,
    EvidenceCompletenessResult,
)
from trading_research.storage.corporate_status_repositories import (
    corporate_status_from_payload,
    corporate_status_to_payload,
    list_corporate_status_evidence,
    list_evidence_completeness_results,
    load_corporate_status_evidence,
    load_evidence_completeness_result,
    save_corporate_status_evidence,
    save_evidence_completeness_result,
)
from trading_research.storage.database import connect

AS_OF = datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "test.db")
        yield c
        c.close()


def _evidence() -> CorporateStatusEvidence:
    ref = FilingReference(
        accession_number="0000320193-26-000010", form_type="10-K", filing_date=date(2015, 2, 1),
        accepted_at=datetime(2015, 2, 1, 20, 0, tzinfo=timezone.utc),
        source_url="https://www.sec.gov/Archives/edgar/data/1/x/doc.htm",
    )
    signal = CorporateRiskSignal(
        signal_type="bankruptcy", status=STATUS_NOT_FOUND_IN_SEARCHED_SOURCES, basis="none found",
        evidence_refs=(ref,),
    )
    source = SourceRecord(
        source_id="sec-submissions-AAPL", source_type="corporate_status", provider="sec-edgar",
        source_locator=None, retrieved_at=AS_OF, available_at=AS_OF, content_hash="h" * 64, status="ok",
    )
    return CorporateStatusEvidence(
        symbol="AAPL", as_of=AS_OF, reporting_status=REPORTING_STATUS_ACTIVE, reporting_status_reason=None,
        earliest_reliable_filing_date=date(2015, 2, 1), operating_history_years=Decimal("11.4"),
        latest_annual_filing=ref, latest_quarterly_filing=None, late_filing_notices=(),
        bankruptcy_signals=(signal,), delisting_signals=(signal,), registration_status_signals=(signal,),
        shell_company_signals=(signal,), going_concern_signals=(signal,),
        completeness_status=COMPLETENESS_COMPLETE, sources=(source,),
    )


# --- schema applied idempotently via connect() -------------------------------

def test_schema_applied_and_idempotent(conn):
    tables = {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "corporate_status_evidence" in tables
    assert "evidence_completeness_results" in tables
    # Re-applying must not raise (idempotent CREATE TABLE IF NOT EXISTS).
    from trading_research.storage.corporate_status_schema import apply_corporate_status_schema
    apply_corporate_status_schema(conn)
    apply_corporate_status_schema(conn)


# --- round-trip payload (de)serialization -------------------------------------

def test_payload_round_trip_preserves_all_fields():
    evidence = _evidence()
    payload = corporate_status_to_payload(evidence)
    restored = corporate_status_from_payload(payload)
    assert restored == evidence


# --- save/load corporate status evidence --------------------------------------

def test_save_and_load_corporate_status_evidence(conn):
    evidence = _evidence()
    corporate_status_id = save_corporate_status_evidence(conn, evidence)
    loaded = load_corporate_status_evidence(conn, corporate_status_id)
    assert loaded == evidence


def test_load_missing_corporate_status_returns_none(conn):
    assert load_corporate_status_evidence(conn, "does-not-exist") is None


def test_list_corporate_status_evidence_filters_by_symbol(conn):
    from dataclasses import replace

    save_corporate_status_evidence(conn, _evidence())
    other = replace(_evidence(), symbol="MSFT")
    save_corporate_status_evidence(conn, other)

    aapl_rows = list_corporate_status_evidence(conn, symbol="AAPL")
    assert len(aapl_rows) == 1
    assert aapl_rows[0]["symbol"] == "AAPL"

    all_rows = list_corporate_status_evidence(conn)
    assert len(all_rows) == 2


# --- save/load evidence-completeness result ------------------------------------

def test_save_and_load_evidence_completeness_result(conn):
    result = EvidenceCompletenessResult(
        symbol="AAPL", screening_completeness=STATUS_COMPLETE_FOR_SCREENING,
        research_completeness=STATUS_COMPLETE_FOR_RESEARCH, blocking_categories=(),
        policy_version=POLICY_VERSION,
    )
    result_id = save_evidence_completeness_result(conn, result)
    loaded = load_evidence_completeness_result(conn, result_id)
    assert loaded == result


def test_evidence_completeness_policy_version_persisted(conn):
    result = EvidenceCompletenessResult(
        symbol="AAPL", screening_completeness=STATUS_COMPLETE_FOR_SCREENING,
        research_completeness=STATUS_COMPLETE_FOR_RESEARCH, blocking_categories=(),
        policy_version=POLICY_VERSION,
    )
    result_id = save_evidence_completeness_result(conn, result)
    rows = list_evidence_completeness_results(conn, symbol="AAPL")
    assert len(rows) == 1
    assert rows[0]["policy_version"] == POLICY_VERSION


def test_load_missing_evidence_completeness_result_returns_none(conn):
    assert load_evidence_completeness_result(conn, "does-not-exist") is None
