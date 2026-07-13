"""Thin, injectable-transport HTTP JSON client shared by the real provider
clients in this package (docs/milestone-6.md Steps 6-7). Bounded retry only
(no infinite retry), rate-limited via `rate_limits.MinIntervalRateLimiter`,
and every call returns latency/status/retrieval metadata so callers can
persist it (Step 5) without re-deriving it later.

`transport` is always injectable (`httpx.MockTransport` in tests) — mirrors
how `runtime/client/process_client.py::RuntimeClient` injects its transport
in Milestone 4, so the default test suite never opens a real socket.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import httpx

from .errors import MalformedProviderResponseError, ProviderRateLimitedError, ProviderRequestError, RetryBoundExceededError
from .rate_limits import MinIntervalRateLimiter

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


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
    # Invoked once per get_json() call (success or final failure) with a
    # plain dict shaped like `persistence.ProviderRequestRecord`'s fields
    # minus `requested_as_of` (the caller doesn't know that at this layer) —
    # callers with a database connection persist it (docs/milestone-6.md
    # Step 5). Kept as a callback, not a direct storage import, so this
    # module stays framework-neutral and independently testable.
    on_response: Callable[[dict], None] | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

    def get_json(
        self, url: str, *, params: Mapping[str, Any] | None = None, operation: str = "unknown", symbol: str = "",
    ) -> tuple[Any, HttpResponseMeta]:
        last_exc: Exception | None = None
        rate_limited = False
        for attempt in range(1, self.max_attempts + 1):
            self.rate_limiter.acquire()
            start = self.clock()
            try:
                with httpx.Client(headers=dict(self.base_headers), timeout=self.timeout_seconds, transport=self.transport) as client:
                    response = client.get(url, params=params)
            except httpx.TimeoutException as exc:
                last_exc = ProviderRequestError(f"request to {url} timed out: {exc}", retryable=True)
                continue
            except httpx.HTTPError as exc:
                last_exc = ProviderRequestError(f"request to {url} failed: {exc}", retryable=True)
                continue

            latency_ms = int((self.clock() - start) * 1000)
            meta = HttpResponseMeta(
                status_code=response.status_code, latency_ms=latency_ms, retrieved_at=self.wall_clock(),
                request_url=str(response.url), attempt_count=attempt, rate_limited=rate_limited,
            )

            if response.status_code == 429:
                rate_limited = True
                last_exc = ProviderRateLimitedError(f"{url} rate-limited (429)")
                continue
            if response.status_code in _RETRYABLE_STATUS_CODES:
                last_exc = ProviderRequestError(
                    f"{url} returned retryable status {response.status_code}", retryable=True,
                    status_code=response.status_code,
                )
                continue
            if response.status_code >= 400:
                self._notify(operation, symbol, meta, success=False, error_code="ProviderRequestError", retryable=False, retry_count=attempt - 1)
                raise ProviderRequestError(
                    f"{url} returned non-retryable status {response.status_code}: {response.text[:500]}",
                    retryable=False, status_code=response.status_code,
                )

            try:
                parsed = response.json()
            except ValueError as exc:
                self._notify(operation, symbol, meta, success=False, error_code="MalformedProviderResponseError", retryable=False, retry_count=attempt - 1)
                raise MalformedProviderResponseError(f"{url} returned non-JSON body: {exc}") from exc

            self._notify(operation, symbol, meta, success=True, error_code=None, retryable=None, retry_count=attempt - 1)
            return parsed, meta

        assert last_exc is not None
        self._notify(
            operation, symbol, None, success=False, error_code=type(last_exc).__name__,
            retryable=getattr(last_exc, "retryable", True), retry_count=self.max_attempts - 1,
        )
        raise RetryBoundExceededError(f"{url}: exhausted {self.max_attempts} attempt(s): {last_exc}") from last_exc

    def _notify(
        self, operation: str, symbol: str, meta: HttpResponseMeta | None, *, success: bool,
        error_code: str | None, retryable: bool | None, retry_count: int,
    ) -> None:
        if self.on_response is None:
            return
        self.on_response({
            "provider": self.provider, "operation": operation, "symbol": symbol,
            "retrieved_at": self.wall_clock(), "http_status": meta.status_code if meta else None,
            "cache_status": "MISS", "rate_limited": bool(meta.rate_limited) if meta else True,
            "retry_count": retry_count, "latency_ms": meta.latency_ms if meta else None,
            "success": success, "error_code": error_code, "retryable": retryable,
        })
