"""Real corporate-action evidence client against Alpaca's Market Data API
(docs/milestone-7.md Step 8).

Endpoint contract verified from the official `alpaca-py` SDK source
(https://github.com/alpacahq/alpaca-py, `alpaca/data/historical/corporate_actions.py`,
`alpaca/data/models/corporate_actions.py`, `alpaca/data/requests.py`,
`alpaca/common/rest.py::_get_marketdata`) on 2026-07-13, since the interactive
`docs.alpaca.markets/reference/corporateactions-1` page does not statically
render its example response body — the SDK source is the ground truth this
implementation follows, not a guess:

* `GET https://data.alpaca.markets/v1/corporate-actions`
* same `APCA-API-KEY-ID`/`APCA-API-SECRET-KEY` header pair as
  `AlpacaMarketDataClient` and `AlpacaNewsClient` (this is the Market Data
  API, not the separate Broker API `corporate_actions/announcements`
  endpoint, which uses different auth and a different response shape —
  `ca_type`/`ca_sub_type`/`corporate_action_id` — and is *not* what this
  module implements).
* query params: `symbols` (comma-separated), `types` (comma-separated),
  `start`/`end` (`YYYY-MM-DD`), `limit` (1-1000, default 1000), `sort`
  (`asc`/`desc`), `page_token` for pagination.
* response body: `{"corporate_actions": {<type_key>: [...], ...},
  "next_page_token": <str|null>}` — the `corporate_actions` value is a dict
  keyed by pluralized snake_case type name (`forward_splits`,
  `reverse_splits`, `cash_dividends`, `stock_dividends`, `unit_splits`,
  `spin_offs`, `cash_mergers`, `stock_mergers`, `stock_and_cash_mergers`,
  `redemptions`, `name_changes`, `worthless_removals`,
  `rights_distributions`), each value a list of per-type objects.

This module implements only the two type keys `docs/milestone-7.md` Step 8
names as the primary examples and that this session verified field-for-field
against the SDK's Pydantic models — `forward_splits`/`reverse_splits`
(`ForwardSplit`/`ReverseSplit`: `new_rate`, `old_rate`, `process_date`,
`ex_date`, `record_date`, `payable_date`) and `cash_dividends`
(`CashDividend`: `rate`, `special`, `foreign`, `process_date`, `ex_date`,
`record_date`, `payable_date`). Mergers, spin-offs, symbol changes, and the
remaining type keys are DEFERRED — see this module's `DEFERRED_ACTION_TYPES`
and the Milestone 7 scratchpad's "Deferred work" section; their field
shapes were only partially verified this session and this module declines to
guess at the unverified remainder rather than risk misclassifying a merger's
effective date.

Point-in-time safety: `list_corporate_actions` only returns actions whose
`process_date` (Alpaca's own "this action affects account balances as of
this date" field — the closest verified analog to an effective date) is on
or before `as_of`. Nothing here infers a split or dividend from a price
jump; every returned `CorporateAction` traces back to one Alpaca corporate-
actions record with its own `process_date`/`ex_date`/`record_date`/
`payable_date` preserved verbatim. Adjusted price bars
(`AlpacaMarketDataClient.get_price_history(..., adjustment="split")`) are a
separate, already-existing evidence path — this module never conflates the
two; a caller wanting both must call both explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from .cache import CacheKey, ProviderCache, TTL_FILINGS
from .errors import MalformedProviderResponseError, ProviderConfigurationError
from .http_client import HttpJsonClient

CORPORATE_ACTIONS_BASE_URL = "https://data.alpaca.markets/v1/corporate-actions"
PROVIDER_NAME = "alpaca-corporate-actions"

ACTION_TYPE_FORWARD_SPLIT = "forward_split"
ACTION_TYPE_REVERSE_SPLIT = "reverse_split"
ACTION_TYPE_CASH_DIVIDEND = "cash_dividend"

IMPLEMENTED_ACTION_TYPES = (ACTION_TYPE_FORWARD_SPLIT, ACTION_TYPE_REVERSE_SPLIT, ACTION_TYPE_CASH_DIVIDEND)

# Alpaca-documented type keys this module deliberately does not implement —
# their exact per-field response shape was not verified against the SDK
# source with the same confidence as splits/dividends this session (see
# module docstring). Requesting one of these via `types=` would require
# extending this module first, not silently returning nothing for it.
DEFERRED_ACTION_TYPES = (
    "unit_split", "stock_dividend", "spin_off", "cash_merger", "stock_merger",
    "stock_and_cash_merger", "redemption", "name_change", "worthless_removal",
    "rights_distribution",
)

_RAW_TYPE_KEYS = {
    "forward_splits": ACTION_TYPE_FORWARD_SPLIT,
    "reverse_splits": ACTION_TYPE_REVERSE_SPLIT,
    "cash_dividends": ACTION_TYPE_CASH_DIVIDEND,
}

MAX_ACTIONS_RETURNED = 500
MAX_PAGES = 5


@dataclass(frozen=True)
class CorporateAction:
    """One normalized corporate action, distinct from and never conflated
    with an adjusted price bar (module docstring)."""

    symbol: str
    action_type: str  # one of IMPLEMENTED_ACTION_TYPES
    process_date: date  # Alpaca's "affects account balances as of" date
    ex_date: date | None
    record_date: date | None
    payable_date: date | None
    rate: Decimal | None  # cash_dividend: per-share cash amount
    old_rate: Decimal | None  # split: shares held before the split
    new_rate: Decimal | None  # split: shares held after the split
    provider: str
    provider_retrieved_at: datetime


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    return date.fromisoformat(raw)


def _parse_action(symbol: str, action_type: str, raw: dict, *, retrieved_at: datetime) -> CorporateAction:
    process_date = _parse_date(raw.get("process_date"))
    if process_date is None:
        raise MalformedProviderResponseError(
            f"Alpaca corporate action for {symbol} ({action_type}) missing required 'process_date'"
        )
    return CorporateAction(
        symbol=symbol,
        action_type=action_type,
        process_date=process_date,
        ex_date=_parse_date(raw.get("ex_date")),
        record_date=_parse_date(raw.get("record_date")),
        payable_date=_parse_date(raw.get("payable_date")),
        rate=Decimal(str(raw["rate"])) if raw.get("rate") is not None else None,
        old_rate=Decimal(str(raw["old_rate"])) if raw.get("old_rate") is not None else None,
        new_rate=Decimal(str(raw["new_rate"])) if raw.get("new_rate") is not None else None,
        provider=PROVIDER_NAME,
        provider_retrieved_at=retrieved_at,
    )


class AlpacaCorporateActionsClient:
    """Fails closed at construction when credentials are absent — mirrors
    `AlpacaMarketDataClient`/`AlpacaNewsClient`'s posture exactly."""

    def __init__(
        self, *, api_key: str | None, api_secret: str | None, http_client: HttpJsonClient,
        cache: ProviderCache | None = None,
    ):
        if not api_key or not api_secret:
            raise ProviderConfigurationError(
                "AlpacaCorporateActionsClient requires ALPACA_MARKET_DATA_API_KEY and ALPACA_MARKET_DATA_API_SECRET"
            )
        self._http = http_client
        self._cache = cache

    def list_corporate_actions(
        self, symbol: str, *, as_of: datetime, start: date | None = None,
        types: tuple[str, ...] = IMPLEMENTED_ACTION_TYPES,
    ) -> tuple[CorporateAction, ...]:
        symbol = symbol.upper()
        unknown = set(types) - set(IMPLEMENTED_ACTION_TYPES)
        if unknown:
            raise ProviderConfigurationError(
                f"requested corporate-action type(s) {sorted(unknown)} are not implemented — "
                f"see DEFERRED_ACTION_TYPES; only {IMPLEMENTED_ACTION_TYPES} are supported"
            )
        raw_type_keys = [k for k, v in _RAW_TYPE_KEYS.items() if v in types]

        window_start = start or date(1970, 1, 1)
        window_end = as_of.date()

        collected: dict[str, list[dict]] = {}
        next_page_token: str | None = None
        for _page in range(MAX_PAGES):
            params = {
                "symbols": symbol,
                "types": ",".join(raw_type_keys),
                "start": window_start.isoformat(),
                "end": window_end.isoformat(),
                "limit": min(1000, MAX_ACTIONS_RETURNED),
                "sort": "asc",
            }
            if next_page_token:
                params["page_token"] = next_page_token

            key = CacheKey.build(
                provider=PROVIDER_NAME, operation="corporate_actions", symbol=symbol,
                start=params["start"], end=params["end"], types=params["types"], page_token=next_page_token or "",
            )
            payload = self._cache.get(key) if self._cache else None
            if payload is None:
                payload, _meta = self._http.get_json(
                    CORPORATE_ACTIONS_BASE_URL, params=params, operation="corporate_actions", symbol=symbol,
                )
                if self._cache:
                    self._cache.set(key, payload, ttl_seconds=TTL_FILINGS)

            if not isinstance(payload, dict) or "corporate_actions" not in payload:
                raise MalformedProviderResponseError(
                    f"Alpaca corporate-actions response for {symbol} missing 'corporate_actions'"
                )

            actions_by_type = payload["corporate_actions"] or {}
            if not isinstance(actions_by_type, dict):
                raise MalformedProviderResponseError(
                    f"Alpaca corporate-actions response for {symbol}: 'corporate_actions' is not an object"
                )
            for raw_key, items in actions_by_type.items():
                collected.setdefault(raw_key, []).extend(items or [])

            next_page_token = payload.get("next_page_token")
            total = sum(len(v) for v in collected.values())
            if not next_page_token or total >= MAX_ACTIONS_RETURNED:
                break

        retrieved_at = as_of
        actions: list[CorporateAction] = []
        for raw_key, items in collected.items():
            action_type = _RAW_TYPE_KEYS.get(raw_key)
            if action_type is None:
                continue  # a type key this module has not verified/implemented — skip, never guess
            for raw in items:
                action = _parse_action(symbol, action_type, raw, retrieved_at=retrieved_at)
                if action.process_date > as_of.date():
                    continue  # point-in-time safety: never leak a future action into a historical snapshot
                actions.append(action)

        actions.sort(key=lambda a: (a.process_date, a.action_type))
        return tuple(actions[:MAX_ACTIONS_RETURNED])
