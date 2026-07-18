from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import httpx

from trading_research.evidence_providers.config import RedditFreeProviderConfig
from trading_research.evidence_providers.reddit_free import RedditFreeProvider
from trading_research.storage.trading_schema import apply_trading_schema

AS_OF = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)


def _config(**overrides) -> RedditFreeProviderConfig:
    values = {
        "enabled": True,
        "provider_class": "trading_research.evidence_providers.reddit_free.RedditFreeProvider",
        "cache_ttl_minutes": 60,
        "user_agent": "AgenticTradingDesk/1.0",
        "subreddits": ("stocks",),
        "max_posts_per_symbol": 100,
        "request_timeout_seconds": 5,
        "max_attempts": 3,
        "min_request_interval_seconds": 2.0,
        "max_requests_per_endpoint_hour": 30,
    }
    values.update(overrides)
    return RedditFreeProviderConfig(**values)


def _feed(*entries: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        + "".join(entries)
        + "</feed>"
    ).encode()


def _entry(post_id: str = "t3_good", title: str = "AAPL is a strong buy") -> str:
    return f"""
      <entry>
        <author><name>alice</name></author>
        <category term="stocks" label="r/stocks"/>
        <content type="html">&lt;p&gt;Bullish breakout with great earnings&lt;/p&gt;</content>
        <id>{post_id}</id>
        <link href="https://www.reddit.com/r/stocks/comments/good/"/>
        <updated>2026-07-17T11:00:00+00:00</updated>
        <published>2026-07-17T11:00:00+00:00</published>
        <title>{title}</title>
      </entry>
    """


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_trading_schema(conn)
    return conn


def test_parses_scores_and_deduplicates_posts_and_storage(tmp_path):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers.get("authorization") is None
        assert request.headers["user-agent"] == "AgenticTradingDesk/1.0"
        return httpx.Response(200, content=_feed(_entry(), _entry()), headers={"content-type": "application/atom+xml"})

    conn = _conn()
    provider = RedditFreeProvider(
        _config(), conn=conn, data_dir=tmp_path, transport=httpx.MockTransport(handler), sleep_fn=lambda _: None
    )
    result = provider.fetch("aapl", AS_OF)

    assert result.missing_data_reasons == ()
    assert len(result.posts) == 1
    assert len(result.records) == 1
    assert result.posts[0].sentiment_compound > 0
    assert result.average_sentiment == result.posts[0].sentiment_compound
    assert result.records[0].classification.label == "bullish"
    row = conn.execute("SELECT * FROM reddit_posts WHERE reddit_post_id = 't3_good'").fetchone()
    assert row["symbol"] == "AAPL"
    assert row["source_endpoint"].endswith("/r/stocks/search.rss")
    assert row["body"]

    # The second call is served from the one-hour disk cache and remains a
    # single stored row despite being persisted again.
    second = provider.fetch("AAPL", AS_OF)
    assert len(second.posts) == 1
    assert calls == 1
    assert conn.execute("SELECT COUNT(*) FROM reddit_posts").fetchone()[0] == 1


def test_broken_or_empty_responses_fail_closed(tmp_path):
    def broken(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not xml")

    provider = RedditFreeProvider(
        _config(max_attempts=1), data_dir=tmp_path, transport=httpx.MockTransport(broken), sleep_fn=lambda _: None
    )
    result = provider.fetch("AAPL", AS_OF)
    assert result.posts == ()
    assert result.net_sentiment is None
    assert result.missing_data_reasons

    def empty(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_feed())

    provider = RedditFreeProvider(
        _config(max_attempts=1), data_dir=tmp_path / "empty", transport=httpx.MockTransport(empty), sleep_fn=lambda _: None
    )
    assert provider.fetch("AAPL", AS_OF).posts == ()


def test_403_retries_then_disables_until_next_day(tmp_path):
    calls = 0

    def forbidden(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, content=b"blocked")

    provider = RedditFreeProvider(
        _config(), data_dir=tmp_path, transport=httpx.MockTransport(forbidden), sleep_fn=lambda _: None
    )
    assert provider.fetch("AAPL", AS_OF).posts == ()
    assert calls == 3
    assert provider.fetch("AAPL", AS_OF).posts == ()
    assert calls == 3


def test_posts_outside_24_hours_or_after_as_of_are_excluded(tmp_path):
    old = _entry("t3_old").replace("2026-07-17T11:00:00+00:00", "2026-07-15T11:00:00+00:00")
    future = _entry("t3_future").replace("2026-07-17T11:00:00+00:00", "2026-07-18T11:00:00+00:00")

    provider = RedditFreeProvider(
        _config(max_attempts=1),
        data_dir=tmp_path,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=_feed(old, future))),
        sleep_fn=lambda _: None,
    )
    assert provider.fetch("AAPL", AS_OF).posts == ()
