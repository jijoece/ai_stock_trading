"""Persistence for `CorporateStatusEvidence` and `EvidenceCompletenessResult`
(Milestone 7), operating on `corporate_status_schema.py`'s tables. Mirrors
`research/evidence.py`'s `snapshot_to_row`/`snapshot_from_row` convention:
sub-structures round-trip through JSON, scalar columns exist for querying.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from ..evidence_providers.corporate_status import (
    CorporateRiskSignal,
    CorporateStatusEvidence,
    FilingReference,
    SourceRecord,
)
from ..research.evidence_completeness import EvidenceCompletenessResult, POLICY_VERSION


# --- CorporateStatusEvidence (de)serialization --------------------------

def _filing_ref_to_dict(ref: FilingReference | None) -> dict | None:
    if ref is None:
        return None
    return {
        "accession_number": ref.accession_number,
        "form_type": ref.form_type,
        "filing_date": ref.filing_date.isoformat(),
        "accepted_at": ref.accepted_at.isoformat(),
        "source_url": ref.source_url,
        "is_amendment": ref.is_amendment,
    }


def _filing_ref_from_dict(data: dict | None) -> FilingReference | None:
    if data is None:
        return None
    return FilingReference(
        accession_number=data["accession_number"], form_type=data["form_type"],
        filing_date=date.fromisoformat(data["filing_date"]),
        accepted_at=datetime.fromisoformat(data["accepted_at"]),
        source_url=data["source_url"], is_amendment=data["is_amendment"],
    )


def _risk_signal_to_dict(signal: CorporateRiskSignal) -> dict:
    return {
        "signal_type": signal.signal_type,
        "status": signal.status,
        "basis": signal.basis,
        "evidence_refs": [_filing_ref_to_dict(r) for r in signal.evidence_refs],
    }


def _risk_signal_from_dict(data: dict) -> CorporateRiskSignal:
    return CorporateRiskSignal(
        signal_type=data["signal_type"], status=data["status"], basis=data["basis"],
        evidence_refs=tuple(_filing_ref_from_dict(r) for r in data["evidence_refs"]),
    )


def _source_to_dict(source: SourceRecord) -> dict:
    return {
        "source_id": source.source_id,
        "source_type": source.source_type,
        "provider": source.provider,
        "source_locator": source.source_locator,
        "retrieved_at": source.retrieved_at.isoformat(),
        "available_at": source.available_at.isoformat() if source.available_at else None,
        "content_hash": source.content_hash,
        "status": source.status,
    }


def _source_from_dict(data: dict) -> SourceRecord:
    return SourceRecord(
        source_id=data["source_id"], source_type=data["source_type"], provider=data["provider"],
        source_locator=data["source_locator"], retrieved_at=datetime.fromisoformat(data["retrieved_at"]),
        available_at=datetime.fromisoformat(data["available_at"]) if data["available_at"] else None,
        content_hash=data["content_hash"], status=data["status"],
    )


def corporate_status_to_payload(evidence: CorporateStatusEvidence) -> dict:
    return {
        "symbol": evidence.symbol,
        "as_of": evidence.as_of.isoformat(),
        "reporting_status": evidence.reporting_status,
        "reporting_status_reason": evidence.reporting_status_reason,
        "earliest_reliable_filing_date": (
            evidence.earliest_reliable_filing_date.isoformat() if evidence.earliest_reliable_filing_date else None
        ),
        "operating_history_years": (
            str(evidence.operating_history_years) if evidence.operating_history_years is not None else None
        ),
        "latest_annual_filing": _filing_ref_to_dict(evidence.latest_annual_filing),
        "latest_quarterly_filing": _filing_ref_to_dict(evidence.latest_quarterly_filing),
        "late_filing_notices": [_filing_ref_to_dict(r) for r in evidence.late_filing_notices],
        "bankruptcy_signals": [_risk_signal_to_dict(s) for s in evidence.bankruptcy_signals],
        "delisting_signals": [_risk_signal_to_dict(s) for s in evidence.delisting_signals],
        "registration_status_signals": [_risk_signal_to_dict(s) for s in evidence.registration_status_signals],
        "shell_company_signals": [_risk_signal_to_dict(s) for s in evidence.shell_company_signals],
        "going_concern_signals": [_risk_signal_to_dict(s) for s in evidence.going_concern_signals],
        "completeness_status": evidence.completeness_status,
        "sources": [_source_to_dict(s) for s in evidence.sources],
    }


def corporate_status_from_payload(data: dict) -> CorporateStatusEvidence:
    return CorporateStatusEvidence(
        symbol=data["symbol"], as_of=datetime.fromisoformat(data["as_of"]),
        reporting_status=data["reporting_status"], reporting_status_reason=data["reporting_status_reason"],
        earliest_reliable_filing_date=(
            date.fromisoformat(data["earliest_reliable_filing_date"])
            if data["earliest_reliable_filing_date"] else None
        ),
        operating_history_years=(
            Decimal(data["operating_history_years"]) if data["operating_history_years"] is not None else None
        ),
        latest_annual_filing=_filing_ref_from_dict(data["latest_annual_filing"]),
        latest_quarterly_filing=_filing_ref_from_dict(data["latest_quarterly_filing"]),
        late_filing_notices=tuple(_filing_ref_from_dict(r) for r in data["late_filing_notices"]),
        bankruptcy_signals=tuple(_risk_signal_from_dict(s) for s in data["bankruptcy_signals"]),
        delisting_signals=tuple(_risk_signal_from_dict(s) for s in data["delisting_signals"]),
        registration_status_signals=tuple(_risk_signal_from_dict(s) for s in data["registration_status_signals"]),
        shell_company_signals=tuple(_risk_signal_from_dict(s) for s in data["shell_company_signals"]),
        going_concern_signals=tuple(_risk_signal_from_dict(s) for s in data["going_concern_signals"]),
        completeness_status=data["completeness_status"],
        sources=tuple(_source_from_dict(s) for s in data["sources"]),
    )


def save_corporate_status_evidence(
    conn: sqlite3.Connection, evidence: CorporateStatusEvidence, *, created_at: datetime | None = None,
) -> str:
    corporate_status_id = str(uuid.uuid4())
    created_at = created_at or datetime.now(timezone.utc)
    payload = corporate_status_to_payload(evidence)
    conn.execute(
        "INSERT INTO corporate_status_evidence "
        "(corporate_status_id, symbol, as_of, reporting_status, reporting_status_reason, "
        "earliest_reliable_filing_date, operating_history_years, completeness_status, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            corporate_status_id, evidence.symbol, evidence.as_of.isoformat(), evidence.reporting_status,
            evidence.reporting_status_reason,
            evidence.earliest_reliable_filing_date.isoformat() if evidence.earliest_reliable_filing_date else None,
            str(evidence.operating_history_years) if evidence.operating_history_years is not None else None,
            evidence.completeness_status, json.dumps(payload), created_at.isoformat(),
        ),
    )
    conn.commit()
    return corporate_status_id


def load_corporate_status_evidence(conn: sqlite3.Connection, corporate_status_id: str) -> CorporateStatusEvidence | None:
    row = conn.execute(
        "SELECT payload_json FROM corporate_status_evidence WHERE corporate_status_id = ?", (corporate_status_id,),
    ).fetchone()
    if row is None:
        return None
    return corporate_status_from_payload(json.loads(row["payload_json"]))


def list_corporate_status_evidence(conn: sqlite3.Connection, *, symbol: str | None = None) -> list[dict]:
    if symbol:
        rows = conn.execute(
            "SELECT * FROM corporate_status_evidence WHERE symbol = ? ORDER BY created_at", (symbol.upper(),),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM corporate_status_evidence ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


# --- EvidenceCompletenessResult (de)serialization ------------------------

def save_evidence_completeness_result(
    conn: sqlite3.Connection, result: EvidenceCompletenessResult, *, created_at: datetime | None = None,
) -> str:
    evidence_completeness_id = str(uuid.uuid4())
    created_at = created_at or datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO evidence_completeness_results "
        "(evidence_completeness_id, symbol, screening_completeness, research_completeness, "
        "blocking_categories_json, policy_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            evidence_completeness_id, result.symbol, result.screening_completeness, result.research_completeness,
            json.dumps(list(result.blocking_categories)), result.policy_version, created_at.isoformat(),
        ),
    )
    conn.commit()
    return evidence_completeness_id


def load_evidence_completeness_result(
    conn: sqlite3.Connection, evidence_completeness_id: str,
) -> EvidenceCompletenessResult | None:
    row = conn.execute(
        "SELECT * FROM evidence_completeness_results WHERE evidence_completeness_id = ?",
        (evidence_completeness_id,),
    ).fetchone()
    if row is None:
        return None
    return EvidenceCompletenessResult(
        symbol=row["symbol"], screening_completeness=row["screening_completeness"],
        research_completeness=row["research_completeness"],
        blocking_categories=tuple(json.loads(row["blocking_categories_json"])),
        policy_version=row["policy_version"],
    )


def list_evidence_completeness_results(conn: sqlite3.Connection, *, symbol: str | None = None) -> list[dict]:
    if symbol:
        rows = conn.execute(
            "SELECT * FROM evidence_completeness_results WHERE symbol = ? ORDER BY created_at", (symbol.upper(),),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM evidence_completeness_results ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]
