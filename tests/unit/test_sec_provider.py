"""Unit tests for evidence_providers/sec_provider.py — Milestone 6
docs/milestone-6.md Step 22 category B. No real network: every request is
served by `httpx.MockTransport`."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import httpx
import pytest

from trading_research.evidence_providers.cache import ProviderCache
from trading_research.evidence_providers.errors import (
    MalformedProviderResponseError,
    ProviderConfigurationError,
    ProviderRequestError,
    RetryBoundExceededError,
)
from trading_research.evidence_providers.http_client import HttpJsonClient
from trading_research.evidence_providers.rate_limits import MinIntervalRateLimiter
from trading_research.evidence_providers.sec_provider import SecEdgarClient

AS_OF = datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc)

TICKER_INDEX_BODY = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}

SUBMISSIONS_BODY = {
    "cik": "320193",
    "filings": {
        "recent": {
            "accessionNumber": ["0000320193-26-000010", "0000320193-26-000011"],
            "filingDate": ["2026-05-01", "2026-08-01"],
            "acceptanceDateTime": ["2026-05-01T20:00:00.000Z", "2026-08-01T20:00:00.000Z"],
            "form": ["10-Q", "10-Q"],
            "reportDate": ["2026-03-31", "2026-06-30"],
            "primaryDocument": ["aapl-10q.htm", "aapl-10q2.htm"],
        }
    },
}

COMPANY_FACTS_BODY = {
    "cik": 320193,
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        {"start": "2025-01-01", "end": "2025-12-31", "val": 1000, "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-01", "accn": "x"},
                    ]
                }
            }
        }
    },
}


def _client(handler, *, max_attempts=2) -> SecEdgarClient:
    transport = httpx.MockTransport(handler)
    http = HttpJsonClient(
        base_headers={"User-Agent": "test-agent contact@example.com"},
        rate_limiter=MinIntervalRateLimiter(0.0, sleep_fn=lambda s: None),
        max_attempts=max_attempts, transport=transport, provider="sec-edgar",
    )
    return SecEdgarClient(http_client=http, cache=ProviderCache(clock=time.monotonic), user_agent="test-agent contact@example.com")


def test_requires_compliant_user_agent():
    http = HttpJsonClient(base_headers={}, rate_limiter=MinIntervalRateLimiter(0.0))
    with pytest.raises(ProviderConfigurationError):
        SecEdgarClient(http_client=http, user_agent="no-contact-info")


def test_resolve_cik_success():
    def handler(request):
        return httpx.Response(200, json=TICKER_INDEX_BODY)

    client = _client(handler)
    assert client.resolve_cik("AAPL") == "0000320193"
    assert client.resolve_cik("NOPE") is None


def test_resolve_cik_caches_index_across_calls():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=TICKER_INDEX_BODY)

    client = _client(handler)
    client.resolve_cik("AAPL")
    client.resolve_cik("AAPL")
    assert calls["n"] == 1


def test_list_filings_excludes_future_accepted_filing():
    def handler(request):
        if "submissions" in str(request.url):
            return httpx.Response(200, json=SUBMISSIONS_BODY)
        return httpx.Response(200, json=TICKER_INDEX_BODY)

    client = _client(handler)
    filings = client.list_filings("AAPL", available_by=AS_OF, cik="0000320193")
    assert len(filings) == 1  # the 2026-08-01 filing is after AS_OF -> excluded
    assert filings[0].accession_number == "0000320193-26-000010"
    assert filings[0].accepted_at <= AS_OF


def test_list_filings_missing_fields_raises_malformed():
    def handler(request):
        return httpx.Response(200, json={"cik": "1", "filings": {"recent": {}}})

    client = _client(handler)
    with pytest.raises(MalformedProviderResponseError):
        client.list_filings("AAPL", available_by=AS_OF, cik="1")


def test_get_company_facts_excludes_future_filed_value():
    def handler(request):
        return httpx.Response(200, json=COMPANY_FACTS_BODY)

    client = _client(handler)
    early_as_of = datetime(2026, 1, 1, tzinfo=timezone.utc)  # before filed=2026-02-01
    facts = client.get_company_facts("AAPL", as_of=early_as_of, cik="0000320193")
    assert facts == ()  # filed after early_as_of -> excluded, no look-ahead

    facts_later = client.get_company_facts("AAPL", as_of=AS_OF, cik="0000320193")
    assert len(facts_later) == 1
    assert facts_later[0].value == 1000


def test_non_retryable_4xx_is_not_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, text="not found")

    client = _client(handler, max_attempts=3)
    with pytest.raises(ProviderRequestError) as exc_info:
        client.list_filings("AAPL", available_by=AS_OF, cik="1")
    assert exc_info.value.retryable is False
    assert calls["n"] == 1  # no retry on a non-retryable 4xx


def test_retryable_5xx_is_bounded_retried_then_raises():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="unavailable")

    client = _client(handler, max_attempts=3)
    with pytest.raises(RetryBoundExceededError):
        client.list_filings("AAPL", available_by=AS_OF, cik="1")
    assert calls["n"] == 3  # exactly max_attempts, never infinite


def test_rate_limit_429_is_retried_and_marked():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json=SUBMISSIONS_BODY)

    client = _client(handler, max_attempts=2)
    filings = client.list_filings("AAPL", available_by=AS_OF, cik="1")
    assert calls["n"] == 2
    assert len(filings) == 1


def test_no_secret_values_appear_in_headers_sent():
    """SEC requires no credential at all — this asserts the client never
    accidentally sends anything resembling one."""
    seen_headers = {}

    def handler(request):
        seen_headers.update(dict(request.headers))
        return httpx.Response(200, json=TICKER_INDEX_BODY)

    client = _client(handler)
    client.resolve_cik("AAPL")
    for forbidden in ("authorization", "apca-api-key-id", "apca-api-secret-key", "x-api-key"):
        assert forbidden not in {k.lower() for k in seen_headers}
