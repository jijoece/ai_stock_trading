"""Unit tests for evidence_providers/corporate_actions.py — Milestone 7
docs/milestone-7.md Step 8/27. No real network: httpx.MockTransport only.

Endpoint contract verified against the official alpaca-py SDK source (see
corporate_actions.py module docstring) — these tests assert this module's
behavior against that verified contract, not a guessed one.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest

from trading_research.evidence_providers.corporate_actions import (
    ACTION_TYPE_CASH_DIVIDEND,
    ACTION_TYPE_FORWARD_SPLIT,
    ACTION_TYPE_REVERSE_SPLIT,
    MAX_ACTIONS_RETURNED,
    AlpacaCorporateActionsClient,
)
from trading_research.evidence_providers.errors import MalformedProviderResponseError, ProviderConfigurationError
from trading_research.evidence_providers.http_client import HttpJsonClient
from trading_research.evidence_providers.rate_limits import MinIntervalRateLimiter

AS_OF = datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc)

FORWARD_SPLIT_BODY = {
    "corporate_actions": {
        "forward_splits": [
            {
                "symbol": "AAPL", "new_rate": 4.0, "old_rate": 1.0,
                "process_date": "2026-06-01", "ex_date": "2026-06-01",
                "record_date": "2026-05-28", "payable_date": None,
            }
        ]
    },
    "next_page_token": None,
}

DIVIDEND_BODY = {
    "corporate_actions": {
        "cash_dividends": [
            {
                "symbol": "AAPL", "rate": 0.24, "special": False, "foreign": False,
                "process_date": "2026-06-15", "ex_date": "2026-06-10",
                "record_date": "2026-06-11", "payable_date": "2026-06-15",
            }
        ]
    },
    "next_page_token": None,
}


def _client(handler, *, cache=None) -> AlpacaCorporateActionsClient:
    transport = httpx.MockTransport(handler)
    http = HttpJsonClient(
        base_headers={"APCA-API-KEY-ID": "k", "APCA-API-SECRET-KEY": "s"},
        rate_limiter=MinIntervalRateLimiter(0.0), transport=transport, provider="alpaca-corporate-actions",
    )
    return AlpacaCorporateActionsClient(api_key="k", api_secret="s", http_client=http, cache=cache)


def test_requires_credentials():
    http = HttpJsonClient(base_headers={}, rate_limiter=MinIntervalRateLimiter(0.0))
    with pytest.raises(ProviderConfigurationError):
        AlpacaCorporateActionsClient(api_key=None, api_secret="s", http_client=http)
    with pytest.raises(ProviderConfigurationError):
        AlpacaCorporateActionsClient(api_key="k", api_secret=None, http_client=http)


def test_sends_alpaca_auth_headers():
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("apca-api-key-id")
        seen["secret"] = request.headers.get("apca-api-secret-key")
        return httpx.Response(200, json=FORWARD_SPLIT_BODY)

    client = _client(handler)
    client.list_corporate_actions("AAPL", as_of=AS_OF)
    assert seen["key"] == "k"
    assert seen["secret"] == "s"


def test_normalizes_forward_split():
    def handler(request):
        return httpx.Response(200, json=FORWARD_SPLIT_BODY)

    client = _client(handler)
    actions = client.list_corporate_actions("AAPL", as_of=AS_OF)
    assert len(actions) == 1
    action = actions[0]
    assert action.action_type == ACTION_TYPE_FORWARD_SPLIT
    assert action.process_date == date(2026, 6, 1)
    assert action.ex_date == date(2026, 6, 1)
    assert action.record_date == date(2026, 5, 28)
    assert action.payable_date is None
    assert action.old_rate == Decimal("1.0")
    assert action.new_rate == Decimal("4.0")
    assert action.provider == "alpaca-corporate-actions"


def test_normalizes_cash_dividend():
    def handler(request):
        return httpx.Response(200, json=DIVIDEND_BODY)

    client = _client(handler)
    actions = client.list_corporate_actions("AAPL", as_of=AS_OF)
    assert len(actions) == 1
    action = actions[0]
    assert action.action_type == ACTION_TYPE_CASH_DIVIDEND
    assert action.rate == Decimal("0.24")
    assert action.ex_date == date(2026, 6, 10)
    assert action.record_date == date(2026, 6, 11)
    assert action.payable_date == date(2026, 6, 15)


def test_reverse_split_normalized():
    body = {
        "corporate_actions": {
            "reverse_splits": [{
                "symbol": "XYZ", "new_rate": 1.0, "old_rate": 10.0,
                "process_date": "2026-05-01", "ex_date": "2026-05-01",
                "record_date": None, "payable_date": None,
            }]
        },
        "next_page_token": None,
    }

    def handler(request):
        return httpx.Response(200, json=body)

    client = _client(handler)
    actions = client.list_corporate_actions("XYZ", as_of=AS_OF)
    assert actions[0].action_type == ACTION_TYPE_REVERSE_SPLIT
    assert actions[0].old_rate == Decimal("10.0")
    assert actions[0].new_rate == Decimal("1.0")


# -- point-in-time safety ------------------------------------------------------

def test_excludes_future_actions():
    future_body = {
        "corporate_actions": {
            "forward_splits": [
                FORWARD_SPLIT_BODY["corporate_actions"]["forward_splits"][0],
                {
                    "symbol": "AAPL", "new_rate": 2.0, "old_rate": 1.0,
                    "process_date": "2026-12-31", "ex_date": "2026-12-31",
                    "record_date": None, "payable_date": None,
                },
            ]
        },
        "next_page_token": None,
    }

    def handler(request):
        return httpx.Response(200, json=future_body)

    client = _client(handler)
    actions = client.list_corporate_actions("AAPL", as_of=AS_OF)
    assert all(a.process_date <= AS_OF.date() for a in actions)
    assert date(2026, 12, 31) not in {a.process_date for a in actions}


def test_never_infers_action_from_price_data():
    """Structural check: this module imports nothing from
    market_data_provider.py and defines no dependency on price bars — it
    only ever parses the corporate-actions response body. (Prose in the
    module docstring may *mention* `get_price_history` to explain the
    distinction; only actual imports/code are checked here.)"""
    import ast
    import inspect

    from trading_research.evidence_providers import corporate_actions

    tree = ast.parse(inspect.getsource(corporate_actions))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "market_data_provider" not in " ".join(
        n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
    )
    assert "AlpacaMarketDataClient" not in imported_names
    assert "PriceBar" not in imported_names


# -- unknown/deferred action types --------------------------------------------

def test_deferred_action_type_rejected():
    http = HttpJsonClient(base_headers={}, rate_limiter=MinIntervalRateLimiter(0.0))
    client = AlpacaCorporateActionsClient(
        api_key="k", api_secret="s",
        http_client=HttpJsonClient(base_headers={}, rate_limiter=MinIntervalRateLimiter(0.0), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=FORWARD_SPLIT_BODY))),
    )
    with pytest.raises(ProviderConfigurationError):
        client.list_corporate_actions("AAPL", as_of=AS_OF, types=("cash_merger",))


def test_unrecognized_response_type_key_skipped_not_guessed():
    body = {
        "corporate_actions": {
            "forward_splits": FORWARD_SPLIT_BODY["corporate_actions"]["forward_splits"],
            "cash_mergers": [{"symbol": "AAPL", "process_date": "2026-06-01"}],  # not implemented -> skipped
        },
        "next_page_token": None,
    }

    def handler(request):
        return httpx.Response(200, json=body)

    client = _client(handler)
    actions = client.list_corporate_actions("AAPL", as_of=AS_OF)
    assert len(actions) == 1
    assert actions[0].action_type == ACTION_TYPE_FORWARD_SPLIT


# -- malformed responses -----------------------------------------------------

def test_malformed_response_missing_corporate_actions_key():
    def handler(request):
        return httpx.Response(200, json={"unexpected": True})

    client = _client(handler)
    with pytest.raises(MalformedProviderResponseError):
        client.list_corporate_actions("AAPL", as_of=AS_OF)


def test_missing_process_date_raises():
    body = {
        "corporate_actions": {
            "cash_dividends": [{"symbol": "AAPL", "rate": 0.5}]  # no process_date
        },
        "next_page_token": None,
    }

    def handler(request):
        return httpx.Response(200, json=body)

    client = _client(handler)
    with pytest.raises(MalformedProviderResponseError):
        client.list_corporate_actions("AAPL", as_of=AS_OF)


# -- adjusted bars vs modeled actions distinction ------------------------------

def test_corporate_action_distinct_from_price_bar_model():
    from trading_research.evidence_providers.models import PriceBar

    fields = {f for f in dir(PriceBar) if not f.startswith("_")}
    assert "process_date" not in fields
    assert "ex_date" not in fields


# -- pagination / size cap ----------------------------------------------------

def test_pagination_merges_pages():
    page1 = {
        "corporate_actions": {"forward_splits": [FORWARD_SPLIT_BODY["corporate_actions"]["forward_splits"][0]]},
        "next_page_token": "tok1",
    }
    page2 = {
        "corporate_actions": {"cash_dividends": [DIVIDEND_BODY["corporate_actions"]["cash_dividends"][0]]},
        "next_page_token": None,
    }
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=page1 if calls["n"] == 1 else page2)

    client = _client(handler)
    actions = client.list_corporate_actions("AAPL", as_of=AS_OF)
    assert calls["n"] == 2
    assert {a.action_type for a in actions} == {ACTION_TYPE_FORWARD_SPLIT, ACTION_TYPE_CASH_DIVIDEND}


def test_caps_returned_actions():
    many = [
        {
            "symbol": "AAPL", "rate": 0.1, "special": False, "foreign": False,
            "process_date": f"2020-01-{(i % 27) + 1:02d}", "ex_date": None, "record_date": None, "payable_date": None,
        }
        for i in range(700)
    ]

    def handler(request):
        return httpx.Response(200, json={"corporate_actions": {"cash_dividends": many}, "next_page_token": None})

    client = _client(handler)
    actions = client.list_corporate_actions("AAPL", as_of=AS_OF)
    assert len(actions) <= MAX_ACTIONS_RETURNED


# -- caching -------------------------------------------------------------------

def test_cache_hit_avoids_second_network_call():
    import time

    from trading_research.evidence_providers.cache import ProviderCache

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=FORWARD_SPLIT_BODY)

    cache = ProviderCache(clock=time.monotonic)
    client = _client(handler, cache=cache)
    client.list_corporate_actions("AAPL", as_of=AS_OF)
    client.list_corporate_actions("AAPL", as_of=AS_OF)
    assert calls["n"] == 1
