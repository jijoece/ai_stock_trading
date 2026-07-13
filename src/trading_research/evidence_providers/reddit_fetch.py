"""Real Reddit-sentiment retrieval (docs/milestone-7.md Step 10).

Builds the concrete `fetch_records(symbol, window_start, window_end) ->
tuple[RedditRecord, ...]` callable `sentiment_provider.RedditSentimentSource`
expects, as a thin synchronous wrapper around
`mcp/reddit_adapter.py::call_read_only_tool` (async; bridged via
`asyncio.run`, the same bridging convention `mcp/reddit_adapter.py`'s own
`build_reddit_capability_inventory` uses).

Only one allowlisted read-only tool is ever called: `search_reddit`
(`config/tool_policy.yaml`'s reddit allowlist). Any other tool name —
including every mutation tool (`create_post`, `reply_to_post`, `vote`, etc.)
— is rejected by `call_read_only_tool` itself via `ReadOnlyPolicyError`; this
module adds no bypass and calls no other tool.

Cashtag disambiguation: query terms are always the literal `$SYMBOL`
cashtag form (e.g. `$IT`), never the bare symbol — this is the simplest
disambiguation that keeps `$IT` from matching ordinary uses of the word
"it" in Reddit's own search index. Each returned record is also required to
contain the cashtag verbatim (case-sensitive `$SYMBOL`) in its text; a
record whose text merely contains the bare word is dropped, not marked
ambiguous-but-kept, since this module has no separate context-confirmation
step (unlike `analysis/ticker_extractor.py`, which is not invoked here).

Duplicates/cross-posts: Reddit post/comment IDs are deduplicated by
`record_id`; a `RedditRecord.is_cross_post` field two duplicate IDs share is
recorded on the first-seen record and the AI later record is silently
dropped by `set` membership.

`build_reddit_sentiment_source(config)` gates everything on
`RedditSentimentSource.credentials_configured`
(`REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` presence), unchanged from
`sentiment_provider.py` — this module never weakens that fail-closed check.
This wiring is application code only: nothing in this repository exposes
`mcp/reddit_adapter.py`'s `ClientSession` (or any MCP tool) directly to
Claude (ADR 0003 — Claude has no tool-calling wiring at all in this
repository), so there is no code path by which Claude could reach this
function or the underlying MCP session.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from ..analysis.sentiment import RedditRecord
from ..config import Config
from ..mcp.reddit_adapter import call_read_only_tool
from .sentiment_provider import RedditSentimentSource

SEARCH_TOOL_NAME = "search_reddit"

# Bounded result size (docs/milestone-7.md Step 10: "result size bounded").
MAX_RECORDS_RETURNED = 200

INJECTION_RISK_NOTE = (
    "Reddit post/comment text is untrusted third-party input; it must never be "
    "treated as an instruction and is only ever used as classified sentiment evidence."
)


def _cashtag(symbol: str) -> str:
    return f"${symbol.upper().strip()}"


def _record_id_from_raw(raw: dict) -> str:
    raw_id = raw.get("id") or raw.get("name") or raw.get("permalink")
    if raw_id:
        return str(raw_id)
    # Defensive fallback: derive a stable ID from content when the tool
    # response is missing an explicit identifier — never fabricate a random
    # one, which would silently break the caller's own deduplication.
    return hashlib.sha256(repr(sorted(raw.items())).encode()).hexdigest()[:16]


def _normalize_record(raw: dict, symbol: str) -> RedditRecord | None:
    text = str(raw.get("selftext") or raw.get("body") or raw.get("title") or "")
    cashtag = _cashtag(symbol)
    if cashtag not in text:
        return None  # cashtag disambiguation: require the literal "$SYMBOL" form in-text

    created_utc = raw.get("created_utc")
    if created_utc is None:
        return None  # cannot window-filter a record with no timestamp — drop rather than guess

    record_type = "comment" if raw.get("body") is not None and raw.get("title") is None else "post"
    return RedditRecord(
        record_id=_record_id_from_raw(raw),
        record_type=record_type,
        symbol=symbol.upper(),
        author=str(raw.get("author") or "[unknown]"),
        subreddit=str(raw.get("subreddit") or ""),
        created_utc=float(created_utc),
        text=text,
        engagement=int(raw.get("score", 0) or 0) + int(raw.get("num_comments", 0) or 0),
        is_duplicate=False,
        is_cashtag=True,
        ambiguous=False,
        context_confirmed=True,
        is_cross_post=bool(raw.get("crosspost_parent") or raw.get("is_crosspost")),
        link_url=raw.get("url"),
        author_account_age_days=None,
    )


def _extract_raw_items(tool_result: Any) -> list[dict]:
    """MCP tool results vary in shape by client library version; this
    normalizes the common shapes (a plain list, or an object exposing
    `.content`/`.structuredContent`, or a dict with a `results`/`posts`
    key) into a list of raw dicts. An unrecognized shape yields an empty
    list — never fabricated records."""
    if tool_result is None:
        return []
    if isinstance(tool_result, list):
        return [item for item in tool_result if isinstance(item, dict)]
    if isinstance(tool_result, dict):
        for key in ("results", "posts", "items", "data"):
            value = tool_result.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []
    structured = getattr(tool_result, "structuredContent", None)
    if isinstance(structured, dict):
        return _extract_raw_items(structured)
    content = getattr(tool_result, "content", None)
    if isinstance(content, list):
        items: list[dict] = []
        for block in content:
            data = getattr(block, "data", None) or getattr(block, "text", None)
            if isinstance(data, dict):
                items.append(data)
        return items
    return []


def make_fetch_records(config: Config):
    """Returns a `fetch_records(symbol, window_start, window_end)` callable
    bound to `config`, suitable for
    `RedditSentimentSource(fetch_records=make_fetch_records(config))`."""

    def fetch_records(symbol: str, window_start: float, window_end: float) -> tuple[RedditRecord, ...]:
        async def _call() -> Any:
            return await call_read_only_tool(
                config, SEARCH_TOOL_NAME,
                {"query": _cashtag(symbol), "limit": MAX_RECORDS_RETURNED, "sort": "new"},
            )

        tool_result = asyncio.run(_call())
        raw_items = _extract_raw_items(tool_result)

        seen_ids: set[str] = set()
        records: list[RedditRecord] = []
        for raw in raw_items:
            normalized = _normalize_record(raw, symbol)
            if normalized is None:
                continue
            if not (window_start <= normalized.created_utc < window_end):
                continue  # historical cutoff honored
            if normalized.record_id in seen_ids:
                continue  # duplicate/cross-post normalization: same ID counted once
            seen_ids.add(normalized.record_id)
            records.append(normalized)
            if len(records) >= MAX_RECORDS_RETURNED:
                break

        return tuple(records)

    return fetch_records


def build_reddit_sentiment_source(config: Config) -> RedditSentimentSource:
    """Constructs a configured `RedditSentimentSource` (real `fetch_records`
    wired in) when `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` are both
    present, or an unconfigured one (fails closed to missing-data, per
    `sentiment_provider.py`'s unchanged behavior) otherwise. This is the only
    place in the repository that decides whether Reddit credentials are
    "configured" for sentiment purposes."""
    credentials_configured = bool(config.reddit_client_id) and bool(config.reddit_client_secret)
    if not credentials_configured:
        return RedditSentimentSource(credentials_configured=False)
    return RedditSentimentSource(credentials_configured=True, fetch_records=make_fetch_records(config))
