"""Thin, injectable-transport HTTP JSON client shared by the real provider
clients in this package (docs/milestone-6.md Steps 6-7). Bounded retry only
(no infinite retry), rate-limited via `rate_limits.MinIntervalRateLimiter`,
and every call returns latency/status/retrieval metadata so callers can
persist it (Step 5) without re-deriving it later.

`transport` is always injectable (`httpx.MockTransport` in tests) — mirrors
how `runtime/client/process_client.py::RuntimeClient` injects its transport
in Milestone 4, so the default test suite never opens a real socket.

Milestone 11.3 Part 24 hardening:

* one `httpx.Client` is now created lazily and reused across every
  `get_json()` call on this instance (connection pooling), instead of a
  fresh client per attempt; `close()` (and context-manager support) give an
  explicit lifecycle. `httpx.Client` is documented by httpx as safe for
  concurrent use by multiple threads issuing independent requests, so a
  shared `HttpJsonClient` instance is thread-safe for concurrent
  `get_json()` calls *at the transport level* — `on_response`'s callback and
  whatever it does with the result remain the caller's own responsibility.
* response bodies are read via streaming with a hard byte cap
  (`MAX_RESPONSE_BYTES`) — a provider that returns an unbounded body can no
  longer be read entirely into memory.
* parsed JSON is walked for structural depth after parsing
  (`MAX_JSON_DEPTH`) — a pathologically nested body fails closed with
  `MalformedProviderResponseError` rather than risking a deep-recursion
  failure downstream.
* only GET (idempotent read) requests are ever retried — this client has no
  POST/PUT/DELETE method at all, so that boundary is structural, not just a
  policy note.
* a valid `Retry-After` header (seconds or HTTP-date) is parsed and honored
  as an additional, capped wait before the next retry.
* retryable failures apply exponential backoff (`backoff_base_seconds *
  2 ** (attempt - 1)`, capped at `backoff_max_seconds`) on top of the rate
  limiter's own pacing, so a burst of failures cannot turn into a fast
  retry storm against an already-struggling provider.
* known credential-shaped query parameter names are redacted before a URL
  is placed into response metadata or an error message.
* raw response body text is never included in a persisted/raised error
  message — only status code and a fixed, non-body-derived description.
"""
from __future__ import annotations

import json
import socket
import ssl
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .errors import MalformedProviderResponseError, ProviderRateLimitedError, ProviderRequestError, RetryBoundExceededError
from .rate_limits import MinIntervalRateLimiter

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Milestone 11.3 Part 24: hard caps, deliberately generous (real filing/news/
# market-data bodies this codebase consumes are well under these) but never
# unbounded.
MAX_RESPONSE_BYTES = 20 * 1024 * 1024  # 20 MiB
MAX_JSON_DEPTH = 64
_MAX_RETRY_AFTER_SECONDS = 120.0

_CREDENTIAL_PARAM_NAMES = frozenset({
    "apikey", "api_key", "api-key", "token", "access_token", "secret", "secret_key",
    "client_secret", "auth", "authorization", "key",
})

TRANSPORT_NONE = "NONE"
TRANSPORT_TIMEOUT = "TIMEOUT"
TRANSPORT_DNS_FAILURE = "DNS_FAILURE"
TRANSPORT_CONNECTION_REFUSED = "CONNECTION_REFUSED"
TRANSPORT_CONNECTION_RESET = "CONNECTION_RESET"
TRANSPORT_TLS_FAILURE = "TLS_FAILURE"
TRANSPORT_AUTHENTICATION_FAILURE = "AUTHENTICATION_FAILURE"
TRANSPORT_RATE_LIMITED = "RATE_LIMITED"
TRANSPORT_HTTP_CLIENT_ERROR = "HTTP_CLIENT_ERROR"
TRANSPORT_HTTP_SERVER_ERROR = "HTTP_SERVER_ERROR"
TRANSPORT_PROTOCOL_ERROR = "PROTOCOL_ERROR"
TRANSPORT_CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
TRANSPORT_UNKNOWN_ERROR = "UNKNOWN_TRANSPORT_ERROR"

TRANSPORT_FAILURE_CATEGORIES = (
    TRANSPORT_NONE, TRANSPORT_TIMEOUT, TRANSPORT_DNS_FAILURE, TRANSPORT_CONNECTION_REFUSED,
    TRANSPORT_CONNECTION_RESET, TRANSPORT_TLS_FAILURE, TRANSPORT_AUTHENTICATION_FAILURE,
    TRANSPORT_RATE_LIMITED, TRANSPORT_HTTP_CLIENT_ERROR, TRANSPORT_HTTP_SERVER_ERROR,
    TRANSPORT_PROTOCOL_ERROR, TRANSPORT_CONFIGURATION_ERROR, TRANSPORT_UNKNOWN_ERROR,
)


def classify_httpx_transport_exception(exc: httpx.HTTPError) -> str:
    """Map library-specific exceptions once, while their typed cause chain exists."""
    if isinstance(exc, httpx.TimeoutException):
        return TRANSPORT_TIMEOUT
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, socket.gaierror):
            return TRANSPORT_DNS_FAILURE
        if isinstance(cause, ConnectionRefusedError):
            return TRANSPORT_CONNECTION_REFUSED
        if isinstance(cause, ConnectionResetError):
            return TRANSPORT_CONNECTION_RESET
        if isinstance(cause, ssl.SSLError):
            return TRANSPORT_TLS_FAILURE
        cause = cause.__cause__ or cause.__context__
    return TRANSPORT_UNKNOWN_ERROR


class ResponseTooLargeError(MalformedProviderResponseError):
    """A response body exceeded `MAX_RESPONSE_BYTES` before it could be
    fully read — the connection is aborted rather than reading the rest
    into memory."""


def redact_credential_query_params(url: str) -> str:
    """Replace the value of any known credential-shaped query parameter
    with `REDACTED`, preserving every other parameter and the path — used
    before a URL is placed into response metadata or an error message."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    redacted_pairs = [
        (key, "REDACTED" if key.lower() in _CREDENTIAL_PARAM_NAMES else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(redacted_pairs), parts.fragment))


def _json_depth(value: Any, *, _current: int = 0) -> int:
    if _current > MAX_JSON_DEPTH:
        return _current
    if isinstance(value, dict):
        if not value:
            return _current
        return max(_json_depth(v, _current=_current + 1) for v in value.values())
    if isinstance(value, list):
        if not value:
            return _current
        return max(_json_depth(v, _current=_current + 1) for v in value)
    return _current


def _parse_retry_after(header_value: str | None, *, now: float) -> float | None:
    """Returns a bounded, non-negative wait in seconds, or None if absent/
    invalid. Never trusts an unbounded or negative provider-supplied value."""
    if not header_value:
        return None
    header_value = header_value.strip()
    if header_value.isdigit():
        wait = float(header_value)
    else:
        try:
            when = parsedate_to_datetime(header_value)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        wait = when.timestamp() - now
    if wait != wait:  # NaN guard, defensive
        return None
    return max(0.0, min(wait, _MAX_RETRY_AFTER_SECONDS))


@dataclass(frozen=True)
class HttpResponseMeta:
    status_code: int
    latency_ms: int
    retrieved_at: float
    request_url: str
    attempt_count: int
    rate_limited: bool


@dataclass
class HttpJsonClient:
    base_headers: Mapping[str, str]
    rate_limiter: MinIntervalRateLimiter
    max_attempts: int = 2
    timeout_seconds: float = 30.0
    transport: httpx.BaseTransport | None = None
    clock: Callable[[], float] = time.monotonic
    wall_clock: Callable[[], float] = time.time
    provider: str = "unknown"
    backoff_base_seconds: float = 0.25
    backoff_max_seconds: float = 8.0
    backoff_sleep_fn: Callable[[float], None] = time.sleep
    # Invoked once per get_json() call (success or final failure) with a
    # plain dict shaped like `persistence.ProviderRequestRecord`'s fields
    # minus `requested_as_of` (the caller doesn't know that at this layer) —
    # callers with a database connection persist it (docs/milestone-6.md
    # Step 5). Kept as a callback, not a direct storage import, so this
    # module stays framework-neutral and independently testable.
    on_response: Callable[[dict], None] | None = None
    _client: httpx.Client | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

    def __enter__(self) -> "HttpJsonClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                headers=dict(self.base_headers), timeout=self.timeout_seconds, transport=self.transport,
            )
        return self._client

    def _read_bounded(self, response: httpx.Response, url: str) -> bytes:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                response.close()
                raise ResponseTooLargeError(
                    f"{redact_credential_query_params(url)}: response exceeded {MAX_RESPONSE_BYTES} bytes"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def get_json(
        self, url: str, *, params: Mapping[str, Any] | None = None, operation: str = "unknown", symbol: str = "",
    ) -> tuple[Any, HttpResponseMeta]:
        last_exc: Exception | None = None
        last_transport_category = TRANSPORT_NONE
        rate_limited = False
        client = self._get_client()
        for attempt in range(1, self.max_attempts + 1):
            self.rate_limiter.acquire()
            start = self.clock()
            try:
                with client.stream("GET", url, params=params) as response:
                    body = self._read_bounded(response, url)
            except ResponseTooLargeError as exc:
                last_exc = exc
                last_transport_category = TRANSPORT_PROTOCOL_ERROR
                continue
            except httpx.TimeoutException as exc:
                last_exc = ProviderRequestError(
                    f"request to {redact_credential_query_params(url)} timed out", retryable=True
                )
                last_transport_category = TRANSPORT_TIMEOUT
                continue
            except httpx.HTTPError as exc:
                last_transport_category = classify_httpx_transport_exception(exc)
                last_exc = ProviderRequestError(
                    f"request to {redact_credential_query_params(url)} failed ({last_transport_category})",
                    retryable=True,
                )
                continue

            latency_ms = int((self.clock() - start) * 1000)
            safe_url = redact_credential_query_params(str(response.url))
            meta = HttpResponseMeta(
                status_code=response.status_code, latency_ms=latency_ms, retrieved_at=self.wall_clock(),
                request_url=safe_url, attempt_count=attempt, rate_limited=rate_limited,
            )

            if response.status_code == 429:
                rate_limited = True
                last_transport_category = TRANSPORT_RATE_LIMITED
                last_exc = ProviderRateLimitedError(f"{safe_url} rate-limited (429)")
                if attempt < self.max_attempts:
                    self._apply_retry_pacing(response, attempt)
                continue
            if response.status_code in _RETRYABLE_STATUS_CODES:
                last_transport_category = TRANSPORT_HTTP_SERVER_ERROR
                last_exc = ProviderRequestError(
                    f"{safe_url} returned retryable status {response.status_code}", retryable=True,
                    status_code=response.status_code,
                )
                if attempt < self.max_attempts:
                    self._apply_retry_pacing(response, attempt)
                continue
            if response.status_code >= 400:
                category = (
                    TRANSPORT_AUTHENTICATION_FAILURE
                    if response.status_code in (401, 403) else TRANSPORT_HTTP_CLIENT_ERROR
                )
                self._notify(
                    operation, symbol, meta, success=False, error_code="ProviderRequestError", retryable=False,
                    retry_count=attempt - 1, transport_failure_category=category,
                )
                # Never include raw response body text in a raised/persisted
                # error message (Part 24) — status code only.
                raise ProviderRequestError(
                    f"{safe_url} returned non-retryable status {response.status_code}",
                    retryable=False, status_code=response.status_code,
                )

            try:
                parsed = json.loads(body)
            except ValueError as exc:
                self._notify(
                    operation, symbol, meta, success=False, error_code="MalformedProviderResponseError",
                    retryable=False, retry_count=attempt - 1,
                    transport_failure_category=TRANSPORT_PROTOCOL_ERROR,
                )
                raise MalformedProviderResponseError(f"{safe_url} returned a non-JSON body") from exc

            if _json_depth(parsed) > MAX_JSON_DEPTH:
                self._notify(
                    operation, symbol, meta, success=False, error_code="MalformedProviderResponseError",
                    retryable=False, retry_count=attempt - 1,
                    transport_failure_category=TRANSPORT_PROTOCOL_ERROR,
                )
                raise MalformedProviderResponseError(f"{safe_url} returned JSON nested deeper than {MAX_JSON_DEPTH} levels")

            self._notify(
                operation, symbol, meta, success=True, error_code=None, retryable=None, retry_count=attempt - 1,
                transport_failure_category=TRANSPORT_NONE,
            )
            return parsed, meta

        assert last_exc is not None
        self._notify(
            operation, symbol, None, success=False, error_code=type(last_exc).__name__,
            retryable=getattr(last_exc, "retryable", True), retry_count=self.max_attempts - 1,
            transport_failure_category=last_transport_category,
        )
        raise RetryBoundExceededError(f"{redact_credential_query_params(url)}: exhausted {self.max_attempts} attempt(s): {last_exc}") from last_exc

    def _apply_retry_pacing(self, response: httpx.Response, attempt: int) -> None:
        """Retry-After (if present and valid) plus exponential backoff — both
        applied via the injectable `backoff_sleep_fn`, never a busy loop, and
        both bounded so a hostile/broken provider header cannot force an
        effectively infinite wait."""
        retry_after = _parse_retry_after(response.headers.get("Retry-After"), now=self.wall_clock())
        backoff = min(self.backoff_base_seconds * (2 ** (attempt - 1)), self.backoff_max_seconds)
        wait = max(retry_after or 0.0, backoff)
        if wait > 0:
            self.backoff_sleep_fn(wait)

    def _notify(
        self, operation: str, symbol: str, meta: HttpResponseMeta | None, *, success: bool,
        error_code: str | None, retryable: bool | None, retry_count: int,
        transport_failure_category: str,
    ) -> None:
        if self.on_response is None:
            return
        self.on_response({
            "provider": self.provider, "operation": operation, "symbol": symbol,
            "retrieved_at": self.wall_clock(), "http_status": meta.status_code if meta else None,
            "cache_status": "MISS", "rate_limited": bool(meta.rate_limited) if meta else True,
            "retry_count": retry_count, "latency_ms": meta.latency_ms if meta else None,
            "success": success, "error_code": error_code, "retryable": retryable,
            "transport_failure_category": transport_failure_category,
        })
