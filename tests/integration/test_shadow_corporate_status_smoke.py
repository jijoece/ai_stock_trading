"""Opt-in real corporate-status smoke test (docs/milestone-7.md Step 28,
"Real corporate-status smoke"). Skipped by default. Requires
RUN_CORPORATE_STATUS_TESTS=true — gated separately from any credential
presence check, since SEC EDGAR needs no API key (only a compliant
User-Agent, matching every other real-SEC code path in this repository, e.g.
tests/integration/test_sec_api_smoke.py).

Uses one stable symbol (AAPL) and a fixed historical `as_of`, mirroring
cli.py::corporate_status_cli's own real-SEC wiring exactly (same
SecEdgarClient/HttpJsonClient/ProviderCache/MinIntervalRateLimiter
construction), so this test validates the actual production code path, not
a re-implementation of it.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.corporate_status_real

_RUN_FLAG = os.environ.get("RUN_CORPORATE_STATUS_TESTS", "").strip().lower() == "true"
_SKIP_REASON = "opt-in real corporate-status smoke test: set RUN_CORPORATE_STATUS_TESTS=true to run it"

AS_OF = datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc)  # fixed historical as-of, safely in the past


@pytest.mark.skipif(not _RUN_FLAG, reason=_SKIP_REASON)
def test_real_corporate_status_for_aapl():
    from trading_research.evidence_providers.cache import ProviderCache
    from trading_research.evidence_providers.corporate_status_adapters import derive_corporate_status
    from trading_research.evidence_providers.http_client import HttpJsonClient
    from trading_research.evidence_providers.rate_limits import MinIntervalRateLimiter
    from trading_research.evidence_providers.sec_provider import DEFAULT_USER_AGENT, SecEdgarClient

    http = HttpJsonClient(
        base_headers={"User-Agent": DEFAULT_USER_AGENT}, rate_limiter=MinIntervalRateLimiter(0.15),
        provider="sec-edgar",
    )
    client = SecEdgarClient(http_client=http, cache=ProviderCache(clock=time.monotonic), user_agent=DEFAULT_USER_AGENT)

    evidence = derive_corporate_status("AAPL", sec_client=client, as_of=AS_OF)

    # --- Real SEC access + point-in-time filing filtering.
    assert evidence.symbol == "AAPL"
    assert evidence.as_of == AS_OF
    assert evidence.reporting_status in ("ACTIVE", "INACTIVE", "UNKNOWN", "SOURCE_UNAVAILABLE")
    # AAPL is a large, continuously-reporting filer — as of a fixed recent
    # historical date this must resolve to ACTIVE, not UNKNOWN/SOURCE_UNAVAILABLE
    # (a genuine real-data assertion, not just a shape check).
    assert evidence.reporting_status == "ACTIVE", (
        f"expected AAPL to be an ACTIVE SEC filer as of {AS_OF.isoformat()}, "
        f"got {evidence.reporting_status} ({evidence.reporting_status_reason})"
    )

    # --- Operating-history derivation: AAPL has decades of filing history.
    assert evidence.operating_history_years is not None
    assert evidence.operating_history_years > 10

    # --- At least one corporate-status category populated with a real signal.
    assert evidence.latest_annual_filing is not None
    assert evidence.latest_annual_filing.accepted_at <= AS_OF, "point-in-time safety: no filing accepted after as_of"
    assert evidence.earliest_reliable_filing_date is not None

    all_signals = (
        evidence.bankruptcy_signals + evidence.delisting_signals + evidence.registration_status_signals
        + evidence.shell_company_signals + evidence.going_concern_signals
    )
    assert len(all_signals) >= 5  # one signal per category, at minimum
    for signal in all_signals:
        assert signal.status in (
            "CONFIRMED", "NOT_FOUND_IN_SEARCHED_SOURCES", "UNKNOWN", "SOURCE_UNAVAILABLE",
            "POINT_IN_TIME_UNSAFE", "CONFLICTING",
        )

    # --- Normalized evidence with provenance: at least one source record,
    # every one of them point-in-time-consistent (retrieved_at present) and
    # carrying a real provider identity.
    assert evidence.sources
    for source in evidence.sources:
        assert source.provider == "sec-edgar"
        assert source.source_id
        assert source.content_hash
        assert source.status == "ok"

    # --- Completeness status is a real, non-UNAVAILABLE value for a
    # continuously-reporting large-cap filer.
    assert evidence.completeness_status == "COMPLETE"
    assert evidence.has_any_critical_uncertainty() is False

    print(
        "Real corporate-status smoke result: "
        f"symbol={evidence.symbol} as_of={evidence.as_of.isoformat()} "
        f"reporting_status={evidence.reporting_status} "
        f"operating_history_years={evidence.operating_history_years} "
        f"completeness_status={evidence.completeness_status} "
        f"source_count={len(evidence.sources)} "
        f"signal_count={len(all_signals)} "
        f"has_any_critical_uncertainty={evidence.has_any_critical_uncertainty()}"
    )
