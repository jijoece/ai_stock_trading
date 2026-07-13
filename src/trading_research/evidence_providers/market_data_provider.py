"""Real market-data client against Alpaca's data plane (docs/milestone-6.md
Step 7). Reuses the same `ALPACA_API_KEY`/`ALPACA_API_SECRET` credentials the
isolated paper-broker runtime uses (Milestone 4) — but this client calls
`data.alpaca.markets` directly over HTTPS from the *main* process. It does
**not** import LumiBot or alpaca-py, so the Milestone 3/4 process-isolation
invariant ("main application does not import LumiBot") is unaffected.

Adjustment is always explicit: `adjustment="raw"` (default) returns
unadjusted OHLCV and is recorded as `PriceBar.adjusted=False`;
`adjustment="split"`/`"all"` returns split/dividend-adjusted bars and is
recorded as `adjusted=True`. The two are never silently mixed.

Implements both the raw `models.MarketDataProvider` Protocol and
`evaluation/price_provider.py::PriceProvider` (`get_close`) — one real
provider serves both evidence-building and forward-performance evaluation,
per the milestone's "avoid several overlapping market-data providers"
guidance.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from ..evaluation.price_provider import PricePoint
from .cache import CacheKey, ProviderCache, TTL_CURRENT_QUOTE, TTL_HISTORICAL_BARS
from .errors import MalformedProviderResponseError, ProviderConfigurationError
from .http_client import HttpJsonClient
from .models import PriceBar, Quote

DATA_BASE_URL = "https://data.alpaca.markets/v2/stocks"
PROVIDER_NAME = "alpaca-data"


def _parse_bar_date(raw_timestamp: str) -> date:
    return datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00")).date()


class AlpacaMarketDataClient:
    def __init__(
        self, *, api_key: str | None, api_secret: str | None, http_client: HttpJsonClient,
        cache: ProviderCache | None = None, feed: str = "iex",
    ):
        """`feed="iex"` (default) — confirmed via a real request during
        implementation that this account's subscription tier returns
        `403 subscription does not permit querying recent SIP data` for the
        default SIP feed on recent date ranges; IEX is the free-tier-
        compatible feed and is what this client requests unless a caller
        with a higher-tier subscription explicitly overrides it."""
        if not api_key or not api_secret:
            raise ProviderConfigurationError(
                "AlpacaMarketDataClient requires ALPACA_API_KEY and ALPACA_API_SECRET"
            )
        if feed not in ("iex", "sip"):
            raise ProviderConfigurationError(f"unknown feed {feed!r} — fails closed")
        self._http = http_client
        self._cache = cache
        self._feed = feed

    def get_quote(self, symbol: str, *, as_of: datetime) -> Quote | None:
        symbol = symbol.upper()
        key = CacheKey.build(provider=PROVIDER_NAME, operation="quote", symbol=symbol)
        payload = self._cache.get(key) if self._cache else None
        if payload is None:
            payload, _meta = self._http.get_json(
                f"{DATA_BASE_URL}/{symbol}/quotes/latest", params={"feed": self._feed}, operation="quote", symbol=symbol,
            )
            if self._cache:
                self._cache.set(key, payload, ttl_seconds=TTL_CURRENT_QUOTE)
        if not isinstance(payload, dict) or "quote" not in payload:
            raise MalformedProviderResponseError(f"Alpaca quote response for {symbol} missing 'quote'")

        q = payload["quote"]
        provider_ts = datetime.fromisoformat(q["t"].replace("Z", "+00:00"))
        if provider_ts > as_of:
            return None  # a quote timestamped after as_of is not usable — no look-ahead
        bid = Decimal(str(q.get("bp", 0) or 0))
        ask = Decimal(str(q.get("ap", 0) or 0))
        if bid > 0 and ask > 0:
            price = (bid + ask) / 2
        elif bid > 0:
            price = bid
        elif ask > 0:
            price = ask
        else:
            return None  # genuinely no quote available — never fabricated
        return Quote(
            symbol=symbol, as_of=as_of, price=price, bid=bid if bid > 0 else None,
            ask=ask if ask > 0 else None, provider=PROVIDER_NAME, provider_timestamp=provider_ts,
        )

    def get_price_history(
        self, symbol: str, *, start: date, end: date, as_of: datetime, adjustment: str = "raw",
    ) -> tuple[PriceBar, ...]:
        symbol = symbol.upper()
        if adjustment not in ("raw", "split", "all"):
            raise ProviderConfigurationError(f"unknown adjustment {adjustment!r} — fails closed")
        key = CacheKey.build(
            provider=PROVIDER_NAME, operation="bars", symbol=symbol,
            start=start.isoformat(), end=end.isoformat(), adjustment=adjustment,
        )
        payload = self._cache.get(key) if self._cache else None
        if payload is None:
            payload, _meta = self._http.get_json(
                f"{DATA_BASE_URL}/{symbol}/bars",
                params={
                    "timeframe": "1Day", "start": start.isoformat(), "end": end.isoformat(),
                    "adjustment": adjustment, "limit": 10000, "feed": self._feed,
                },
                operation="bars", symbol=symbol,
            )
            if self._cache:
                self._cache.set(key, payload, ttl_seconds=TTL_HISTORICAL_BARS)
        if not isinstance(payload, dict) or "bars" not in payload:
            raise MalformedProviderResponseError(f"Alpaca bars response for {symbol} missing 'bars'")

        bars: list[PriceBar] = []
        seen_dates: set[date] = set()
        adjusted = adjustment in ("split", "all")
        as_of_date = as_of.date()
        for raw in payload["bars"] or []:
            session_date = _parse_bar_date(raw["t"])
            if session_date > as_of_date:
                continue  # reject future bars relative to as_of
            if session_date in seen_dates:
                continue  # reject duplicate sessions (defensive; Alpaca does not send these)
            seen_dates.add(session_date)
            bars.append(PriceBar(
                symbol=symbol, session_date=session_date,
                open=Decimal(str(raw["o"])), high=Decimal(str(raw["h"])),
                low=Decimal(str(raw["l"])), close=Decimal(str(raw["c"])),
                volume=int(raw["v"]), adjusted=adjusted, provider=PROVIDER_NAME,
            ))
        bars.sort(key=lambda b: b.session_date)
        for prev, cur in zip(bars, bars[1:]):
            if cur.session_date <= prev.session_date:
                raise MalformedProviderResponseError(f"{symbol}: non-monotonic bar sequence around {cur.session_date}")
        return tuple(bars)

    # -- evaluation.price_provider.PriceProvider ----------------------------

    def get_close(self, symbol: str, as_of: date) -> PricePoint | None:
        """Historical closing price for one trading date — never a live quote
        substituted for a missing historical close (docs/milestone-6.md
        safety requirement)."""
        as_of_dt = datetime(as_of.year, as_of.month, as_of.day, 23, 59, 59, tzinfo=timezone.utc)
        bars = self.get_price_history(symbol, start=as_of, end=as_of, as_of=as_of_dt)
        for bar in bars:
            if bar.session_date == as_of:
                return PricePoint(symbol=symbol, as_of=as_of, close=bar.close, source=PROVIDER_NAME)
        return None
