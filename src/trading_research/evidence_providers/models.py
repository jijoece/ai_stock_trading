"""Raw, provider-neutral data shapes returned by the low-level HTTP clients in
this package (docs/milestone-6.md Step 4). These sit *underneath*
`research/evidence.py`'s `EvidenceBundle`-returning Protocols: a raw client
(e.g. `sec_provider.SecEdgarClient`) returns these dataclasses, and a thin
adapter in `evidence_adapters.py` converts them into `SourceRecord`/
`EvidenceItem` objects. Nothing here is provider-specific (no SEC/Alpaca
response shapes leak past the client that produced them).

All datetimes are timezone-aware; every raw record carries its own
`available_at` (or an unambiguous field standing in for it) so downstream
point-in-time filtering is possible without re-deriving it from a raw
provider payload.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Mapping, Protocol


def _require_tz_aware(value: datetime | None, name: str) -> None:
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True)
class Quote:
    symbol: str
    as_of: datetime
    price: Decimal
    bid: Decimal | None
    ask: Decimal | None
    provider: str
    provider_timestamp: datetime

    def __post_init__(self) -> None:
        _require_tz_aware(self.as_of, "Quote.as_of")
        _require_tz_aware(self.provider_timestamp, "Quote.provider_timestamp")


@dataclass(frozen=True)
class PriceBar:
    symbol: str
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    adjusted: bool
    provider: str

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ValueError(f"PriceBar({self.symbol}, {self.session_date}): high < low")
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"PriceBar({self.symbol}, {self.session_date}): open outside [low, high]")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"PriceBar({self.symbol}, {self.session_date}): close outside [low, high]")
        if self.volume < 0:
            raise ValueError(f"PriceBar({self.symbol}, {self.session_date}): negative volume")


@dataclass(frozen=True)
class FilingRecord:
    accession_number: str
    cik: str
    symbol: str
    form_type: str
    filing_date: date
    accepted_at: datetime
    report_period: date | None
    primary_document_url: str | None
    is_amendment: bool

    def __post_init__(self) -> None:
        _require_tz_aware(self.accepted_at, "FilingRecord.accepted_at")


@dataclass(frozen=True)
class CompanyFactValue:
    concept: str
    unit: str
    value: Decimal
    period_start: date | None
    period_end: date
    fiscal_year: int | None
    fiscal_period: str | None
    form_type: str
    filed_at: date
    frame: str | None
    accession_number: str | None = None


@dataclass(frozen=True)
class NewsArticle:
    article_id: str
    headline: str
    source: str
    published_at: datetime
    url: str | None
    symbols: tuple[str, ...]
    summary: str
    content_hash: str

    def __post_init__(self) -> None:
        _require_tz_aware(self.published_at, "NewsArticle.published_at")


class MarketDataProvider(Protocol):
    """Raw market-data client (docs/milestone-6.md Step 4 suggested contract)."""

    def get_quote(self, symbol: str, *, as_of: datetime) -> Quote | None: ...

    def get_price_history(
        self, symbol: str, *, start: date, end: date, as_of: datetime
    ) -> tuple[PriceBar, ...]: ...


class FundamentalsProvider(Protocol):
    def get_company_facts(self, symbol: str, *, as_of: datetime) -> tuple[CompanyFactValue, ...]: ...


class FilingProvider(Protocol):
    def list_filings(self, symbol: str, *, available_by: datetime) -> tuple[FilingRecord, ...]: ...


class NewsProvider(Protocol):
    def list_news(
        self, symbol: str, *, published_after: datetime, available_by: datetime
    ) -> tuple[NewsArticle, ...]: ...
