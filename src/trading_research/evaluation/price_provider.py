"""Point-in-time historical close price provider (docs/milestone-4.md Step
11).

No live price data source ships in this milestone — this repository has no
existing historical market-data fetcher (confirmed absent from
`collection/`, `processing/`, `mcp/` at implementation time), and Step 10's
"never fetch current quotes for historical evaluation" / "no look-ahead
bias" requirements mean a real implementation would need genuine
point-in-time historical bars, not a live quote endpoint. `PriceProvider` is
the seam a future milestone implements against a real data source (e.g. a
verified historical-bars API); `DeterministicPriceProvider` is a fixture-
driven offline stand-in for tests, analogous to
`runtime.deterministic_adapter.DeterministicPaperAdapter` in Milestone 3.
See docs/milestone4-isolated-paper-broker.md "Known limitations".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class PricePoint:
    symbol: str
    as_of: date
    close: Decimal
    source: str
    available_at: datetime | None = None

    @property
    def session_date(self) -> date:
        return self.as_of


class PriceProvider(Protocol):
    def get_close(self, symbol: str, as_of: date) -> PricePoint | None:
        """Return the symbol's closing price for the given trading date, or
        `None` if genuinely unavailable (delisted, no data, not yet
        published). Must never return a current/live quote as a stand-in
        for a historical date — no look-ahead, no fabricated prices."""
        ...


@dataclass
class DeterministicPriceProvider:
    """In-memory fixture provider — test/offline use only. `register` is the
    only way a price becomes available; nothing here fetches, estimates, or
    interpolates a missing price."""

    _prices: dict[tuple[str, date], tuple[Decimal, datetime | None]] = field(default_factory=dict)

    def register(
        self, symbol: str, as_of: date, close: Decimal | str | float, *, available_at: datetime | None = None,
    ) -> None:
        self._prices[(symbol, as_of)] = (Decimal(str(close)), available_at)

    def get_close(self, symbol: str, as_of: date) -> PricePoint | None:
        stored = self._prices.get((symbol, as_of))
        if stored is None:
            return None
        close, available_at = stored
        return PricePoint(
            symbol=symbol, as_of=as_of, close=close, source="deterministic-fixture",
            available_at=available_at,
        )
