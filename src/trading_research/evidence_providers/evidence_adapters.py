"""Adapters that satisfy `research/evidence.py`'s existing
`FundamentalsEvidenceProvider` / `MarketEvidenceProvider` / `NewsEvidenceProvider`
/ `SentimentEvidenceProvider` / `FilingEvidenceProvider` Protocols using the
real raw clients in this package (docs/milestone-6.md Steps 4-5).

Every adapter's `.fetch(symbol, as_of)` returns an `EvidenceBundle` and never
raises for an "expected" failure mode (provider unconfigured, no data,
transient error) — it converts those into `missing_data_reasons` instead, so
`build_evidence_snapshot` (which does not catch provider exceptions per its
own docstring) sees a normal empty/partial bundle rather than a crash. A
genuinely unexpected error (a bug, a malformed response the client itself
could not classify) is still allowed to propagate — "fail closed," not
"never fail."
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from ..research.evidence import EvidenceBundle
from ..research.models import EvidenceItem, SourceRecord
from .errors import ProviderConfigurationError, ProviderError
from .fundamentals import normalize_fundamentals
from .market_data_provider import AlpacaMarketDataClient
from .models import CompanyFactValue, FilingRecord
from .news_provider import UnconfiguredNewsProvider
from .sec_provider import SecEdgarClient
from .sentiment_provider import RedditSentimentSource


def _content_hash(*parts: object) -> str:
    return hashlib.sha256(repr(parts).encode()).hexdigest()


class RealFundamentalsEvidenceProvider:
    """Derives fundamentals evidence from real SEC company facts
    (docs/milestone-6.md Step 8: "derive fundamentals from official SEC
    company facts where sufficient")."""

    def __init__(self, sec_client: SecEdgarClient):
        self._sec = sec_client

    def fetch(self, symbol: str, as_of: datetime) -> EvidenceBundle:
        try:
            facts: tuple[CompanyFactValue, ...] = self._sec.get_company_facts(symbol, as_of=as_of)
        except ProviderError as exc:
            source = SourceRecord(
                source_id=f"sec-companyfacts-{symbol}", source_type="fundamentals", provider="sec-edgar",
                source_locator=None, retrieved_at=as_of, published_at=None, effective_at=None,
                available_at=None, content_hash=_content_hash(symbol, "error"), status="error",
                is_stale=False, point_in_time_safe=True, error_code=type(exc).__name__,
            )
            return EvidenceBundle(source_records=(source,), missing_data_reasons=(f"SEC company facts unavailable for {symbol}: {exc}",))

        if not facts:
            source = SourceRecord(
                source_id=f"sec-companyfacts-{symbol}", source_type="fundamentals", provider="sec-edgar",
                source_locator=None, retrieved_at=as_of, published_at=None, effective_at=None,
                available_at=None, content_hash=_content_hash(symbol, "empty"), status="missing",
                is_stale=False, point_in_time_safe=True, error_code="no_company_facts",
            )
            return EvidenceBundle(source_records=(source,), missing_data_reasons=(f"no SEC company facts available for {symbol}",))

        normalized = normalize_fundamentals(facts)
        if not normalized:
            return EvidenceBundle(missing_data_reasons=(f"SEC company facts present but no known concept matched for {symbol}",))

        items = []
        sources = []
        seen_sources: set[str] = set()
        for field_name, nf in sorted(normalized.items()):
            filed_at_dt = datetime(nf.filed_at.year, nf.filed_at.month, nf.filed_at.day, tzinfo=as_of.tzinfo)
            period_end_dt = datetime(nf.period_end.year, nf.period_end.month, nf.period_end.day, tzinfo=as_of.tzinfo)
            source_id = f"sec-companyfacts-{symbol}-{nf.concept}-{nf.period_end.isoformat()}"
            if source_id not in seen_sources:
                seen_sources.add(source_id)
                sources.append(SourceRecord(
                    source_id=source_id, source_type="fundamentals", provider="sec-edgar",
                    source_locator=f"https://data.sec.gov/api/xbrl/companyfacts (concept={nf.concept})",
                    retrieved_at=as_of, published_at=period_end_dt, effective_at=period_end_dt,
                    available_at=filed_at_dt, content_hash=_content_hash(nf.concept, str(nf.value), nf.filed_at),
                    status="ok", is_stale=False, point_in_time_safe=filed_at_dt <= as_of, error_code=None,
                    metadata={"form_type": nf.form_type, "fiscal_period": nf.fiscal_period or ""},
                ))
            items.append(EvidenceItem(
                evidence_id=f"{source_id}-{field_name}", source_id=source_id, category="fundamentals",
                title=f"{symbol} {field_name.replace('_', ' ')}", summary=f"{field_name}={nf.value} ({nf.unit}, period ending {nf.period_end})",
                normalized_values={field_name: float(nf.value)}, as_of=period_end_dt, confidence="high",
                stale=False, conflict_group=None,
            ))
        return EvidenceBundle(source_records=tuple(sources), evidence_items=tuple(items))


class RealFilingEvidenceProvider:
    def __init__(self, sec_client: SecEdgarClient, *, form_types: tuple[str, ...] = ("8-K", "10-K", "10-Q")):
        self._sec = sec_client
        self._form_types = form_types

    def fetch(self, symbol: str, as_of: datetime) -> EvidenceBundle:
        try:
            filings: tuple[FilingRecord, ...] = self._sec.list_filings(symbol, available_by=as_of)
        except ProviderError as exc:
            return EvidenceBundle(missing_data_reasons=(f"SEC filings unavailable for {symbol}: {exc}",))

        relevant = [f for f in filings if f.form_type in self._form_types]
        if not relevant:
            return EvidenceBundle(missing_data_reasons=(f"no recent {self._form_types} filings for {symbol} as of {as_of.isoformat()}",))

        relevant = sorted(relevant, key=lambda f: f.accepted_at, reverse=True)[:10]
        sources, items = [], []
        for f in relevant:
            source_id = f"sec-filing-{f.accession_number}"
            sources.append(SourceRecord(
                source_id=source_id, source_type="filing", provider="sec-edgar",
                source_locator=f.primary_document_url, retrieved_at=as_of,
                published_at=f.accepted_at, effective_at=f.accepted_at, available_at=f.accepted_at,
                content_hash=_content_hash(f.accession_number), status="ok", is_stale=False,
                point_in_time_safe=f.accepted_at <= as_of, error_code=None,
                metadata={"form_type": f.form_type, "is_amendment": f.is_amendment},
            ))
            items.append(EvidenceItem(
                evidence_id=f"{source_id}-1", source_id=source_id, category="filing",
                title=f"{symbol} {f.form_type} filed {f.filing_date.isoformat()}",
                summary=f"{f.form_type} accepted {f.accepted_at.isoformat()}, report period {f.report_period}",
                normalized_values={}, as_of=f.accepted_at, confidence="high", stale=False, conflict_group=None,
            ))
        return EvidenceBundle(source_records=tuple(sources), evidence_items=tuple(items))


class RealMarketEvidenceProvider:
    """Recent-price evidence for the research committee (distinct from the
    deterministic screener/scorer's own market data, which is supplied
    separately by `deterministic_factors` — this only feeds the *research*
    evidence snapshot, per docs/milestone-6.md Step 7)."""

    def __init__(self, client: AlpacaMarketDataClient, *, lookback_days: int = 10):
        self._client = client
        self._lookback_days = lookback_days

    def fetch(self, symbol: str, as_of: datetime) -> EvidenceBundle:
        start = (as_of - timedelta(days=self._lookback_days * 2)).date()  # buffer for weekends/holidays
        end = as_of.date()
        try:
            bars = self._client.get_price_history(symbol, start=start, end=end, as_of=as_of)
        except ProviderError as exc:
            return EvidenceBundle(missing_data_reasons=(f"market data unavailable for {symbol}: {exc}",))

        if not bars:
            return EvidenceBundle(missing_data_reasons=(f"no historical bars for {symbol} as of {as_of.isoformat()}",))

        recent = bars[-self._lookback_days:]
        latest = recent[-1]
        source_id = f"alpaca-bars-{symbol}-{latest.session_date.isoformat()}"
        available_at = datetime(latest.session_date.year, latest.session_date.month, latest.session_date.day, 21, 0, tzinfo=as_of.tzinfo)
        source = SourceRecord(
            source_id=source_id, source_type="market", provider="alpaca-data", source_locator=None,
            retrieved_at=as_of, published_at=available_at, effective_at=available_at, available_at=available_at,
            content_hash=_content_hash(symbol, str(latest.close), latest.session_date.isoformat()),
            status="ok", is_stale=(as_of.date() - latest.session_date).days > 5,
            point_in_time_safe=available_at <= as_of, error_code=None,
            metadata={"adjusted": latest.adjusted, "sessions_returned": len(recent)},
        )
        item = EvidenceItem(
            evidence_id=f"{source_id}-close", source_id=source_id, category="market",
            title=f"{symbol} close on {latest.session_date.isoformat()}",
            summary=f"close={latest.close} volume={latest.volume} over {len(recent)} recent session(s)",
            normalized_values={
                "latest_close": float(latest.close), "latest_volume": latest.volume,
                "session_count": len(recent),
                "period_return": float((latest.close - recent[0].close) / recent[0].close) if recent[0].close else 0.0,
            },
            as_of=available_at, confidence="high", stale=source.is_stale, conflict_group=None,
        )
        return EvidenceBundle(source_records=(source,), evidence_items=(item,))


class RealNewsEvidenceProvider:
    def __init__(self, client: UnconfiguredNewsProvider | object):
        self._client = client

    def fetch(self, symbol: str, as_of: datetime) -> EvidenceBundle:
        try:
            articles = self._client.list_news(symbol, published_after=as_of - timedelta(days=7), available_by=as_of)
        except ProviderConfigurationError as exc:
            return EvidenceBundle(missing_data_reasons=(str(exc),))
        except ProviderError as exc:
            return EvidenceBundle(missing_data_reasons=(f"news provider failed for {symbol}: {exc}",))

        if not articles:
            return EvidenceBundle(missing_data_reasons=(f"no news articles for {symbol} in the requested window",))

        sources, items = [], []
        for article in articles:
            if article.published_at > as_of:
                continue  # exclude future-published articles — no look-ahead
            source_id = f"news-{article.article_id}"
            sources.append(SourceRecord(
                source_id=source_id, source_type="news", provider=article.source,
                source_locator=article.url, retrieved_at=as_of, published_at=article.published_at,
                effective_at=article.published_at, available_at=article.published_at,
                content_hash=article.content_hash, status="ok", is_stale=False,
                point_in_time_safe=article.published_at <= as_of, error_code=None,
            ))
            items.append(EvidenceItem(
                evidence_id=f"{source_id}-1", source_id=source_id, category="news", title=article.headline,
                summary=article.summary, normalized_values={}, as_of=article.published_at,
                confidence="medium", stale=False, conflict_group=None,
            ))
        if not sources:
            return EvidenceBundle(missing_data_reasons=(f"all retrieved news articles for {symbol} were future-published relative to as_of",))
        return EvidenceBundle(source_records=tuple(sources), evidence_items=tuple(items))


class RealSentimentEvidenceProvider:
    def __init__(self, source: RedditSentimentSource):
        self._source = source

    def fetch(self, symbol: str, as_of: datetime) -> EvidenceBundle:
        result = self._source.fetch(symbol, as_of)
        source_id = f"reddit-sentiment-{symbol}-{as_of.date().isoformat()}"
        record = SourceRecord(
            source_id=source_id, source_type="sentiment", provider="reddit-mcp", source_locator=None,
            retrieved_at=as_of, published_at=as_of, effective_at=as_of, available_at=as_of,
            content_hash=_content_hash(symbol, result.net_sentiment, result.total_mentions),
            status="ok" if not result.missing_data_reasons else "missing", is_stale=False,
            point_in_time_safe=True, error_code=None if not result.missing_data_reasons else "reddit_unavailable",
        )
        if result.missing_data_reasons:
            return EvidenceBundle(source_records=(record,), missing_data_reasons=result.missing_data_reasons)
        item = EvidenceItem(
            evidence_id=f"{source_id}-1", source_id=source_id, category="sentiment",
            title=f"{symbol} Reddit sentiment", summary=f"net_sentiment={result.net_sentiment:+.2f} over {result.total_mentions} mentions",
            normalized_values={
                "net_sentiment": result.net_sentiment, "total_mentions": result.total_mentions,
                "unique_authors": result.unique_authors,
            },
            as_of=as_of, confidence="low", stale=False, conflict_group=None,
        )
        return EvidenceBundle(source_records=(record,), evidence_items=(item,))
