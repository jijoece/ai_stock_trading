"""Credential-free Reddit sentiment from public Atom search feeds.

The provider sends no cookies, authorization headers, API keys, or query
credentials. Reddit text is untrusted evidence and is never interpreted as an
instruction. Network/cache/storage failures fail closed to an empty result.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

import httpx
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from ..analysis.sentiment import BEARISH, BULLISH, NEUTRAL, Classification, RedditRecord
from ..config import REPO_ROOT
from .config import RedditFreeProviderConfig
from .rate_limits import MinIntervalRateLimiter

log = logging.getLogger(__name__)

PROVIDER_NAME = "reddit-free-rss"
RSS_URL_TEMPLATE = "https://www.reddit.com/r/{subreddit}/search.rss"
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_CACHE_AGE_SECONDS = 24 * 60 * 60
ATOM = "{http://www.w3.org/2005/Atom}"
_SAFE_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,14}\Z")
_SAFE_SUBREDDIT = re.compile(r"[A-Za-z0-9_]{1,30}\Z")


@dataclass(frozen=True)
class RedditFreePost:
    reddit_post_id: str
    symbol: str
    subreddit: str
    author: str
    title: str
    body: str
    score: int
    num_comments: int
    created_utc: float
    permalink: str | None
    source_endpoint: str
    fetch_timestamp: str
    sentiment_compound: float

    def to_record(self) -> RedditRecord:
        label = BULLISH if self.sentiment_compound >= 0.05 else BEARISH if self.sentiment_compound <= -0.05 else NEUTRAL
        return RedditRecord(
            record_id=self.reddit_post_id,
            record_type="post",
            symbol=self.symbol,
            author=self.author,
            subreddit=self.subreddit,
            created_utc=self.created_utc,
            text="\n".join(value for value in (self.title, self.body) if value),
            engagement=self.score + self.num_comments,
            is_cashtag=f"${self.symbol}" in f"{self.title} {self.body}".upper(),
            context_confirmed=True,
            link_url=self.permalink,
            classification=Classification(label=label, confidence="low"),
        )


@dataclass(frozen=True)
class RedditFreeResult:
    posts: tuple[RedditFreePost, ...]
    records: tuple[RedditRecord, ...]
    average_sentiment: float | None
    net_sentiment: float | None
    total_mentions: int
    unique_authors: int
    missing_data_reasons: tuple[str, ...]

    @classmethod
    def empty(cls, reason: str) -> "RedditFreeResult":
        return cls((), (), None, None, 0, 0, (reason,))


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if value:
            self.parts.append(value)


class _ProviderDisabled(RuntimeError):
    pass


class RedditFreeProvider:
    """Fetch, score, cache, and optionally persist public Reddit RSS posts."""

    provider_name = PROVIDER_NAME
    source_locator = "https://www.reddit.com/search.rss"

    def __init__(
        self,
        config: RedditFreeProviderConfig,
        *,
        conn: sqlite3.Connection | None = None,
        data_dir: str | Path | None = None,
        transport: httpx.BaseTransport | None = None,
        analyzer: SentimentIntensityAnalyzer | None = None,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 1 <= config.max_attempts <= 3 or config.max_posts_per_symbol < 1 or config.cache_ttl_minutes < 1:
            raise ValueError("reddit_free attempts, post limit, and cache TTL must be positive")
        if not 1 <= config.max_requests_per_endpoint_hour <= 30:
            raise ValueError("reddit_free hourly endpoint limit must be in [1, 30]")
        self._config = config
        self._conn = conn
        root = Path(data_dir) if data_dir is not None else Path(os.environ.get("RESEARCH_DATA_DIR", REPO_ROOT / "data"))
        self._cache_dir = root / "reddit_free_cache"
        self._state_path = self._cache_dir / "rate_limit_state.json"
        self._transport = transport
        self._client: httpx.Client | None = None
        self._analyzer = analyzer or SentimentIntensityAnalyzer()
        self._wall_clock = wall_clock
        self._sleep_fn = sleep_fn
        self._limiter = MinIntervalRateLimiter(
            max(2.0, config.min_request_interval_seconds), clock=monotonic_clock, sleep_fn=sleep_fn
        )
        self._state_lock = threading.Lock()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "RedditFreeProvider":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def fetch(self, symbol: str, as_of: datetime) -> RedditFreeResult:
        symbol = symbol.upper().strip()
        if not self._config.enabled:
            return RedditFreeResult.empty("credential-free Reddit provider is disabled by configuration")
        if not _SAFE_SYMBOL.fullmatch(symbol):
            return RedditFreeResult.empty(f"invalid symbol for credential-free Reddit provider: {symbol!r}")
        if as_of.tzinfo is None:
            return RedditFreeResult.empty("credential-free Reddit provider requires a timezone-aware as_of")

        try:
            self._raise_if_disabled()
            posts = self._load_cache(symbol, as_of)
            if posts is None:
                posts = self._fetch_posts(symbol, as_of)
                if posts:
                    self._save_cache(symbol, as_of, posts)
            if not posts:
                return RedditFreeResult.empty(f"no recent Reddit RSS posts retrieved for {symbol}")
            self._persist(posts)
            records = tuple(post.to_record() for post in posts)
            average = round(sum(post.sentiment_compound for post in posts) / len(posts), 4)
            return RedditFreeResult(
                posts=posts,
                records=records,
                average_sentiment=average,
                net_sentiment=average,
                total_mentions=len(posts),
                unique_authors=len({post.author for post in posts}),
                missing_data_reasons=(),
            )
        except Exception as exc:
            log.warning(
                "credential-free Reddit provider unavailable",
                extra={"provider": PROVIDER_NAME, "symbol": symbol, "error_code": type(exc).__name__},
            )
            return RedditFreeResult.empty(f"credential-free Reddit unavailable for {symbol}: {type(exc).__name__}")

    def _client_instance(self) -> httpx.Client:
        if self._client is None:
            # Deliberately no auth/cookie headers and no environment-derived credentials.
            self._client = httpx.Client(
                headers={"User-Agent": self._config.user_agent, "Accept": "application/atom+xml"},
                timeout=self._config.request_timeout_seconds,
                follow_redirects=True,
                trust_env=False,
                transport=self._transport,
            )
        return self._client

    def _fetch_posts(self, symbol: str, as_of: datetime) -> tuple[RedditFreePost, ...]:
        results: list[RedditFreePost] = []
        seen: set[str] = set()
        valid_subreddits: list[str] = []
        for subreddit in self._config.subreddits:
            if not _SAFE_SUBREDDIT.fullmatch(subreddit):
                log.warning("skipping invalid configured subreddit", extra={"provider": PROVIDER_NAME})
                continue
            valid_subreddits.append(subreddit)
        if not valid_subreddits:
            return ()

        # Reddit supports a combined r/one+two+three feed. One request per
        # symbol is materially more reliable than sequential subreddit calls
        # (the latter began returning 429 during the live investigation even
        # with the required two-second pacing).
        endpoint = RSS_URL_TEMPLATE.format(subreddit="+".join(valid_subreddits))
        body = self._get_rss(
            endpoint,
            params={
                "q": symbol,
                "restrict_sr": "1",
                "sort": "new",
                "limit": min(25, self._config.max_posts_per_symbol),
            },
        )
        for post in self._parse_rss(body, symbol, "", endpoint, as_of):
            if post.reddit_post_id in seen:
                continue
            seen.add(post.reddit_post_id)
            results.append(post)
            if len(results) >= self._config.max_posts_per_symbol:
                break
        return tuple(sorted(results, key=lambda post: post.created_utc, reverse=True))

    def _get_rss(self, endpoint: str, *, params: dict[str, object]) -> bytes:
        last_error: Exception | None = None
        blocked_status: int | None = None
        for attempt in range(1, self._config.max_attempts + 1):
            try:
                self._reserve_request(endpoint)
                with self._client_instance().stream("GET", endpoint, params=params) as response:
                    if response.url.scheme != "https" or not (response.url.host or "").endswith("reddit.com"):
                        raise RuntimeError("Reddit RSS redirected outside the allowed HTTPS reddit.com boundary")
                    body = self._read_bounded(response)
                    status = response.status_code
                if status in {403, 429}:
                    blocked_status = status
                    last_error = RuntimeError(f"endpoint returned blocking status {status}")
                elif status >= 500:
                    last_error = RuntimeError(f"endpoint returned retryable status {status}")
                elif status >= 400:
                    raise RuntimeError(f"endpoint returned status {status}")
                elif not body:
                    raise ValueError("endpoint returned an empty body")
                else:
                    return body
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc

            if attempt < self._config.max_attempts:
                self._sleep_fn(min(float(2 ** (attempt - 1)), 8.0))

        if blocked_status is not None:
            self._disable_until_next_day(blocked_status)
        raise RuntimeError(f"RSS request exhausted {self._config.max_attempts} attempts: {type(last_error).__name__}")

    @staticmethod
    def _read_bounded(response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                response.close()
                raise ValueError(f"Reddit RSS response exceeded {MAX_RESPONSE_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    def _parse_rss(
        self, body: bytes, symbol: str, subreddit: str, endpoint: str, as_of: datetime
    ) -> tuple[RedditFreePost, ...]:
        root = ET.fromstring(body)
        posts: list[RedditFreePost] = []
        cutoff = as_of.timestamp() - MAX_CACHE_AGE_SECONDS
        mention = re.compile(rf"(?<![A-Z0-9])\$?{re.escape(symbol)}(?![A-Z0-9])", re.IGNORECASE)
        fetched_at = datetime.fromtimestamp(self._wall_clock(), tz=timezone.utc).isoformat()
        for entry in root.findall(f"{ATOM}entry"):
            raw_id = (entry.findtext(f"{ATOM}id") or "").strip()
            title = html.unescape((entry.findtext(f"{ATOM}title") or "").strip())
            raw_content = entry.findtext(f"{ATOM}content") or ""
            extractor = _TextExtractor()
            extractor.feed(raw_content)
            body_text = html.unescape(" ".join(extractor.parts))
            published = entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated")
            if not raw_id or not published or not mention.search(f"{title} {body_text}"):
                continue
            try:
                created_utc = datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if not cutoff <= created_utc <= as_of.timestamp():
                continue
            author = entry.findtext(f"{ATOM}author/{ATOM}name") or "[unknown]"
            category_node = entry.find(f"{ATOM}category")
            entry_subreddit = (
                category_node.get("term") if category_node is not None and category_node.get("term") else subreddit
            )
            link_node = entry.find(f"{ATOM}link")
            permalink = link_node.get("href") if link_node is not None else None
            sentiment_text = f"{title} {body_text}"[:512]
            compound = round(float(self._analyzer.polarity_scores(sentiment_text)["compound"]), 4)
            posts.append(
                RedditFreePost(
                    reddit_post_id=raw_id,
                    symbol=symbol,
                    subreddit=entry_subreddit,
                    author=author,
                    title=title,
                    body=body_text,
                    score=0,
                    num_comments=0,
                    created_utc=created_utc,
                    permalink=permalink,
                    source_endpoint=endpoint,
                    fetch_timestamp=fetched_at,
                    sentiment_compound=compound,
                )
            )
        return tuple(posts)

    def _cache_path(self, symbol: str, as_of: datetime) -> Path:
        return self._cache_dir / f"symbol_{symbol}_{as_of.date().isoformat()}.json"

    def _load_cache(self, symbol: str, as_of: datetime) -> tuple[RedditFreePost, ...] | None:
        path = self._cache_path(symbol, as_of)
        try:
            payload = json.loads(path.read_text())
            age = self._wall_clock() - float(payload["stored_at"])
            ttl = min(self._config.cache_ttl_minutes * 60, MAX_CACHE_AGE_SECONDS)
            if age < 0 or age > ttl:
                return None
            return tuple(RedditFreePost(**post) for post in payload["posts"])
        except FileNotFoundError:
            return None
        except Exception as exc:
            log.warning(
                "ignoring invalid credential-free Reddit cache",
                extra={"provider": PROVIDER_NAME, "symbol": symbol, "error_code": type(exc).__name__},
            )
            return None

    def _save_cache(self, symbol: str, as_of: datetime, posts: tuple[RedditFreePost, ...]) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {"stored_at": self._wall_clock(), "posts": [asdict(post) for post in posts]}
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=self._cache_dir, prefix=".reddit-", suffix=".tmp", delete=False) as handle:
                temp_name = handle.name
                json.dump(payload, handle, sort_keys=True)
            os.replace(temp_name, self._cache_path(symbol, as_of))
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)

    def _read_state(self) -> dict:
        try:
            payload = json.loads(self._state_path.read_text())
            return payload if isinstance(payload, dict) else {}
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _write_state(self, state: dict) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=self._cache_dir, prefix=".rate-", suffix=".tmp", delete=False) as handle:
                temp_name = handle.name
                json.dump(state, handle, sort_keys=True)
            os.replace(temp_name, self._state_path)
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)

    def _raise_if_disabled(self) -> None:
        state = self._read_state()
        raw = state.get("disabled_until")
        if raw and self._wall_clock() < float(raw):
            raise _ProviderDisabled("provider is disabled after a 403/429 until the next UTC day")

    def _reserve_request(self, endpoint: str) -> None:
        self._raise_if_disabled()
        self._limiter.acquire()
        now = self._wall_clock()
        with self._state_lock:
            state = self._read_state()
            requests = state.setdefault("request_times", {})
            recent = [float(value) for value in requests.get(endpoint, []) if now - float(value) < 3600]
            if len(recent) >= self._config.max_requests_per_endpoint_hour:
                raise _ProviderDisabled("persistent per-endpoint hourly request limit reached")
            recent.append(now)
            requests[endpoint] = recent
            self._write_state(state)

    def _disable_until_next_day(self, status: int) -> None:
        now = datetime.fromtimestamp(self._wall_clock(), tz=timezone.utc)
        tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        with self._state_lock:
            state = self._read_state()
            state["disabled_until"] = tomorrow.timestamp()
            state["disabled_reason"] = f"HTTP {status}"
            self._write_state(state)
        log.warning(
            "credential-free Reddit provider disabled until next UTC day after blocking response",
            extra={"provider": PROVIDER_NAME, "http_status": status, "disabled_until": tomorrow.isoformat()},
        )

    def _persist(self, posts: tuple[RedditFreePost, ...]) -> None:
        if self._conn is None:
            return
        for post in posts:
            self._conn.execute(
                "INSERT OR IGNORE INTO reddit_posts "
                "(id, reddit_post_id, symbol, subreddit, author, created_utc, score, num_comments, title, text, body, url, "
                "injection_risk, retrieved_at, source_endpoint, fetch_timestamp, sentiment_compound) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'none', ?, ?, ?, ?)",
                (
                    f"{post.symbol}:{post.reddit_post_id}",
                    post.reddit_post_id,
                    post.symbol,
                    post.subreddit,
                    post.author,
                    post.created_utc,
                    post.score,
                    post.num_comments,
                    post.title,
                    post.body,
                    post.body,
                    post.permalink,
                    post.fetch_timestamp,
                    post.source_endpoint,
                    post.fetch_timestamp,
                    post.sentiment_compound,
                ),
            )
        self._conn.commit()
