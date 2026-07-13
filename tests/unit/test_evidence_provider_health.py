"""Milestone 6.1 Step 18/19: provider-concentration output test.

`evidence_providers/health.py` had no dedicated test file before this session (the
Milestone 6 `provider-health` CLI command was only exercised manually — see the M6
scratchpad's "Environmental validation" #7). This file adds a narrow test for the new
`compute_provider_concentration` function only, not a full backfill of Milestone 6
coverage, which is out of this session's scope.
"""
from __future__ import annotations

from trading_research.evidence_providers.health import (
    REDUNDANCY_SINGLE_PROVIDER_PER_CATEGORY,
    compute_provider_concentration,
)


def test_provider_concentration_reports_single_provider_per_category():
    concentration = compute_provider_concentration()
    assert concentration["market_data_provider_count"] == 1
    assert concentration["filing_provider_count"] == 1
    assert concentration["fundamentals_provider_count"] == 1
    assert concentration["news_provider_count"] == 0
    assert concentration["sentiment_provider_count"] == 0
    assert concentration["redundancy_status"] == REDUNDANCY_SINGLE_PROVIDER_PER_CATEGORY


def test_provider_concentration_is_deterministic():
    assert compute_provider_concentration() == compute_provider_concentration()
