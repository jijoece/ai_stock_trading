"""Unit tests for evidence_providers/alpaca_news_provider.py — Milestone 7
docs/milestone-7.md Step 27 category D. No real network: httpx.MockTransport
only."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from trading_research.evidence_providers.alpaca_news_provider import (
    MAX_ARTICLES_RETURNED,
    RETENTION_ACCOUNT_LINKED,
    SOURCE_TRUST_ALPACA_AGGREGATED,
    AlpacaNewsClient,
)
from trading_research.evidence_providers.cache import ProviderCache
from trading_research.evidence_providers.errors import (
    MalformedProviderResponseError,
    ProviderConfigurationError,
    ProviderRateLimitedError,
    RetryBoundExceededError,
)
from trading_research.evidence_providers.http_client import HttpJsonClient
from trading_research.evidence_providers.rate_limits import MinIntervalRateLimiter

AS_OF = datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc)
PUBLISHED_AFTER = AS_OF - timedelta(days=7)

BASE_NEWS_BODY = {
    "news": [
        {
            "id": 1001, "headline": "Company X beats earnings", "source": "benzinga",
            "created_at": "2026-07-10T14:00:00Z", "url": "https://example.com/1",
            "symbols": ["AAPL"], "summary": "Solid quarter.",
        },
        {
            "id": 1002, "headline": "Company X announces buyback", "source": "benzinga",
            "created_at": "2026-07-11T09:00:00Z", "url": "https://example.com/2",
            "symbols": ["AAPL"], "summary": "Buyback program expanded.",
        },
    ],
    "next_page_token": None,
}


def _client(handler, *, cache=None) -> AlpacaNewsClient:
    transport = httpx.MockTransport(handler)
    http = HttpJsonClient(
        base_headers={"APCA-API-KEY-ID": "k", "APCA-API-SECRET-KEY": "s"},
        rate_limiter=MinIntervalRateLimiter(0.0), transport=transport, provider="alpaca-news",
    )
    return AlpacaNewsClient(api_key="k", api_secret="s", http_client=http, cache=cache)


# -- authentication presence ------------------------------------------------

def test_requires_credentials():
    http = HttpJsonClient(base_headers={}, rate_limiter=MinIntervalRateLimiter(0.0))
    with pytest.raises(ProviderConfigurationError):
        AlpacaNewsClient(api_key=None, api_secret="s", http_client=http)
    with pytest.raises(ProviderConfigurationError):
        AlpacaNewsClient(api_key="k", api_secret=None, http_client=http)


def test_sends_alpaca_auth_headers():
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("apca-api-key-id")
        seen["secret"] = request.headers.get("apca-api-secret-key")
        return httpx.Response(200, json=BASE_NEWS_BODY)

    client = _client(handler)
    client.list_news("AAPL", published_after=PUBLISHED_AFTER, available_by=AS_OF)
    assert seen["key"] == "k"
    assert seen["secret"] == "s"


# -- article normalization ---------------------------------------------------

def test_article_normalization():
    def handler(request):
        return httpx.Response(200, json=BASE_NEWS_BODY)

    client = _client(handler)
    normalized = client.list_news_normalized("AAPL", published_after=PUBLISHED_AFTER, available_by=AS_OF)
    assert len(normalized) == 2
    first = normalized[0]
    assert first.article.article_id == "1001"
    assert first.article.headline == "Company X beats earnings"
    assert first.article.source == "benzinga"
    assert first.article.symbols == ("AAPL",)
    assert first.article.content_hash
    assert first.source_trust_classification == SOURCE_TRUST_ALPACA_AGGREGATED
    assert first.retention_classification == RETENTION_ACCOUNT_LINKED
    assert "untrusted" in first.prompt_injection_risk_note.lower()
    # sorted ascending by publication time
    assert normalized[0].article.published_at <= normalized[1].article.published_at


# -- publication cutoff (point-in-time) --------------------------------------

def test_excludes_future_articles():
    future_body = {
        "news": BASE_NEWS_BODY["news"] + [{
            "id": 1003, "headline": "Future scoop", "source": "benzinga",
            "created_at": "2026-12-31T00:00:00Z", "url": None, "symbols": ["AAPL"], "summary": "",
        }],
        "next_page_token": None,
    }

    def handler(request):
        return httpx.Response(200, json=future_body)

    client = _client(handler)
    articles = client.list_news("AAPL", published_after=PUBLISHED_AFTER, available_by=AS_OF)
    assert all(a.published_at <= AS_OF for a in articles)
    assert "Future scoop" not in [a.headline for a in articles]


# -- duplicate syndication ----------------------------------------------------

def test_deduplicates_syndicated_copies():
    dup_body = {
        "news": [
            {
                "id": 2001, "headline": "Company X beats earnings", "source": "benzinga",
                "created_at": "2026-07-10T14:00:00Z", "url": "https://a.com/1",
                "symbols": ["AAPL"], "summary": "wire copy 1",
            },
            {
                # same headline+source+minute -> syndicated duplicate
                "id": 2002, "headline": "Company X beats earnings", "source": "benzinga",
                "created_at": "2026-07-10T14:00:30Z", "url": "https://b.com/1",
                "symbols": ["AAPL"], "summary": "wire copy 2",
            },
        ],
        "next_page_token": None,
    }

    def handler(request):
        return httpx.Response(200, json=dup_body)

    client = _client(handler)
    articles = client.list_news("AAPL", published_after=PUBLISHED_AFTER, available_by=AS_OF)
    assert len(articles) == 1


def test_defensive_duplicate_id_across_pages_collapsed():
    page1 = {"news": [BASE_NEWS_BODY["news"][0]], "next_page_token": "tok"}
    page2 = {"news": [BASE_NEWS_BODY["news"][0]], "next_page_token": None}  # same id repeated
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=page1 if calls["n"] == 1 else page2)

    client = _client(handler)
    articles = client.list_news("AAPL", published_after=PUBLISHED_AFTER, available_by=AS_OF)
    assert len(articles) == 1


# -- symbol ambiguity handling -------------------------------------------------

def test_symbols_field_reflects_provider_multi_symbol_tagging():
    body = {
        "news": [{
            "id": 3001, "headline": "Sector-wide move", "source": "benzinga",
            "created_at": "2026-07-10T14:00:00Z", "url": None,
            "symbols": ["AAPL", "MSFT"], "summary": "",
        }],
        "next_page_token": None,
    }

    def handler(request):
        return httpx.Response(200, json=body)

    client = _client(handler)
    articles = client.list_news("AAPL", published_after=PUBLISHED_AFTER, available_by=AS_OF)
    assert articles[0].symbols == ("AAPL", "MSFT")


# -- size cap -------------------------------------------------------------------

def test_caps_article_count():
    many_articles = [
        {
            "id": i, "headline": f"Headline {i}", "source": "benzinga",
            "created_at": f"2026-07-{(i % 9) + 1:02d}T10:00:00Z", "url": None,
            "symbols": ["AAPL"], "summary": "x",
        }
        for i in range(300)
    ]

    def handler(request):
        return httpx.Response(200, json={"news": many_articles, "next_page_token": None})

    client = _client(handler)
    articles = client.list_news("AAPL", published_after=PUBLISHED_AFTER, available_by=AS_OF)
    assert len(articles) <= MAX_ARTICLES_RETURNED


def test_caps_summary_content_size():
    huge_summary = "x" * 10_000
    body = {
        "news": [{
            "id": 4001, "headline": "Long story", "source": "benzinga",
            "created_at": "2026-07-10T14:00:00Z", "url": None,
            "symbols": ["AAPL"], "summary": huge_summary,
        }],
        "next_page_token": None,
    }

    def handler(request):
        return httpx.Response(200, json=body)

    client = _client(handler)
    articles = client.list_news("AAPL", published_after=PUBLISHED_AFTER, available_by=AS_OF)
    assert len(articles[0].summary) <= 4000


# -- provider failure -------------------------------------------------------------

def test_malformed_response_raises():
    def handler(request):
        return httpx.Response(200, json={"not_news": []})

    client = _client(handler)
    with pytest.raises(MalformedProviderResponseError):
        client.list_news("AAPL", published_after=PUBLISHED_AFTER, available_by=AS_OF)


def test_server_error_retried_then_raises():
    def handler(request):
        return httpx.Response(500, json={"error": "boom"})

    transport = httpx.MockTransport(handler)
    http = HttpJsonClient(
        base_headers={"APCA-API-KEY-ID": "k", "APCA-API-SECRET-KEY": "s"},
        rate_limiter=MinIntervalRateLimiter(0.0), transport=transport, provider="alpaca-news", max_attempts=2,
    )
    client = AlpacaNewsClient(api_key="k", api_secret="s", http_client=http)
    with pytest.raises(RetryBoundExceededError):
        client.list_news("AAPL", published_after=PUBLISHED_AFTER, available_by=AS_OF)


# -- rate limit behavior (reuse existing rate-limit test pattern) ----------------

def test_rate_limited_response_raises_after_retries():
    def handler(request):
        return httpx.Response(429, json={"error": "rate limited"})

    transport = httpx.MockTransport(handler)
    http = HttpJsonClient(
        base_headers={"APCA-API-KEY-ID": "k", "APCA-API-SECRET-KEY": "s"},
        rate_limiter=MinIntervalRateLimiter(0.0), transport=transport, provider="alpaca-news", max_attempts=2,
    )
    client = AlpacaNewsClient(api_key="k", api_secret="s", http_client=http)
    with pytest.raises(RetryBoundExceededError):
        client.list_news("AAPL", published_after=PUBLISHED_AFTER, available_by=AS_OF)


def test_rate_limiter_enforces_minimum_interval():
    waits: list[float] = []
    fake_time = [0.0]

    def fake_clock():
        return fake_time[0]

    def fake_sleep(seconds):
        waits.append(seconds)
        fake_time[0] += seconds

    limiter = MinIntervalRateLimiter(0.35, clock=fake_clock, sleep_fn=fake_sleep)

    def handler(request):
        return httpx.Response(200, json=BASE_NEWS_BODY)

    transport = httpx.MockTransport(handler)
    http = HttpJsonClient(
        base_headers={"APCA-API-KEY-ID": "k", "APCA-API-SECRET-KEY": "s"},
        rate_limiter=limiter, transport=transport, provider="alpaca-news",
    )
    client = AlpacaNewsClient(api_key="k", api_secret="s", http_client=http)
    client.list_news("AAPL", published_after=PUBLISHED_AFTER, available_by=AS_OF)
    fake_time[0] += 0.1  # simulate elapsed time less than the interval
    client.list_news("AAPL", published_after=PUBLISHED_AFTER, available_by=AS_OF)
    assert any(w > 0 for w in waits)


# -- licensing/retention classification present ----------------------------------

def test_retention_and_trust_classification_present_on_every_article():
    def handler(request):
        return httpx.Response(200, json=BASE_NEWS_BODY)

    client = _client(handler)
    normalized = client.list_news_normalized("AAPL", published_after=PUBLISHED_AFTER, available_by=AS_OF)
    assert all(n.retention_classification == RETENTION_ACCOUNT_LINKED for n in normalized)
    assert all(n.source_trust_classification == SOURCE_TRUST_ALPACA_AGGREGATED for n in normalized)
    assert all(n.duplicate_group_key for n in normalized)


# -- explicit environment-pending behavior when credentials absent --------------

def test_environment_pending_when_credentials_absent():
    http = HttpJsonClient(base_headers={}, rate_limiter=MinIntervalRateLimiter(0.0))
    with pytest.raises(ProviderConfigurationError, match="ALPACA_MARKET_DATA_API_KEY"):
        AlpacaNewsClient(api_key=None, api_secret=None, http_client=http)


# -- caching -----------------------------------------------------------------

def test_cache_hit_avoids_second_network_call():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=BASE_NEWS_BODY)

    cache = ProviderCache(clock=time.monotonic)
    client = _client(handler, cache=cache)
    client.list_news("AAPL", published_after=PUBLISHED_AFTER, available_by=AS_OF)
    client.list_news("AAPL", published_after=PUBLISHED_AFTER, available_by=AS_OF)
    assert calls["n"] == 1
