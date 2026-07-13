"""Unit tests for evidence_providers/market_data_provider.py — Milestone 6
docs/milestone-6.md Step 22 category C. No real network:
`httpx.MockTransport` only."""
from __future__ import annotations

import time
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest

from trading_research.evidence_providers.cache import ProviderCache
from trading_research.evidence_providers.errors import MalformedProviderResponseError, ProviderConfigurationError
from trading_research.evidence_providers.http_client import HttpJsonClient
from trading_research.evidence_providers.market_data_provider import AlpacaMarketDataClient
from trading_research.evidence_providers.rate_limits import MinIntervalRateLimiter

AS_OF = datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc)

BARS_BODY = {
    "bars": [
        {"t": "2026-07-08T04:00:00Z", "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0, "v": 1000},
        {"t": "2026-07-09T04:00:00Z", "o": 101.0, "h": 103.0, "l": 100.0, "c": 102.0, "v": 1100},
        {"t": "2026-07-10T04:00:00Z", "o": 102.0, "h": 104.0, "l": 101.0, "c": 103.0, "v": 1200},
    ],
    "symbol": "AAPL",
}

FUTURE_BAR_BODY = {
    "bars": BARS_BODY["bars"] + [{"t": "2026-12-31T04:00:00Z", "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1}],
    "symbol": "AAPL",
}


def _client(handler) -> AlpacaMarketDataClient:
    transport = httpx.MockTransport(handler)
    http = HttpJsonClient(
        base_headers={"APCA-API-KEY-ID": "k", "APCA-API-SECRET-KEY": "s"},
        rate_limiter=MinIntervalRateLimiter(0.0), transport=transport, provider="alpaca-data",
    )
    return AlpacaMarketDataClient(api_key="k", api_secret="s", http_client=http, cache=ProviderCache(clock=time.monotonic))


def test_requires_credentials():
    http = HttpJsonClient(base_headers={}, rate_limiter=MinIntervalRateLimiter(0.0))
    with pytest.raises(ProviderConfigurationError):
        AlpacaMarketDataClient(api_key=None, api_secret="s", http_client=http)
    with pytest.raises(ProviderConfigurationError):
        AlpacaMarketDataClient(api_key="k", api_secret=None, http_client=http)


def test_get_price_history_normalizes_bars():
    def handler(request):
        return httpx.Response(200, json=BARS_BODY)

    client = _client(handler)
    bars = client.get_price_history("AAPL", start=date(2026, 7, 8), end=date(2026, 7, 10), as_of=AS_OF)
    assert len(bars) == 3
    assert bars[0].session_date == date(2026, 7, 8)
    assert bars[0].close == Decimal("101.0")
    assert bars[0].adjusted is False
    # monotonic ascending order
    assert [b.session_date for b in bars] == sorted(b.session_date for b in bars)


def test_get_price_history_rejects_future_bars():
    def handler(request):
        return httpx.Response(200, json=FUTURE_BAR_BODY)

    client = _client(handler)
    bars = client.get_price_history("AAPL", start=date(2026, 7, 8), end=date(2026, 12, 31), as_of=AS_OF)
    assert all(b.session_date <= AS_OF.date() for b in bars)
    assert date(2026, 12, 31) not in {b.session_date for b in bars}


def test_get_price_history_adjustment_recorded():
    def handler(request):
        assert request.url.params.get("adjustment") == "split"
        return httpx.Response(200, json=BARS_BODY)

    client = _client(handler)
    bars = client.get_price_history("AAPL", start=date(2026, 7, 8), end=date(2026, 7, 10), as_of=AS_OF, adjustment="split")
    assert all(b.adjusted is True for b in bars)


def test_unknown_adjustment_fails_closed():
    client = _client(lambda r: httpx.Response(200, json=BARS_BODY))
    with pytest.raises(ProviderConfigurationError):
        client.get_price_history("AAPL", start=date(2026, 7, 8), end=date(2026, 7, 10), as_of=AS_OF, adjustment="bogus")


def test_malformed_bars_response_raises():
    def handler(request):
        return httpx.Response(200, json={"no_bars_key": True})

    client = _client(handler)
    with pytest.raises(MalformedProviderResponseError):
        client.get_price_history("AAPL", start=date(2026, 7, 8), end=date(2026, 7, 10), as_of=AS_OF)


def test_get_close_never_substitutes_current_quote_for_missing_historical():
    def handler(request):
        if "bars" in str(request.url):
            return httpx.Response(200, json={"bars": [], "symbol": "AAPL"})
        return httpx.Response(200, json={"quote": {"bp": 999.0, "ap": 999.0, "t": AS_OF.isoformat()}, "symbol": "AAPL"})

    client = _client(handler)
    point = client.get_close("AAPL", date(2026, 7, 8))
    assert point is None  # no historical bar found -> None, never a live quote


def test_get_close_returns_matching_session():
    def handler(request):
        return httpx.Response(200, json=BARS_BODY)

    client = _client(handler)
    point = client.get_close("AAPL", date(2026, 7, 9))
    assert point is not None
    assert point.close == Decimal("102.0")
    assert point.source == "alpaca-data"


def test_get_quote_no_fabricated_price_when_no_bid_or_ask():
    def handler(request):
        return httpx.Response(200, json={"quote": {"bp": 0, "ap": 0, "t": AS_OF.isoformat()}, "symbol": "AAPL"})

    client = _client(handler)
    quote = client.get_quote("AAPL", as_of=AS_OF)
    assert quote is None


def test_get_quote_uses_mid_when_both_present():
    def handler(request):
        return httpx.Response(200, json={"quote": {"bp": 100.0, "ap": 102.0, "t": AS_OF.isoformat()}, "symbol": "AAPL"})

    client = _client(handler)
    quote = client.get_quote("AAPL", as_of=AS_OF)
    assert quote.price == Decimal("101.0")
