"""Unit tests for evidence_providers/corporate_status.py and
corporate_status_adapters.py — docs/milestone-7.md Step 27 category A.
No real network: every request is served by `httpx.MockTransport`, exactly
matching tests/unit/test_sec_provider.py's existing pattern."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx
import pytest

from trading_research.evidence_providers.cache import ProviderCache
from trading_research.evidence_providers.corporate_status import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_UNAVAILABLE,
    REPORTING_STATUS_ACTIVE,
    REPORTING_STATUS_INACTIVE,
    REPORTING_STATUS_SOURCE_UNAVAILABLE,
    REPORTING_STATUS_UNKNOWN,
    STATUS_CONFIRMED,
    STATUS_NOT_FOUND_IN_SEARCHED_SOURCES,
    CorporateRiskSignal,
)
from trading_research.evidence_providers.corporate_status_adapters import derive_corporate_status
from trading_research.evidence_providers.http_client import HttpJsonClient
from trading_research.evidence_providers.rate_limits import MinIntervalRateLimiter
from trading_research.evidence_providers.sec_provider import SecEdgarClient

AS_OF = datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc)


def _client(handler, *, max_attempts=2) -> SecEdgarClient:
    transport = httpx.MockTransport(handler)
    http = HttpJsonClient(
        base_headers={"User-Agent": "test-agent contact@example.com"},
        rate_limiter=MinIntervalRateLimiter(0.0, sleep_fn=lambda s: None),
        max_attempts=max_attempts, transport=transport, provider="sec-edgar",
    )
    return SecEdgarClient(http_client=http, cache=ProviderCache(clock=time.monotonic), user_agent="test-agent contact@example.com")


def _submissions_body(**overrides):
    body = {
        "cik": "320193",
        "filings": {
            "recent": {
                "accessionNumber": ["0000320193-25-000001", "0000320193-25-000010", "0000320193-26-000005"],
                "filingDate": ["2025-02-01", "2025-05-01", "2026-02-01"],
                "acceptanceDateTime": [
                    "2025-02-01T20:00:00.000Z", "2025-05-01T20:00:00.000Z", "2026-02-01T20:00:00.000Z",
                ],
                "form": ["10-K", "10-Q", "10-K"],
                "reportDate": ["2024-12-31", "2025-03-31", "2025-12-31"],
                "primaryDocument": ["aapl-10k.htm", "aapl-10q.htm", "aapl-10k2.htm"],
            }
        },
    }
    body["filings"]["recent"].update(overrides)
    return body


def _handler_for(body):
    def handler(request):
        return httpx.Response(200, json=body)
    return handler


# --- earliest filing derivation -----------------------------------------

def test_earliest_filing_derivation():
    client = _client(_handler_for(_submissions_body()), max_attempts=2)
    result = derive_corporate_status("AAPL", sec_client=client, as_of=AS_OF, cik="0000320193")
    assert result.earliest_reliable_filing_date is not None
    assert result.earliest_reliable_filing_date.isoformat() == "2025-02-01"
    assert result.operating_history_years is not None
    assert result.operating_history_years > 0


# --- annual/quarterly filing presence -------------------------------------

def test_annual_and_quarterly_filing_presence():
    client = _client(_handler_for(_submissions_body()), max_attempts=2)
    result = derive_corporate_status("AAPL", sec_client=client, as_of=AS_OF, cik="0000320193")
    assert result.latest_annual_filing is not None
    assert result.latest_annual_filing.form_type == "10-K"
    assert result.latest_quarterly_filing is not None
    assert result.latest_quarterly_filing.form_type == "10-Q"
    assert result.completeness_status == COMPLETENESS_COMPLETE


# --- late-filing notice ---------------------------------------------------

def test_late_filing_notice_captured():
    body = _submissions_body(
        accessionNumber=["0000320193-25-000001", "0000320193-25-000099"],
        filingDate=["2025-02-01", "2025-05-01"],
        acceptanceDateTime=["2025-02-01T20:00:00.000Z", "2025-05-01T20:00:00.000Z"],
        form=["10-K", "NT 10-Q"],
        reportDate=["2024-12-31", "2025-03-31"],
        primaryDocument=["aapl-10k.htm", "aapl-nt10q.htm"],
    )
    client = _client(_handler_for(body), max_attempts=2)
    result = derive_corporate_status("AAPL", sec_client=client, as_of=AS_OF, cik="0000320193")
    assert len(result.late_filing_notices) == 1
    assert result.late_filing_notices[0].form_type == "NT 10-Q"


# --- bankruptcy signal (metadata-only layer never confirms) --------------

def test_bankruptcy_signal_is_not_found_at_metadata_layer_even_with_8k():
    body = _submissions_body(
        accessionNumber=["0000320193-25-000001", "0000320193-25-000077"],
        filingDate=["2025-02-01", "2025-06-01"],
        acceptanceDateTime=["2025-02-01T20:00:00.000Z", "2025-06-01T20:00:00.000Z"],
        form=["10-K", "8-K"],
        reportDate=["2024-12-31", None],
        primaryDocument=["aapl-10k.htm", "aapl-8k.htm"],
    )
    client = _client(_handler_for(body), max_attempts=2)
    result = derive_corporate_status("AAPL", sec_client=client, as_of=AS_OF, cik="0000320193")
    assert len(result.bankruptcy_signals) == 1
    signal = result.bankruptcy_signals[0]
    # Metadata alone can never CONFIRM a bankruptcy disclosure.
    assert signal.status == STATUS_NOT_FOUND_IN_SEARCHED_SOURCES
    assert signal.status != "FALSE"


# --- delisting signal ------------------------------------------------------

def test_delisting_signal_confirmed_from_form_25():
    body = _submissions_body(
        accessionNumber=["0000320193-25-000001", "0000320193-25-000088"],
        filingDate=["2025-02-01", "2025-07-01"],
        acceptanceDateTime=["2025-02-01T20:00:00.000Z", "2025-07-01T20:00:00.000Z"],
        form=["10-K", "25"],
        reportDate=["2024-12-31", None],
        primaryDocument=["aapl-10k.htm", "aapl-25.htm"],
    )
    client = _client(_handler_for(body), max_attempts=2)
    result = derive_corporate_status("AAPL", sec_client=client, as_of=AS_OF, cik="0000320193")
    assert len(result.delisting_signals) == 1
    assert result.delisting_signals[0].status == STATUS_CONFIRMED


def test_delisting_signal_not_found_when_absent():
    client = _client(_handler_for(_submissions_body()), max_attempts=2)
    result = derive_corporate_status("AAPL", sec_client=client, as_of=AS_OF, cik="0000320193")
    assert result.delisting_signals[0].status == STATUS_NOT_FOUND_IN_SEARCHED_SOURCES


# --- registration-termination signal ---------------------------------------

def test_registration_termination_signal_confirmed_from_form_15():
    body = _submissions_body(
        accessionNumber=["0000320193-25-000001", "0000320193-25-000099"],
        filingDate=["2025-02-01", "2025-08-01"],
        acceptanceDateTime=["2025-02-01T20:00:00.000Z", "2025-08-01T20:00:00.000Z"],
        form=["10-K", "15-12B"],
        reportDate=["2024-12-31", None],
        primaryDocument=["aapl-10k.htm", "aapl-15.htm"],
    )
    client = _client(_handler_for(body), max_attempts=2)
    result = derive_corporate_status("AAPL", sec_client=client, as_of=AS_OF, cik="0000320193")
    assert result.registration_status_signals[0].status == STATUS_CONFIRMED


# --- shell disclosure (metadata layer never confirms) ----------------------

def test_shell_company_signal_always_not_found_at_metadata_layer():
    client = _client(_handler_for(_submissions_body()), max_attempts=2)
    result = derive_corporate_status("AAPL", sec_client=client, as_of=AS_OF, cik="0000320193")
    assert result.shell_company_signals[0].status == STATUS_NOT_FOUND_IN_SEARCHED_SOURCES


# --- going-concern disclosure at metadata layer -----------------------------

def test_going_concern_signal_not_found_at_metadata_layer():
    client = _client(_handler_for(_submissions_body()), max_attempts=2)
    result = derive_corporate_status("AAPL", sec_client=client, as_of=AS_OF, cik="0000320193")
    assert result.going_concern_signals[0].status == STATUS_NOT_FOUND_IN_SEARCHED_SOURCES
    # never silently claim "no going concern issue" == confirmed absent
    assert result.going_concern_signals[0].status != "CONFIRMED_ABSENT"


# --- disclosure absent from searched sections is covered in
# test_disclosure_extraction.py (text-level, not metadata-level)

# --- document unavailable covered in test_filing_documents.py /
# test_disclosure_extraction.py

# --- future filing excluded (point-in-time) --------------------------------

def test_future_filing_excluded_point_in_time():
    body = _submissions_body(
        accessionNumber=["0000320193-25-000001", "0000320193-27-000001"],
        filingDate=["2025-02-01", "2027-01-01"],
        acceptanceDateTime=["2025-02-01T20:00:00.000Z", "2027-01-01T20:00:00.000Z"],
        form=["10-K", "10-K"],
        reportDate=["2024-12-31", "2026-12-31"],
        primaryDocument=["aapl-10k.htm", "aapl-future-10k.htm"],
    )
    client = _client(_handler_for(body), max_attempts=2)
    result = derive_corporate_status("AAPL", sec_client=client, as_of=AS_OF, cik="0000320193")
    # The 2027 filing is after AS_OF (2026-07-11) and must never appear.
    assert result.latest_annual_filing.accession_number == "0000320193-25-000001"
    all_accessions = {s.accession_number for group in (
        result.bankruptcy_signals, result.delisting_signals, result.registration_status_signals,
        result.shell_company_signals, result.going_concern_signals,
    ) for s in group for s in s.evidence_refs}
    assert "0000320193-27-000001" not in all_accessions


# --- amendment handling ------------------------------------------------------

def test_amendment_form_type_retained():
    body = _submissions_body(
        accessionNumber=["0000320193-25-000001", "0000320193-25-000002"],
        filingDate=["2025-02-01", "2025-02-15"],
        acceptanceDateTime=["2025-02-01T20:00:00.000Z", "2025-02-15T20:00:00.000Z"],
        form=["10-K", "10-K/A"],
        reportDate=["2024-12-31", "2024-12-31"],
        primaryDocument=["aapl-10k.htm", "aapl-10ka.htm"],
    )
    client = _client(_handler_for(body), max_attempts=2)
    result = derive_corporate_status("AAPL", sec_client=client, as_of=AS_OF, cik="0000320193")
    # 10-K/A is the later-accepted annual-form filing and should be
    # reflected in latest_annual_filing (form_type retained verbatim, not
    # collapsed away).
    assert result.latest_annual_filing.form_type == "10-K/A"
    assert result.latest_annual_filing.is_amendment is True


# --- duplicate filing -------------------------------------------------------

def test_duplicate_accession_collapsed_once():
    body = _submissions_body(
        accessionNumber=["0000320193-25-000001", "0000320193-25-000001", "0000320193-25-000010"],
        filingDate=["2025-02-01", "2025-02-01", "2025-05-01"],
        acceptanceDateTime=["2025-02-01T20:00:00.000Z", "2025-02-01T20:00:00.000Z", "2025-05-01T20:00:00.000Z"],
        form=["10-K", "10-K", "10-Q"],
        reportDate=["2024-12-31", "2024-12-31", "2025-03-31"],
        primaryDocument=["aapl-10k.htm", "aapl-10k.htm", "aapl-10q.htm"],
    )
    client = _client(_handler_for(body), max_attempts=2)
    result = derive_corporate_status("AAPL", sec_client=client, as_of=AS_OF, cik="0000320193")
    # Duplicate accession collapses to one filing for gap/earliest-date math.
    assert result.earliest_reliable_filing_date.isoformat() == "2025-02-01"


# --- point-in-time filtering (reporting status derived only from
# already-filtered filings) --------------------------------------------------

def test_reporting_status_active_when_recent_filing_present():
    client = _client(_handler_for(_submissions_body()), max_attempts=2)
    result = derive_corporate_status("AAPL", sec_client=client, as_of=AS_OF, cik="0000320193")
    assert result.reporting_status == REPORTING_STATUS_ACTIVE


def test_reporting_status_inactive_when_large_gap():
    body = _submissions_body(
        accessionNumber=["0000320193-20-000001"],
        filingDate=["2020-02-01"],
        acceptanceDateTime=["2020-02-01T20:00:00.000Z"],
        form=["10-K"],
        reportDate=["2019-12-31"],
        primaryDocument=["aapl-10k.htm"],
    )
    client = _client(_handler_for(body), max_attempts=2)
    result = derive_corporate_status("AAPL", sec_client=client, as_of=AS_OF, cik="0000320193")
    assert result.reporting_status == REPORTING_STATUS_INACTIVE
    assert result.reporting_status_reason is not None
    # Must not editorialize into "the company is defunct".
    assert "defunct" not in result.reporting_status_reason.lower()


# --- no false healthy default ------------------------------------------------

def test_no_filings_produces_unknown_not_healthy_default():
    client = _client(_handler_for({"cik": "1", "filings": {"recent": {
        "accessionNumber": [], "filingDate": [], "acceptanceDateTime": [], "form": [],
        "reportDate": [], "primaryDocument": [],
    }}}), max_attempts=2)
    result = derive_corporate_status("NOPE", sec_client=client, as_of=AS_OF, cik="0000000001")
    assert result.reporting_status == REPORTING_STATUS_UNKNOWN
    assert result.completeness_status == COMPLETENESS_UNAVAILABLE
    for group in (
        result.bankruptcy_signals, result.delisting_signals, result.registration_status_signals,
        result.shell_company_signals, result.going_concern_signals,
    ):
        assert group[0].status == STATUS_NOT_FOUND_IN_SEARCHED_SOURCES
        assert group[0].status != "CONFIRMED"  # never a fabricated healthy-company default


def test_provider_unavailable_source_status_on_error():
    def handler(request):
        return httpx.Response(503, text="unavailable")

    client = _client(handler, max_attempts=1)
    result = derive_corporate_status("AAPL", sec_client=client, as_of=AS_OF, cik="0000320193")
    assert result.reporting_status == REPORTING_STATUS_SOURCE_UNAVAILABLE
    assert result.completeness_status == COMPLETENESS_UNAVAILABLE


def test_corporate_status_evidence_has_any_critical_uncertainty_for_unknown():
    client = _client(_handler_for({"cik": "1", "filings": {"recent": {
        "accessionNumber": [], "filingDate": [], "acceptanceDateTime": [], "form": [],
        "reportDate": [], "primaryDocument": [],
    }}}), max_attempts=2)
    result = derive_corporate_status("NOPE", sec_client=client, as_of=AS_OF, cik="0000000001")
    assert result.has_any_critical_uncertainty() is True


def test_corporate_status_evidence_no_critical_uncertainty_for_active_clean():
    client = _client(_handler_for(_submissions_body()), max_attempts=2)
    result = derive_corporate_status("AAPL", sec_client=client, as_of=AS_OF, cik="0000320193")
    # All risk signals are NOT_FOUND_IN_SEARCHED_SOURCES, not blocking states.
    assert result.has_any_critical_uncertainty() is False


def test_corporate_risk_signal_rejects_invalid_status():
    with pytest.raises(ValueError):
        CorporateRiskSignal(signal_type="bankruptcy", status="FALSE", basis="x")
