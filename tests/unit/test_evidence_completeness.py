"""Unit tests for research/evidence_completeness.py — docs/milestone-7.md
Step 27 category F."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from trading_research.evidence_providers.corporate_status import (
    COMPLETENESS_COMPLETE,
    REPORTING_STATUS_ACTIVE,
    REPORTING_STATUS_UNKNOWN,
    STATUS_CONFLICTING,
    STATUS_NOT_FOUND_IN_SEARCHED_SOURCES,
    STATUS_UNKNOWN,
    CorporateRiskSignal,
    CorporateStatusEvidence,
)
from trading_research.evidence_providers.normalization import (
    OUTCOME_COMPLETE,
    OUTCOME_COMPLETE_WITH_CONFLICTS,
    OUTCOME_INCOMPLETE_REQUIRED_DATA,
    OUTCOME_POINT_IN_TIME_UNSAFE,
    OUTCOME_PROVIDER_UNAVAILABLE,
)
from trading_research.research.evidence_completeness import (
    ALL_STATUS_VALUES,
    POLICY_VERSION,
    STATUS_COMPLETE_FOR_RESEARCH,
    STATUS_COMPLETE_FOR_SCREENING,
    STATUS_CONFLICTING_CRITICAL_DATA,
    STATUS_MISSING_CRITICAL_CORPORATE_STATUS,
    STATUS_MISSING_CRITICAL_FUNDAMENTALS,
    STATUS_MISSING_CRITICAL_MARKET_DATA,
    STATUS_MISSING_NEWS,
    STATUS_MISSING_SENTIMENT,
    STATUS_PARTIAL_NONCRITICAL,
    STATUS_POINT_IN_TIME_UNSAFE,
    STATUS_PROVIDER_UNAVAILABLE,
    evaluate_completeness,
)

AS_OF = datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc)


def _clean_corporate_status() -> CorporateStatusEvidence:
    not_found = CorporateRiskSignal(signal_type="x", status=STATUS_NOT_FOUND_IN_SEARCHED_SOURCES, basis="b")
    return CorporateStatusEvidence(
        symbol="AAPL", as_of=AS_OF, reporting_status=REPORTING_STATUS_ACTIVE, reporting_status_reason=None,
        earliest_reliable_filing_date=date(2015, 1, 1), operating_history_years=Decimal("11.5"),
        latest_annual_filing=None, latest_quarterly_filing=None, late_filing_notices=(),
        bankruptcy_signals=(not_found,), delisting_signals=(not_found,), registration_status_signals=(not_found,),
        shell_company_signals=(not_found,), going_concern_signals=(not_found,),
        completeness_status=COMPLETENESS_COMPLETE, sources=(),
    )


def _unknown_corporate_status() -> CorporateStatusEvidence:
    unknown = CorporateRiskSignal(signal_type="x", status=STATUS_UNKNOWN, basis="insufficient evidence")
    return CorporateStatusEvidence(
        symbol="AAPL", as_of=AS_OF, reporting_status=REPORTING_STATUS_UNKNOWN, reporting_status_reason="no filings",
        earliest_reliable_filing_date=None, operating_history_years=None,
        latest_annual_filing=None, latest_quarterly_filing=None, late_filing_notices=(),
        bankruptcy_signals=(unknown,), delisting_signals=(unknown,), registration_status_signals=(unknown,),
        shell_company_signals=(unknown,), going_concern_signals=(unknown,),
        completeness_status="UNAVAILABLE", sources=(),
    )


def _conflicting_corporate_status() -> CorporateStatusEvidence:
    conflicting = CorporateRiskSignal(signal_type="x", status=STATUS_CONFLICTING, basis="sources disagree")
    return CorporateStatusEvidence(
        symbol="AAPL", as_of=AS_OF, reporting_status=REPORTING_STATUS_ACTIVE, reporting_status_reason=None,
        earliest_reliable_filing_date=date(2015, 1, 1), operating_history_years=Decimal("11.5"),
        latest_annual_filing=None, latest_quarterly_filing=None, late_filing_notices=(),
        bankruptcy_signals=(conflicting,), delisting_signals=(), registration_status_signals=(),
        shell_company_signals=(), going_concern_signals=(), completeness_status=COMPLETENESS_COMPLETE, sources=(),
    )


# --- news missing but screening complete -------------------------------------

def test_news_missing_does_not_block_screening():
    result = evaluate_completeness(
        symbol="AAPL", snapshot_outcome=OUTCOME_COMPLETE, corporate_status=_clean_corporate_status(),
        news_present=False, sentiment_present=True,
    )
    assert result.screening_completeness == STATUS_COMPLETE_FOR_SCREENING
    assert result.screening_blocked is False
    assert STATUS_MISSING_NEWS in result.blocking_categories
    assert result.research_completeness == STATUS_PARTIAL_NONCRITICAL


def test_sentiment_missing_does_not_block_screening():
    result = evaluate_completeness(
        symbol="AAPL", snapshot_outcome=OUTCOME_COMPLETE, corporate_status=_clean_corporate_status(),
        news_present=True, sentiment_present=False,
    )
    assert result.screening_completeness == STATUS_COMPLETE_FOR_SCREENING
    assert STATUS_MISSING_SENTIMENT in result.blocking_categories


def test_both_news_and_sentiment_missing_still_screening_complete():
    result = evaluate_completeness(
        symbol="AAPL", snapshot_outcome=OUTCOME_COMPLETE, corporate_status=_clean_corporate_status(),
        news_present=False, sentiment_present=False,
    )
    assert result.screening_completeness == STATUS_COMPLETE_FOR_SCREENING
    assert STATUS_MISSING_NEWS in result.blocking_categories
    assert STATUS_MISSING_SENTIMENT in result.blocking_categories


# --- fully complete ---------------------------------------------------------

def test_fully_complete_snapshot_and_corporate_status():
    result = evaluate_completeness(
        symbol="AAPL", snapshot_outcome=OUTCOME_COMPLETE, corporate_status=_clean_corporate_status(),
        news_present=True, sentiment_present=True,
    )
    assert result.screening_completeness == STATUS_COMPLETE_FOR_SCREENING
    assert result.research_completeness == STATUS_COMPLETE_FOR_RESEARCH
    assert result.blocking_categories == ()


# --- critical status missing blocks -------------------------------------------

def test_corporate_status_none_blocks_screening():
    result = evaluate_completeness(
        symbol="AAPL", snapshot_outcome=OUTCOME_COMPLETE, corporate_status=None,
    )
    assert result.screening_completeness == STATUS_MISSING_CRITICAL_CORPORATE_STATUS
    assert result.screening_blocked is True


def test_corporate_status_unknown_blocks_screening():
    result = evaluate_completeness(
        symbol="AAPL", snapshot_outcome=OUTCOME_COMPLETE, corporate_status=_unknown_corporate_status(),
    )
    assert result.screening_completeness == STATUS_MISSING_CRITICAL_CORPORATE_STATUS
    assert result.screening_blocked is True


def test_missing_market_data_blocks():
    result = evaluate_completeness(
        symbol="AAPL", snapshot_outcome=OUTCOME_INCOMPLETE_REQUIRED_DATA,
        snapshot_reasons=("required evidence category missing: market",),
        corporate_status=_clean_corporate_status(),
    )
    assert result.screening_completeness == STATUS_MISSING_CRITICAL_MARKET_DATA
    assert result.screening_blocked is True


def test_missing_fundamentals_blocks():
    result = evaluate_completeness(
        symbol="AAPL", snapshot_outcome=OUTCOME_INCOMPLETE_REQUIRED_DATA,
        snapshot_reasons=("required evidence category missing: fundamentals",),
        corporate_status=_clean_corporate_status(),
    )
    assert result.screening_completeness == STATUS_MISSING_CRITICAL_FUNDAMENTALS
    assert result.screening_blocked is True


# --- conflicting critical evidence blocks --------------------------------------

def test_conflicting_corporate_status_blocks():
    result = evaluate_completeness(
        symbol="AAPL", snapshot_outcome=OUTCOME_COMPLETE, corporate_status=_conflicting_corporate_status(),
    )
    assert result.screening_completeness == STATUS_MISSING_CRITICAL_CORPORATE_STATUS
    assert result.screening_blocked is True


def test_conflicting_snapshot_outcome_blocks():
    result = evaluate_completeness(
        symbol="AAPL", snapshot_outcome=OUTCOME_COMPLETE_WITH_CONFLICTS, corporate_status=_clean_corporate_status(),
    )
    assert result.screening_completeness == STATUS_CONFLICTING_CRITICAL_DATA
    assert result.screening_blocked is True


# --- unsafe evidence blocks -----------------------------------------------------

def test_point_in_time_unsafe_blocks():
    result = evaluate_completeness(
        symbol="AAPL", snapshot_outcome=OUTCOME_POINT_IN_TIME_UNSAFE, corporate_status=_clean_corporate_status(),
    )
    assert result.screening_completeness == STATUS_POINT_IN_TIME_UNSAFE
    assert result.screening_blocked is True


def test_provider_unavailable_blocks():
    result = evaluate_completeness(
        symbol="AAPL", snapshot_outcome=OUTCOME_PROVIDER_UNAVAILABLE, corporate_status=_clean_corporate_status(),
    )
    assert result.screening_completeness == STATUS_PROVIDER_UNAVAILABLE
    assert result.screening_blocked is True


# --- policy version persisted / present -----------------------------------------

def test_policy_version_stamped_on_every_result():
    result = evaluate_completeness(
        symbol="AAPL", snapshot_outcome=OUTCOME_COMPLETE, corporate_status=_clean_corporate_status(),
    )
    assert result.policy_version == POLICY_VERSION
    assert result.policy_version != ""


# --- every status exercised at least once ---------------------------------------

def test_all_status_values_are_reachable_or_at_least_valid_vocabulary():
    # STATUS_MISSING_NEWS / STATUS_MISSING_SENTIMENT are exercised in
    # test_news_missing_does_not_block_screening /
    # test_sentiment_missing_does_not_block_screening above (as
    # non-blocking members of blocking_categories); the remaining ones
    # are exercised via dedicated tests. This test just asserts every
    # value in the vocabulary is a valid string constant used somewhere.
    exercised = {
        STATUS_COMPLETE_FOR_SCREENING, STATUS_COMPLETE_FOR_RESEARCH, STATUS_PARTIAL_NONCRITICAL,
        STATUS_MISSING_CRITICAL_CORPORATE_STATUS, STATUS_MISSING_CRITICAL_MARKET_DATA,
        STATUS_MISSING_CRITICAL_FUNDAMENTALS, STATUS_MISSING_NEWS, STATUS_MISSING_SENTIMENT,
        STATUS_CONFLICTING_CRITICAL_DATA, STATUS_POINT_IN_TIME_UNSAFE, STATUS_PROVIDER_UNAVAILABLE,
    }
    assert exercised == set(ALL_STATUS_VALUES)


# --- deterministic / no model influence ------------------------------------------

def test_pure_function_same_inputs_same_output():
    r1 = evaluate_completeness(
        symbol="AAPL", snapshot_outcome=OUTCOME_COMPLETE, corporate_status=_clean_corporate_status(),
    )
    r2 = evaluate_completeness(
        symbol="AAPL", snapshot_outcome=OUTCOME_COMPLETE, corporate_status=_clean_corporate_status(),
    )
    assert r1 == r2


def test_invalid_status_rejected():
    from trading_research.research.evidence_completeness import EvidenceCompletenessResult

    with pytest.raises(ValueError):
        EvidenceCompletenessResult(
            symbol="AAPL", screening_completeness="NOT_A_REAL_STATUS",
            research_completeness=STATUS_COMPLETE_FOR_RESEARCH, blocking_categories=(),
            policy_version=POLICY_VERSION,
        )
