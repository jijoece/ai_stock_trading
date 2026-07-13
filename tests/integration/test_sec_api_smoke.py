"""Opt-in real SEC EDGAR smoke test (Milestone 6, docs/milestone-6.md Step
23). Never runs automatically — requires `RUN_SEC_API_TESTS=true`. SEC EDGAR
needs no API key, only a compliant `User-Agent`, so this can run whenever
network access is available and the flag is set; it never runs just because
network happens to be reachable.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import pytest

from trading_research.evidence_providers.cache import ProviderCache
from trading_research.evidence_providers.evidence_adapters import RealFundamentalsEvidenceProvider
from trading_research.evidence_providers.http_client import HttpJsonClient
from trading_research.evidence_providers.rate_limits import MinIntervalRateLimiter
from trading_research.evidence_providers.sec_provider import DEFAULT_USER_AGENT, SecEdgarClient

pytestmark = pytest.mark.sec_api

SKIP_REASON = "RUN_SEC_API_TESTS is not 'true' — real SEC EDGAR smoke test skipped by default"


@pytest.mark.skipif(os.environ.get("RUN_SEC_API_TESTS") != "true", reason=SKIP_REASON)
def test_real_sec_edgar_connectivity_and_normalization():
    as_of = datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc)
    http = HttpJsonClient(
        base_headers={"User-Agent": DEFAULT_USER_AGENT},
        rate_limiter=MinIntervalRateLimiter(0.15), provider="sec-edgar",
    )
    client = SecEdgarClient(http_client=http, cache=ProviderCache(clock=time.monotonic), user_agent=DEFAULT_USER_AGENT)

    cik = client.resolve_cik("AAPL")
    assert cik == "0000320193"

    filings = client.list_filings("AAPL", available_by=as_of, cik=cik)
    assert len(filings) > 0
    assert all(f.accepted_at <= as_of for f in filings)  # point-in-time safe

    facts = client.get_company_facts("AAPL", as_of=as_of, cik=cik)
    assert len(facts) > 0

    bundle = RealFundamentalsEvidenceProvider(client).fetch("AAPL", as_of)
    assert bundle.evidence_items  # normalized into the existing EvidenceBundle/EvidenceItem shape
    assert all(item.category == "fundamentals" for item in bundle.evidence_items)
    assert bundle.source_records
    assert all(sr.point_in_time_safe for sr in bundle.source_records)
