"""Opt-in real Alpaca-news smoke test (docs/milestone-7.md Step 28, "Real
news smoke"). Gated on BOTH `RUN_NEWS_API_TESTS=true` AND real
`ALPACA_MARKET_DATA_API_KEY`/`ALPACA_MARKET_DATA_API_SECRET` credentials
being present — this repository's `AlpacaNewsClient`
(evidence_providers/alpaca_news_provider.py) fails closed at construction
time without both, so this test would error (not cleanly skip) if it tried
to construct one without credentials; the credential check below runs BEFORE
any construction attempt specifically to keep the skip clean.

At the time this test was authored, `ALPACA_MARKET_DATA_API_KEY`/`_SECRET`
are absent in this environment (confirmed via `.env` presence check, boolean
only) — this test is therefore expected to SKIP, not run, in that state.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.news_api

_RUN_FLAG = os.environ.get("RUN_NEWS_API_TESTS", "").strip().lower() == "true"
_API_KEY = os.environ.get("ALPACA_MARKET_DATA_API_KEY")
_API_SECRET = os.environ.get("ALPACA_MARKET_DATA_API_SECRET")

_SKIP_REASON = (
    "opt-in real Alpaca-news smoke test: set RUN_NEWS_API_TESTS=true AND real "
    "ALPACA_MARKET_DATA_API_KEY/ALPACA_MARKET_DATA_API_SECRET to run it — both credentials are "
    "absent in this environment, so this test is environmentally pending, not implemented-and-broken"
)


@pytest.mark.skipif(not (_RUN_FLAG and _API_KEY and _API_SECRET), reason=_SKIP_REASON)
def test_real_alpaca_news_connectivity_and_normalization():
    from trading_research.evidence_providers.alpaca_news_provider import AlpacaNewsClient
    from trading_research.evidence_providers.cache import ProviderCache
    from trading_research.evidence_providers.http_client import HttpJsonClient
    from trading_research.evidence_providers.rate_limits import MinIntervalRateLimiter

    now = datetime.now(timezone.utc)
    published_after = now - timedelta(days=14)

    http = HttpJsonClient(
        base_headers={"APCA-API-KEY-ID": _API_KEY, "APCA-API-SECRET-KEY": _API_SECRET},
        rate_limiter=MinIntervalRateLimiter(0.2), provider="alpaca-news",
    )
    client = AlpacaNewsClient(api_key=_API_KEY, api_secret=_API_SECRET, http_client=http, cache=ProviderCache(clock=__import__("time").monotonic))

    normalized = client.list_news_normalized("AAPL", published_after=published_after, available_by=now)

    # --- Authentication succeeded (no exception) + bounded historical query.
    assert isinstance(normalized, tuple)

    # --- Publication timestamps, normalized records, no future data.
    seen_ids = set()
    for item in normalized:
        assert item.article.published_at <= now, "no future-dated article may be returned"
        assert item.article.published_at >= published_after
        assert item.duplicate_group_key
        # --- Deduplication: group keys must not repeat identical articles.
        assert item.article.article_id not in seen_ids
        seen_ids.add(item.article.article_id)

    print(
        f"Real Alpaca-news smoke result: symbol=AAPL article_count={len(normalized)} "
        f"window_start={published_after.isoformat()} window_end={now.isoformat()}"
    )
