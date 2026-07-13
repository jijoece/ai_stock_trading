"""Unit tests for evidence_providers/operating_history.py —
docs/milestone-7.md Step 27 category C."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from trading_research.evidence_providers.corporate_status import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_UNAVAILABLE,
    REPORTING_STATUS_ACTIVE,
    REPORTING_STATUS_SOURCE_UNAVAILABLE,
    REPORTING_STATUS_UNKNOWN,
    CorporateStatusEvidence,
    FilingReference,
)
from trading_research.evidence_providers.operating_history import (
    DERIVATION_METHOD_EARLIEST_SEC_FILING_DATE,
    OUTCOME_DERIVED,
    OUTCOME_UNKNOWN,
    derive_operating_history,
)

AS_OF = datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc)


def _filing_ref(*, accession="acc-1", form_type="10-K", filing_date=date(2015, 2, 1)):
    return FilingReference(
        accession_number=accession, form_type=form_type, filing_date=filing_date,
        accepted_at=datetime(filing_date.year, filing_date.month, filing_date.day, 20, 0, tzinfo=timezone.utc),
        source_url="https://www.sec.gov/Archives/edgar/data/1/acc-1/doc.htm",
    )


def _evidence(**overrides) -> CorporateStatusEvidence:
    base = dict(
        symbol="AAPL", as_of=AS_OF, reporting_status=REPORTING_STATUS_ACTIVE, reporting_status_reason=None,
        earliest_reliable_filing_date=date(2015, 2, 1), operating_history_years=Decimal("11.4"),
        latest_annual_filing=_filing_ref(filing_date=date(2015, 2, 1)), latest_quarterly_filing=None,
        late_filing_notices=(), bankruptcy_signals=(), delisting_signals=(), registration_status_signals=(),
        shell_company_signals=(), going_concern_signals=(), completeness_status=COMPLETENESS_COMPLETE, sources=(),
    )
    base.update(overrides)
    return CorporateStatusEvidence(**base)


# --- public-reporting-history derivation -------------------------------------

def test_derives_operating_history_from_earliest_filing():
    evidence = _evidence()
    result = derive_operating_history(evidence)
    assert result.outcome == OUTCOME_DERIVED
    assert result.value_years == Decimal("11.4")
    assert result.derivation_method == DERIVATION_METHOD_EARLIEST_SEC_FILING_DATE


def test_source_method_retained():
    evidence = _evidence()
    result = derive_operating_history(evidence)
    assert result.earliest_known_source is not None
    assert result.earliest_known_source.filing_date == date(2015, 2, 1)
    assert result.derivation_method is not None


# --- unknown history ------------------------------------------------------

def test_unknown_history_when_no_earliest_filing_date():
    evidence = _evidence(
        earliest_reliable_filing_date=None, operating_history_years=None,
        latest_annual_filing=None, reporting_status=REPORTING_STATUS_UNKNOWN,
        completeness_status=COMPLETENESS_UNAVAILABLE,
    )
    result = derive_operating_history(evidence)
    assert result.outcome == OUTCOME_UNKNOWN
    assert result.value_years is None
    assert result.reason is not None


def test_unknown_history_when_source_unavailable():
    evidence = _evidence(
        reporting_status=REPORTING_STATUS_SOURCE_UNAVAILABLE, reporting_status_reason="SEC unavailable",
        earliest_reliable_filing_date=None, operating_history_years=None, latest_annual_filing=None,
        completeness_status=COMPLETENESS_UNAVAILABLE,
    )
    result = derive_operating_history(evidence)
    assert result.outcome == OUTCOME_UNKNOWN
    assert result.value_years is None


def test_never_fabricates_zero_for_unknown():
    evidence = _evidence(
        earliest_reliable_filing_date=None, operating_history_years=None,
        latest_annual_filing=None, reporting_status=REPORTING_STATUS_UNKNOWN,
        completeness_status=COMPLETENESS_UNAVAILABLE,
    )
    result = derive_operating_history(evidence)
    assert result.value_years != Decimal("0")
    assert result.value_years is None


# --- historical as-of -------------------------------------------------------

def test_historical_as_of_uses_evidence_as_of_not_wall_clock():
    historical_as_of = datetime(2020, 1, 1, tzinfo=timezone.utc)
    evidence = _evidence(
        as_of=historical_as_of, earliest_reliable_filing_date=date(2015, 2, 1),
        operating_history_years=Decimal("4.9"),
        latest_annual_filing=_filing_ref(filing_date=date(2015, 2, 1)),
    )
    result = derive_operating_history(evidence)
    assert result.as_of == historical_as_of
    assert result.value_years == Decimal("4.9")


# --- proxy not mislabeled as company age ------------------------------------

def test_module_docstring_disclaims_company_age_and_listing_history():
    import trading_research.evidence_providers.operating_history as mod

    doc = mod.__doc__.lower()
    assert "not" in doc
    assert "company age" in doc
    assert "exchange-listing history" in doc or "exchange listing history" in doc
    assert "public-reporting-history" in doc or "public reporting history" in doc


def test_integration_note_documents_screener_semantic_gap():
    import trading_research.evidence_providers.operating_history as mod

    assert "screener" in mod.INTEGRATION_NOTE.lower()
    assert mod.INTEGRATION_NOTE  # non-empty


# --- result validation -------------------------------------------------------

def test_derived_outcome_requires_non_none_value():
    import pytest
    from trading_research.evidence_providers.operating_history import OperatingHistoryResult

    with pytest.raises(ValueError):
        OperatingHistoryResult(
            symbol="AAPL", as_of=AS_OF, outcome=OUTCOME_DERIVED, value_years=None,
            derivation_method=DERIVATION_METHOD_EARLIEST_SEC_FILING_DATE, earliest_known_source=None, reason=None,
        )


def test_prefers_matching_filing_reference_as_earliest_source():
    quarterly = _filing_ref(accession="acc-q", form_type="10-Q", filing_date=date(2015, 2, 1))
    annual = _filing_ref(accession="acc-a", form_type="10-K", filing_date=date(2016, 1, 1))
    evidence = _evidence(
        earliest_reliable_filing_date=date(2015, 2, 1),
        latest_annual_filing=annual, latest_quarterly_filing=quarterly,
    )
    result = derive_operating_history(evidence)
    # The quarterly reference matches the earliest_reliable_filing_date;
    # it should be preferred as the earliest_known_source.
    assert result.earliest_known_source.accession_number == "acc-q"
