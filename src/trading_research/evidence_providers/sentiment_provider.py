"""Real Reddit-sentiment evidence source (docs/milestone-6.md Step 10).

Reuses the existing deterministic aggregation pipeline
(`analysis/sentiment.py::aggregate`) — this module never re-implements
sentiment scoring, it only supplies already-fetched `RedditRecord`s to it.
Live retrieval goes through the existing MCP read-only allowlist
(`mcp/reddit_adapter.py::call_read_only_tool`), never a raw Reddit API call
and never a tool outside `config/tool_policy.yaml`'s allowlist.

**Real Reddit credentials are not configured in this environment**
(`REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` both empty — confirmed at
implementation time; anonymous Reddit API access is documented as blocked in
`.env.example`). `RedditSentimentSource` fails closed to an explicit
missing-data reason when no `fetch_records` callable is supplied, rather than
fabricating a live round trip this session cannot validate. When Reddit
credentials become available, a caller supplies `fetch_records` (an
already-authenticated wrapper around `call_read_only_tool`) and this class's
behavior is otherwise unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from ..analysis.sentiment import KeywordClassifier, RedditRecord, aggregate

MISSING_CREDENTIALS_REASON = "Reddit sentiment unavailable: REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET not configured"


@dataclass
class RedditSentimentResult:
    records: tuple[RedditRecord, ...]
    net_sentiment: float | None
    total_mentions: int
    unique_authors: int
    missing_data_reasons: tuple[str, ...]


class RedditSentimentSource:
    def __init__(
        self, *,
        credentials_configured: bool,
        fetch_records: Callable[[str, float, float], tuple[RedditRecord, ...]] | None = None,
    ):
        self.credentials_configured = credentials_configured
        self._fetch_records = fetch_records

    def fetch(self, symbol: str, as_of: datetime, window_seconds: float = 86_400.0) -> RedditSentimentResult:
        if not self.credentials_configured or self._fetch_records is None:
            return RedditSentimentResult(
                records=(), net_sentiment=None, total_mentions=0, unique_authors=0,
                missing_data_reasons=(MISSING_CREDENTIALS_REASON,),
            )
        window_end = as_of.timestamp()
        window_start = window_end - window_seconds
        records = self._fetch_records(symbol, window_start, window_end)
        if not records:
            return RedditSentimentResult(
                records=(), net_sentiment=None, total_mentions=0, unique_authors=0,
                missing_data_reasons=(f"no Reddit records retrieved for {symbol} in the requested window",),
            )
        agg = aggregate(list(records), symbol, window_start, window_end, classifier=KeywordClassifier())
        return RedditSentimentResult(
            records=tuple(records), net_sentiment=agg.net_sentiment,
            total_mentions=agg.total_mentions, unique_authors=agg.unique_authors, missing_data_reasons=(),
        )
