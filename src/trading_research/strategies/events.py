"""Normalized, point-in-time-safe market-event record (Milestone 23, B5).

`MarketEvent` is a deterministic contract, not a provider adapter. Wiring
live sources (`evidence_providers/alpaca_news_provider.py`,
`corporate_actions.py`, `economic_calendar.py`) into `MarketEvent` instances
is a separate follow-up per source — each has different reliability and
latency characteristics that must be verified before being trusted as an
event detector. `event_type` is restricted to a small allowlist so an
unrecognized event never silently enters the strategy.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..models.trading_models import DataFreshness

ALLOWED_EVENT_TYPES = frozenset({
    "earnings_result",
    "guidance_change",
    "analyst_estimate_revision",
    "material_sec_filing",
    "contract_or_partnership_announcement",
    "regulatory_decision",
})


class MarketEventError(ValueError):
    """A `MarketEvent` was constructed with an invalid or unrecognized value."""


@dataclass(frozen=True)
class MarketEvent:
    event_id: str
    symbol: str
    event_type: str
    event_timestamp: datetime
    published_timestamp: datetime
    effective_timestamp: datetime
    source: str
    source_reference: str
    confidence_source: str
    positive_or_negative: int  # +1 positive, -1 negative, 0 neutral
    freshness: DataFreshness

    def __post_init__(self) -> None:
        if self.event_type not in ALLOWED_EVENT_TYPES:
            raise MarketEventError(
                f"event_type {self.event_type!r} is not in the supported allowlist {sorted(ALLOWED_EVENT_TYPES)}"
            )
        for name in ("event_timestamp", "published_timestamp", "effective_timestamp"):
            ts = getattr(self, name)
            if ts.tzinfo is None:
                raise MarketEventError(f"MarketEvent.{name} must be timezone-aware")
        if self.positive_or_negative not in (-1, 0, 1):
            raise MarketEventError("MarketEvent.positive_or_negative must be one of -1, 0, 1")
        if not self.event_id or not self.source or not self.source_reference:
            raise MarketEventError("MarketEvent.event_id/source/source_reference must be non-empty")
