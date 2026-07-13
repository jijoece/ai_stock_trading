"""Real news-provider client (docs/milestone-6.md Step 9).

**No news-provider API key is configured in this environment** (confirmed
absent from `.env` at implementation time — see the Milestone 6 scratchpad's
"Available credentials/providers" section). `UnconfiguredNewsProvider` is the
one concrete implementation shipped: it is honest about this rather than
silently falling back to fixture data in a real run. A future operator who
obtains a real news-provider API key implements `models.NewsProvider`
(`list_news(symbol, *, published_after, available_by) -> tuple[NewsArticle, ...]`)
and passes it to `evidence_adapters.RealNewsEvidenceProvider` in place of
this class — no other code changes.
"""
from __future__ import annotations

from datetime import datetime

from .errors import ProviderConfigurationError
from .models import NewsArticle


class UnconfiguredNewsProvider:
    """Fails closed at every call — never silently returns empty as if there
    were simply no news, and never substitutes fixture data for a real
    provider in a real run."""

    def __init__(self, *, reason: str = "no real news-provider API key is configured"):
        self.reason = reason

    def list_news(
        self, symbol: str, *, published_after: datetime, available_by: datetime
    ) -> tuple[NewsArticle, ...]:
        raise ProviderConfigurationError(f"real news provider unavailable for {symbol}: {self.reason}")
