from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from trading_research.evidence_providers.config import RedditFreeProviderConfig
from trading_research.evidence_providers.reddit_free import RedditFreeProvider


@pytest.mark.reddit_free_real
@pytest.mark.skipif(os.environ.get("RUN_REDDIT_FREE_TESTS") != "1", reason="set RUN_REDDIT_FREE_TESTS=1 to enable")
def test_live_reddit_rss_returns_aapl_post(tmp_path):
    config = RedditFreeProviderConfig(
        enabled=True,
        provider_class="trading_research.evidence_providers.reddit_free.RedditFreeProvider",
        cache_ttl_minutes=60,
        user_agent="AgenticTradingDesk/1.0",
        subreddits=("wallstreetbets", "stocks", "investing"),
        max_posts_per_symbol=100,
        request_timeout_seconds=20,
        max_attempts=3,
        min_request_interval_seconds=2.0,
        max_requests_per_endpoint_hour=30,
    )
    with RedditFreeProvider(config, data_dir=tmp_path) as provider:
        result = provider.fetch("AAPL", datetime.now(timezone.utc))
    assert len(result.posts) >= 1
