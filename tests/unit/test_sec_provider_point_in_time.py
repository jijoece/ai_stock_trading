"""Milestone 11.3 Part 31: SEC point-in-time assurance for date-only
`filed` company-fact values.

`get_company_facts`'s `filed` field from SEC is a DATE only, not a
timestamp — a naive "available from 00:00 UTC on the filed date" check lets
a same-day *morning* `as_of` see a fact that, in reality, may not have been
filed until that afternoon (SEC accepts same-day filings up to ~5:30pm ET).
This is a real look-ahead bias. The fix treats a date-only fact as
available only from the start of the *next* UTC day — so no same-day
`as_of`, at any hour, can see it."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from trading_research.evidence_providers.http_client import HttpJsonClient
from trading_research.evidence_providers.rate_limits import MinIntervalRateLimiter
from trading_research.evidence_providers.sec_provider import SecEdgarClient

FILED_DATE = "2026-06-17"

COMPANY_FACTS_BODY = {
    "cik": 320193,
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        {
                            "start": "2026-01-01", "end": "2026-03-31", "val": 5000, "fy": 2026, "fp": "Q1",
                            "form": "10-Q", "filed": FILED_DATE, "accn": "x",
                        },
                    ]
                }
            }
        }
    },
}


def _client() -> SecEdgarClient:
    def handler(request):
        return httpx.Response(200, json=COMPANY_FACTS_BODY)

    http = HttpJsonClient(
        backoff_sleep_fn=lambda s: None, base_headers={"User-Agent": "test (contact: t@example.com)"},
        rate_limiter=MinIntervalRateLimiter(0.0), transport=httpx.MockTransport(handler), provider="sec-edgar",
    )
    return SecEdgarClient(http_client=http, user_agent="test (contact: t@example.com)")


def test_morning_as_of_on_filed_date_cannot_see_same_day_filing():
    """An intraday research `as_of` earlier the same calendar day the fact
    was filed must NOT see it — even though a naive midnight-UTC check
    would have let it through."""
    client = _client()
    morning_as_of = datetime(2026, 6, 17, 13, 0, tzinfo=timezone.utc)  # 9am ET same day as filed
    facts = client.get_company_facts("AAPL", as_of=morning_as_of, cik="0000320193")
    assert facts == ()


def test_late_same_day_as_of_still_cannot_see_same_day_filing():
    """Even a late-evening as_of on the *same* filed date must not see it —
    we only know the filing DATE, not the actual intraday acceptance time,
    so the conservative rule withholds visibility for the entire filed day."""
    client = _client()
    late_as_of = datetime(2026, 6, 17, 23, 59, tzinfo=timezone.utc)
    facts = client.get_company_facts("AAPL", as_of=late_as_of, cik="0000320193")
    assert facts == ()


def test_next_day_as_of_can_see_the_filing():
    client = _client()
    next_day_as_of = datetime(2026, 6, 18, 0, 0, 1, tzinfo=timezone.utc)
    facts = client.get_company_facts("AAPL", as_of=next_day_as_of, cik="0000320193")
    assert len(facts) == 1
    assert facts[0].value == 5000


def test_well_after_filing_as_of_can_see_the_filing():
    client = _client()
    later_as_of = datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc)
    facts = client.get_company_facts("AAPL", as_of=later_as_of, cik="0000320193")
    assert len(facts) == 1
