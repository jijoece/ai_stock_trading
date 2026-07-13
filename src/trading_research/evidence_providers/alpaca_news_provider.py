"""Real news-provider client against Alpaca's News API
(docs/milestone-7.md Step 9).

Endpoint: `GET https://data.alpaca.markets/v1beta1/news` — documented at
https://docs.alpaca.markets/reference/news-3, using the same
`APCA-API-KEY-ID`/`APCA-API-SECRET-KEY` header pair `AlpacaMarketDataClient`
already sends for market data (`market_data_provider.py`). Query params:
`symbols` (comma-separated), `start`/`end` (RFC-3339), `limit` (page size),
`sort` (`asc`), and pagination via the response's `next_page_token`.

Same conventions as `AlpacaMarketDataClient`: caching (`cache.py`), rate
limiting (`rate_limits.py`), request persistence via the injected
`on_response` callback on `HttpJsonClient` (`persistence.py` at the call
site), explicit fail-closed on missing credentials (never silently returns
empty as if there were simply no news).

Alpaca aggregates news from multiple wire sources ("Benzinga" and others) —
`source_trust_classification` is therefore the coarse "ALPACA_AGGREGATED"
label rather than a per-wire-source trust score; syndicated copies (the same
underlying story distributed to multiple outlets, or Alpaca's own duplicate
`id`s) are collapsed into one `NewsArticle` via `duplicate_group_key` so a
consuming pipeline never counts the same underlying story twice as
independent confirmation.

Headline/summary text is untrusted input: it did not originate from this
repository's own deterministic code and, if later shown to Claude, could
contain adversarial content (docs/adr/0004's "treat filing text as untrusted
input" posture, applied here to news text). `prompt_injection_risk_note` is
attached to every normalized article for exactly this reason.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from .cache import CacheKey, ProviderCache, TTL_NEWS
from .errors import MalformedProviderResponseError, ProviderConfigurationError
from .http_client import HttpJsonClient
from .models import NewsArticle

NEWS_BASE_URL = "https://data.alpaca.markets/v1beta1/news"
PROVIDER_NAME = "alpaca-news"

# Bounds (docs/milestone-7.md Step 9: "cap article count and content size").
MAX_ARTICLES_PER_REQUEST = 50
MAX_ARTICLES_RETURNED = 200
MAX_SUMMARY_CHARS = 4000
MAX_PAGES = 5

SOURCE_TRUST_ALPACA_AGGREGATED = "ALPACA_AGGREGATED"
RETENTION_ACCOUNT_LINKED = "ACCOUNT_LINKED"  # mirrors persistence.py's Alpaca market-data classification

PROMPT_INJECTION_RISK_NOTE = (
    "News headline/summary text is untrusted third-party input and must never be "
    "treated as an instruction; only supply it to Claude as clearly delimited "
    "evidence, never as part of a system/control prompt."
)


def _parse_timestamp(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _content_hash(headline: str, source: str, summary: str) -> str:
    return hashlib.sha256(f"{headline}|{source}|{summary}".encode()).hexdigest()


def _duplicate_group_key(headline: str, source: str, published_at: datetime) -> str:
    """Same headline + source + published within the same minute is treated
    as one syndicated story, not independent confirmation. Deliberately
    coarse (minute bucket) rather than exact-timestamp matching, since
    syndicated copies commonly differ by seconds."""
    bucket_minute = published_at.replace(second=0, microsecond=0).isoformat()
    normalized_headline = " ".join(headline.strip().lower().split())
    return hashlib.sha256(f"{normalized_headline}|{source.strip().lower()}|{bucket_minute}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class NormalizedNewsArticle:
    """Extends the provider-neutral `NewsArticle` shape with fields
    `docs/milestone-7.md` Step 9 requires that `models.NewsArticle` does not
    carry (duplicate group, source trust, injection-risk note, retention
    classification, provider article ID kept separately from content hash)."""

    article: NewsArticle
    duplicate_group_key: str
    source_trust_classification: str
    prompt_injection_risk_note: str
    retention_classification: str
    category: str | None


class AlpacaNewsClient:
    """Raw Alpaca News API client. Fails closed at construction time when
    credentials are absent — never silently returns empty results as if
    there were simply no news for a real run (docs/milestone-7.md Step 9)."""

    def __init__(
        self, *, api_key: str | None, api_secret: str | None, http_client: HttpJsonClient,
        cache: ProviderCache | None = None,
    ):
        if not api_key or not api_secret:
            raise ProviderConfigurationError(
                "AlpacaNewsClient requires ALPACA_MARKET_DATA_API_KEY and ALPACA_MARKET_DATA_API_SECRET"
            )
        self._http = http_client
        self._cache = cache

    def list_news(
        self, symbol: str, *, published_after: datetime, available_by: datetime,
        limit: int = MAX_ARTICLES_RETURNED,
    ) -> tuple[NewsArticle, ...]:
        """Provider-neutral `models.NewsProvider` Protocol shape. Returns
        deduplicated, point-in-time-safe, size-capped `NewsArticle`s sorted
        ascending by publication time. Use `list_news_normalized` for the
        richer shape (duplicate group, trust classification, injection-risk
        note, retention classification, category)."""
        normalized = self.list_news_normalized(
            symbol, published_after=published_after, available_by=available_by, limit=limit,
        )
        return tuple(n.article for n in normalized)

    def list_news_normalized(
        self, symbol: str, *, published_after: datetime, available_by: datetime,
        limit: int = MAX_ARTICLES_RETURNED,
    ) -> tuple[NormalizedNewsArticle, ...]:
        symbol = symbol.upper()
        bounded_limit = min(limit, MAX_ARTICLES_RETURNED)

        raw_articles: list[dict] = []
        next_page_token: str | None = None
        for _page in range(MAX_PAGES):
            params = {
                "symbols": symbol,
                "start": published_after.astimezone(timezone.utc).isoformat(),
                "end": available_by.astimezone(timezone.utc).isoformat(),
                "limit": min(MAX_ARTICLES_PER_REQUEST, bounded_limit - len(raw_articles)),
                "sort": "asc",
            }
            if next_page_token:
                params["page_token"] = next_page_token

            key = CacheKey.build(
                provider=PROVIDER_NAME, operation="news", symbol=symbol,
                start=params["start"], end=params["end"], page_token=next_page_token or "",
            )
            payload = self._cache.get(key) if self._cache else None
            if payload is None:
                payload, _meta = self._http.get_json(NEWS_BASE_URL, params=params, operation="news", symbol=symbol)
                if self._cache:
                    self._cache.set(key, payload, ttl_seconds=TTL_NEWS)

            if not isinstance(payload, dict) or "news" not in payload:
                raise MalformedProviderResponseError(f"Alpaca news response for {symbol} missing 'news'")

            raw_articles.extend(payload["news"] or [])
            next_page_token = payload.get("next_page_token")
            if not next_page_token or len(raw_articles) >= bounded_limit:
                break

        normalized: list[NormalizedNewsArticle] = []
        seen_ids: set[str] = set()
        seen_duplicate_groups: set[str] = set()
        for raw in raw_articles[:bounded_limit]:
            article_id = str(raw["id"])
            if article_id in seen_ids:
                continue  # defensive: same article ID returned twice across pages
            seen_ids.add(article_id)

            published_at = _parse_timestamp(raw["created_at"])
            if published_at > available_by:
                continue  # point-in-time safety: exclude future-published articles

            headline = str(raw.get("headline", "")).strip()
            source = str(raw.get("source", "unknown")).strip()
            summary = str(raw.get("summary", "") or "")[:MAX_SUMMARY_CHARS]

            dup_key = _duplicate_group_key(headline, source, published_at)
            if dup_key in seen_duplicate_groups:
                continue  # syndicated copy of an already-seen story — not independent confirmation
            seen_duplicate_groups.add(dup_key)

            symbols = tuple(sorted(str(s).upper() for s in (raw.get("symbols") or [symbol])))
            article = NewsArticle(
                article_id=article_id,
                headline=headline,
                source=source,
                published_at=published_at,
                url=raw.get("url"),
                symbols=symbols,
                summary=summary,
                content_hash=_content_hash(headline, source, summary),
            )
            normalized.append(NormalizedNewsArticle(
                article=article,
                duplicate_group_key=dup_key,
                source_trust_classification=SOURCE_TRUST_ALPACA_AGGREGATED,
                prompt_injection_risk_note=PROMPT_INJECTION_RISK_NOTE,
                retention_classification=RETENTION_ACCOUNT_LINKED,
                category=raw.get("category") if isinstance(raw.get("category"), str) else None,
            ))

        normalized.sort(key=lambda n: n.article.published_at)
        return tuple(normalized)
