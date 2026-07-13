"""Opt-in real news-provider smoke test (Milestone 6, docs/milestone-6.md
Step 23). **Environmentally pending in this repository**: no real news
provider is implemented (see `evidence_providers/news_provider.py`'s module
docstring — no API key is configured anywhere in this milestone's
environment). This test documents the gate honestly rather than faking a
pass: it is always skipped, with a reason that names the real blocker,
whether or not `RUN_NEWS_API_TESTS=true` is set.
"""
from __future__ import annotations

import os

import pytest

from trading_research.evidence_providers.errors import ProviderConfigurationError
from trading_research.evidence_providers.news_provider import UnconfiguredNewsProvider

pytestmark = pytest.mark.news_api


@pytest.mark.skipif(
    os.environ.get("RUN_NEWS_API_TESTS") != "true",
    reason="RUN_NEWS_API_TESTS is not 'true' — skipped by default",
)
def test_no_real_news_provider_is_configured_in_this_environment():
    """This is the honest, current state: no real news-provider
    implementation exists in this milestone (docs/milestone-6.md Step 9
    marks it optional; ENVIRONMENTALLY_PENDING per the final report). This
    test exists so a future contributor who adds
    `RUN_NEWS_API_TESTS=true` gets an explicit, actionable failure — not a
    silent skip that could be mistaken for "already validated"."""
    provider = UnconfiguredNewsProvider()
    with pytest.raises(ProviderConfigurationError):
        from datetime import datetime, timezone

        provider.list_news("AAPL", published_after=datetime.now(timezone.utc), available_by=datetime.now(timezone.utc))
    pytest.skip(
        "ENVIRONMENTALLY_PENDING: no real news-provider API key is configured in this "
        "milestone's environment — see docs/milestone6-real-evidence-continuous-evaluation.md "
        "'Known limitations'. Implement a real NewsProvider (models.py Protocol) and wire it "
        "into evidence_adapters.RealNewsEvidenceProvider to unblock this test."
    )
