"""Mock Robinhood and Reddit adapters for offline development and tests.

These return deterministic fixture data shaped like the real adapters'
output. They exist so the full pipeline (extract → aggregate → screen →
risk → recommend → paper-fill) runs end-to-end with zero network access and
zero credentials — and so CI can never touch a live broker or Reddit.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..analysis.sentiment import RedditRecord


@dataclass(frozen=True)
class MockQuote:
    symbol: str
    bid: float
    ask: float
    last: float
    as_of_epoch: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


_QUOTES = {
    "SOFI": (14.90, 14.94, 14.92),
    "PLTR": (23.80, 23.86, 23.83),
    "OPEN": (3.41, 3.45, 3.43),
    "LUMN": (5.62, 5.68, 5.65),
    "F": (11.20, 11.22, 11.21),
    "AI": (21.30, 21.40, 21.35),
    "AAPL": (232.10, 232.16, 232.13),
}

_FUNDAMENTALS = {
    "SOFI": {"market_cap": 15_800_000_000, "avg_daily_dollar_volume": 350_000_000,
             "revenue_growth_yoy": 0.27, "free_cash_flow": 210_000_000,
             "cash": 2_900_000_000, "debt": 3_100_000_000, "is_otc": False,
             "days_to_earnings": 18.0},
    "OPEN": {"market_cap": 2_400_000_000, "avg_daily_dollar_volume": 95_000_000,
             "revenue_growth_yoy": -0.04, "free_cash_flow": -120_000_000,
             "cash": 880_000_000, "debt": 2_200_000_000, "is_otc": False,
             "days_to_earnings": 9.0},
}


class MockRobinhoodAdapter:
    """Read-only mock. Has no write/order methods at all, by construction."""

    def __init__(self, now_epoch: float | None = None):
        self._now = now_epoch or time.time()

    def get_equity_quote(self, symbol: str) -> MockQuote:
        symbol = symbol.upper()
        if symbol not in _QUOTES:
            raise KeyError(f"no mock quote for {symbol}")
        bid, ask, last = _QUOTES[symbol]
        return MockQuote(symbol=symbol, bid=bid, ask=ask, last=last, as_of_epoch=self._now)

    def get_equity_fundamentals(self, symbol: str) -> dict:
        symbol = symbol.upper()
        if symbol not in _FUNDAMENTALS:
            raise KeyError(f"no mock fundamentals for {symbol}")
        return dict(_FUNDAMENTALS[symbol])

    def get_account_state(self) -> dict:
        return {
            "equity": 100_000.0,
            "settled_cash": 40_000.0,
            "as_of_epoch": self._now,
            "positions": [],
        }


@dataclass
class MockRedditAdapter:
    """Read-only mock returning fixture posts/comments as RedditRecords."""

    now_epoch: float = field(default_factory=time.time)

    def fetch_records(self, symbol: str) -> list[RedditRecord]:
        symbol = symbol.upper()
        h = 3600.0
        base = self.now_epoch
        fixtures = {
            "SOFI": [
                RedditRecord("p1", "post", "SOFI", "u_alpha", "stocks", base - 2 * h,
                             "$SOFI earnings look strong, revenue beat and guidance up. Long here.", 148),
                RedditRecord("p2", "post", "SOFI", "u_beta", "wallstreetbets", base - 5 * h,
                             "SOFI calls printing, this stock wants to moon", 96),
                RedditRecord("c1", "comment", "SOFI", "u_gamma", "stocks", base - 1.5 * h,
                             "I trimmed my SOFI position, valuation getting rich — might sell more", 12),
                RedditRecord("c2", "comment", "SOFI", "u_alpha", "investing", base - 20 * h,
                             "SOFI is undervalued relative to book, buy the dip", 33),
                RedditRecord("p3", "post", "SOFI", "u_promo", "pennystocks", base - 3 * h,
                             "SOFI to the moon buy buy buy 🚀🚀🚀 link in bio", 2, is_duplicate=True),
            ],
            "OPEN": [
                RedditRecord("p4", "post", "OPEN", "u_delta", "wallstreetbets", base - 4 * h,
                             "$OPEN short squeeze setup? Float is tiny. YOLO puts or calls?", 61),
                RedditRecord("c3", "comment", "OPEN", "u_eps", "stocks", base - 2 * h,
                             "OPEN keeps diluting shareholders, dump it — bagholder city", 9),
            ],
        }
        return fixtures.get(symbol, [])
