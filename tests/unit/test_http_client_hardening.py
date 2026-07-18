"""Milestone 11.3 Part 24: HTTP client pooling/bounding — persistent
client reuse, bounded response bytes/JSON depth, Retry-After honored,
exponential backoff with a cap, credential redaction, no raw body in
errors."""
from __future__ import annotations

import json

import httpx
import pytest

from trading_research.evidence_providers.errors import (
    MalformedProviderResponseError,
    ProviderRequestError,
    RetryBoundExceededError,
)
from trading_research.evidence_providers.http_client import (
    MAX_RESPONSE_BYTES,
    HttpJsonClient,
    redact_credential_query_params,
)
from trading_research.evidence_providers.rate_limits import MinIntervalRateLimiter


def _client(handler, **kwargs) -> HttpJsonClient:
    transport = httpx.MockTransport(handler)
    return HttpJsonClient(
        base_headers={}, rate_limiter=MinIntervalRateLimiter(0.0), transport=transport,
        backoff_sleep_fn=lambda s: None, **kwargs,
    )


# --- persistent client / pooling --------------------------------------------

def test_underlying_httpx_client_is_reused_across_calls():
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    client.get_json("https://example.com/a")
    first = client._client
    client.get_json("https://example.com/b")
    second = client._client
    assert first is not None
    assert first is second


def test_close_tears_down_the_underlying_client():
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    client.get_json("https://example.com/a")
    assert client._client is not None
    client.close()
    assert client._client is None


def test_context_manager_closes_on_exit():
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as client:
        client.get_json("https://example.com/a")
        assert client._client is not None
    assert client._client is None


# --- bounded response bytes --------------------------------------------------

def test_oversized_response_is_rejected_not_buffered_fully():
    huge_body = b"[" + b"1," * (MAX_RESPONSE_BYTES // 2) + b"1]"

    def handler(request):
        return httpx.Response(200, content=huge_body)

    client = _client(handler, max_attempts=1)
    with pytest.raises(RetryBoundExceededError):
        client.get_json("https://example.com/big")


def test_normal_sized_response_is_unaffected():
    def handler(request):
        return httpx.Response(200, json={"a": 1})

    client = _client(handler)
    parsed, meta = client.get_json("https://example.com/small")
    assert parsed == {"a": 1}
    assert meta.status_code == 200


# --- bounded JSON depth -------------------------------------------------------

def test_pathologically_nested_json_is_rejected():
    nested: object = 1
    for _ in range(200):
        nested = [nested]

    def handler(request):
        return httpx.Response(200, content=json.dumps(nested).encode())

    client = _client(handler, max_attempts=1)
    with pytest.raises(MalformedProviderResponseError):
        client.get_json("https://example.com/deep")


def test_reasonably_nested_json_is_accepted():
    def handler(request):
        return httpx.Response(200, json={"a": {"b": {"c": [1, 2, 3]}}})

    client = _client(handler)
    parsed, _meta = client.get_json("https://example.com/ok-nested")
    assert parsed["a"]["b"]["c"] == [1, 2, 3]


# --- Retry-After ---------------------------------------------------------------

def test_retry_after_seconds_header_is_honored():
    waits: list[float] = []
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"error": "rate limited"})
        return httpx.Response(200, json={"ok": True})

    client = _client(handler, max_attempts=2, backoff_base_seconds=0.01, backoff_max_seconds=1.0)
    client.backoff_sleep_fn = lambda s: waits.append(s)
    parsed, _meta = client.get_json("https://example.com/retry-after")
    assert parsed == {"ok": True}
    assert waits and waits[0] >= 3.0


def test_retry_after_is_capped_not_trusted_unbounded():
    waits: list[float] = []

    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "999999"}, json={"error": "rate limited"})

    client = _client(handler, max_attempts=2, backoff_base_seconds=0.01, backoff_max_seconds=1.0)
    client.backoff_sleep_fn = lambda s: waits.append(s)
    with pytest.raises(RetryBoundExceededError):
        client.get_json("https://example.com/retry-after-huge")
    assert waits and waits[0] <= 120.0


# --- exponential backoff ------------------------------------------------------

def test_backoff_grows_exponentially_and_is_capped():
    waits: list[float] = []

    def handler(request):
        return httpx.Response(503, json={"error": "unavailable"})

    client = _client(handler, max_attempts=4, backoff_base_seconds=1.0, backoff_max_seconds=2.5)
    client.backoff_sleep_fn = lambda s: waits.append(s)
    with pytest.raises(RetryBoundExceededError):
        client.get_json("https://example.com/backoff")
    # base * 2**0, base * 2**1, base * 2**2 capped at 2.5 -> [1.0, 2.0, 2.5]
    assert waits == [1.0, 2.0, 2.5]


# --- credential redaction -----------------------------------------------------

def test_credential_query_param_redacted_in_helper():
    redacted = redact_credential_query_params("https://example.com/x?apikey=SECRET123&symbol=AAPL")
    assert "SECRET123" not in redacted
    assert "symbol=AAPL" in redacted
    assert "REDACTED" in redacted


def test_credential_query_param_redacted_in_response_meta():
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    _parsed, meta = client.get_json("https://example.com/x", params={"apikey": "SECRET123", "symbol": "AAPL"})
    assert "SECRET123" not in meta.request_url
    assert "AAPL" in meta.request_url


def test_credential_query_param_redacted_in_error_message():
    def handler(request):
        return httpx.Response(404, json={"error": "not found"})

    client = _client(handler, max_attempts=1)
    with pytest.raises(ProviderRequestError) as exc_info:
        client.get_json("https://example.com/x", params={"token": "SECRET123"})
    assert "SECRET123" not in str(exc_info.value)


# --- no raw body in errors ----------------------------------------------------

def test_non_retryable_error_never_includes_raw_response_body():
    def handler(request):
        return httpx.Response(404, content=b"super-secret-internal-debug-trace-do-not-leak")

    client = _client(handler, max_attempts=1)
    with pytest.raises(ProviderRequestError) as exc_info:
        client.get_json("https://example.com/notfound")
    assert "super-secret-internal-debug-trace-do-not-leak" not in str(exc_info.value)


def test_retry_bound_exceeded_error_never_includes_raw_response_body():
    def handler(request):
        return httpx.Response(503, content=b"super-secret-internal-debug-trace-do-not-leak")

    client = _client(handler, max_attempts=2, backoff_base_seconds=0.0, backoff_max_seconds=0.0)
    with pytest.raises(RetryBoundExceededError) as exc_info:
        client.get_json("https://example.com/unavailable")
    assert "super-secret-internal-debug-trace-do-not-leak" not in str(exc_info.value)


# --- idempotent-only retry (structural) ---------------------------------------

def test_client_has_no_write_method_only_get_json():
    import inspect
    public_methods = {
        name for name, member in vars(HttpJsonClient).items()
        if not name.startswith("_") and inspect.isfunction(member)
    }
    assert public_methods == {"get_json", "close"}
