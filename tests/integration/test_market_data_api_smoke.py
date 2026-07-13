"""Opt-in real Alpaca market-data smoke test (Milestone 6, docs/milestone-6.md
Step 23). Never runs automatically — requires `RUN_MARKET_DATA_TESTS=true`
**and** `ALPACA_MARKET_DATA_API_KEY`/`ALPACA_MARKET_DATA_API_SECRET`
(deliberately distinct from the Milestone 4 paper-broker credential pair —
see .env.example)."""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from trading_research.evidence_providers.cache import ProviderCache
from trading_research.evidence_providers.evidence_adapters import RealMarketEvidenceProvider
from trading_research.evidence_providers.http_client import HttpJsonClient
from trading_research.evidence_providers.market_data_provider import AlpacaMarketDataClient
from trading_research.evidence_providers.rate_limits import MinIntervalRateLimiter

pytestmark = pytest.mark.market_data_api

SKIP_REASON = "RUN_MARKET_DATA_TESTS is not 'true' or ALPACA_MARKET_DATA_API_KEY/SECRET are absent — skipped by default"


def _should_run() -> bool:
    return (
        os.environ.get("RUN_MARKET_DATA_TESTS") == "true"
        and bool(os.environ.get("ALPACA_MARKET_DATA_API_KEY"))
        and bool(os.environ.get("ALPACA_MARKET_DATA_API_SECRET"))
    )


@pytest.mark.skipif(not _should_run(), reason=SKIP_REASON)
def test_real_alpaca_market_data_connectivity_and_normalization():
    as_of = datetime.now(timezone.utc) - timedelta(days=1)
    http = HttpJsonClient(
        base_headers={
            "APCA-API-KEY-ID": os.environ["ALPACA_MARKET_DATA_API_KEY"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_MARKET_DATA_API_SECRET"],
        },
        rate_limiter=MinIntervalRateLimiter(0.35), provider="alpaca-data",
    )
    client = AlpacaMarketDataClient(
        api_key=os.environ["ALPACA_MARKET_DATA_API_KEY"], api_secret=os.environ["ALPACA_MARKET_DATA_API_SECRET"],
        http_client=http, cache=ProviderCache(clock=time.monotonic),
    )

    start = (as_of - timedelta(days=14)).date()
    bars = client.get_price_history("AAPL", start=start, end=as_of.date(), as_of=as_of)
    assert len(bars) > 0
    assert all(b.session_date <= as_of.date() for b in bars)  # no future bars
    assert all(b.adjusted is False for b in bars)  # explicit adjustment metadata

    benchmark_bars = client.get_price_history("SPY", start=start, end=as_of.date(), as_of=as_of)
    assert len(benchmark_bars) > 0

    close = client.get_close("AAPL", bars[-1].session_date)
    assert close is not None
    assert close.close == bars[-1].close

    bundle = RealMarketEvidenceProvider(client).fetch("AAPL", as_of)
    assert bundle.evidence_items
    assert bundle.source_records
