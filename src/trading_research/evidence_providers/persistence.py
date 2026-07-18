"""Persists real evidence-provider request/response metadata
(docs/milestone-6.md Step 5) into `storage/evidence_provider_schema.py`'s
`evidence_provider_requests` table.

Never persists: API keys, authorization headers, signed URLs, account
identifiers. Raw payloads are only stored when `licensing_classification`
says it is safe (`PUBLIC_DOMAIN` — SEC EDGAR only in this milestone) and are
capped at `MAX_RAW_PAYLOAD_BYTES`; anything else persists normalized data,
a content hash, and a source locator only, per docs/milestone-6.md Step 5's
"If provider terms do not permit raw response persistence."
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

MAX_RAW_PAYLOAD_BYTES = 200_000

LICENSE_PUBLIC_DOMAIN = "PUBLIC_DOMAIN"       # SEC EDGAR
LICENSE_ACCOUNT_LINKED = "ACCOUNT_LINKED"     # Alpaca market data (this account's own use only)
LICENSE_RESTRICTED = "RESTRICTED"             # anything not yet clsasified as safe to store raw

CACHE_HIT = "HIT"
CACHE_MISS = "MISS"
CACHE_BYPASS = "BYPASS"  # caching not applicable to this operation

CORRELATION_LEGACY_MANUAL = "LEGACY_MANUAL"
CORRELATION_MANUAL = "MANUAL"
CORRELATION_RESEARCH_CYCLE = "RESEARCH_CYCLE"
CORRELATION_SCHEDULED = "SCHEDULED"
CORRELATION_MODES = (
    CORRELATION_LEGACY_MANUAL, CORRELATION_MANUAL, CORRELATION_RESEARCH_CYCLE, CORRELATION_SCHEDULED,
)


@dataclass(frozen=True)
class ProviderRequestContext:
    correlation_mode: str = CORRELATION_MANUAL
    research_cycle_id: str | None = None
    scheduler_run_id: str | None = None
    research_run_id: str | None = None
    symbol_attempt_id: str | None = None
    provider_request_group_id: str | None = None


_REQUEST_CONTEXT: ContextVar[ProviderRequestContext] = ContextVar(
    "evidence_provider_request_context", default=ProviderRequestContext()
)


def current_provider_request_context() -> ProviderRequestContext:
    return _REQUEST_CONTEXT.get()


@contextmanager
def provider_request_context(**overrides: str | None) -> Iterator[ProviderRequestContext]:
    """Nest immutable request ownership without coupling provider adapters to the scheduler.

    Inner cycle/symbol contexts inherit the outer scheduler-run identity. ContextVars
    keep overlapping threads/tasks isolated, and the persistence boundary still
    validates scheduled rows rather than trusting ambient context blindly.
    """
    current = current_provider_request_context()
    values = {
        "correlation_mode": current.correlation_mode,
        "research_cycle_id": current.research_cycle_id,
        "scheduler_run_id": current.scheduler_run_id,
        "research_run_id": current.research_run_id,
        "symbol_attempt_id": current.symbol_attempt_id,
        "provider_request_group_id": current.provider_request_group_id,
    }
    values.update({key: value for key, value in overrides.items() if value is not None})
    context = ProviderRequestContext(**values)
    token = _REQUEST_CONTEXT.set(context)
    try:
        yield context
    finally:
        _REQUEST_CONTEXT.reset(token)


@dataclass(frozen=True)
class ProviderRequestRecord:
    provider: str
    operation: str
    symbol: str
    requested_as_of: datetime
    retrieved_at: datetime
    provider_response_timestamp: datetime | None
    http_status: int | None
    content_hash: str | None
    normalized_record_hash: str | None
    cache_status: str
    rate_limited: bool
    retry_count: int
    latency_ms: int | None
    success: bool
    error_code: str | None
    retryable: bool | None
    licensing_classification: str
    raw_payload: dict | list | None = None
    correlation_mode: str | None = None
    research_cycle_id: str | None = None
    scheduler_run_id: str | None = None
    research_run_id: str | None = None
    symbol_attempt_id: str | None = None
    provider_request_group_id: str | None = None
    transport_failure_category: str = "NONE"


def save_provider_request(conn: sqlite3.Connection, record: ProviderRequestRecord) -> str:
    request_id = str(uuid.uuid4())
    raw_payload_json = None
    raw_payload_stored = False
    if record.raw_payload is not None and record.licensing_classification == LICENSE_PUBLIC_DOMAIN:
        serialized = json.dumps(record.raw_payload)
        if len(serialized.encode()) <= MAX_RAW_PAYLOAD_BYTES:
            raw_payload_json = serialized
            raw_payload_stored = True

    context = current_provider_request_context()
    correlation_mode = record.correlation_mode or context.correlation_mode
    research_cycle_id = record.research_cycle_id or context.research_cycle_id
    scheduler_run_id = record.scheduler_run_id or context.scheduler_run_id
    research_run_id = record.research_run_id or context.research_run_id
    symbol_attempt_id = record.symbol_attempt_id or context.symbol_attempt_id
    provider_request_group_id = record.provider_request_group_id or context.provider_request_group_id
    if correlation_mode not in CORRELATION_MODES:
        raise ValueError(f"unknown provider request correlation mode {correlation_mode!r}")
    if correlation_mode == CORRELATION_SCHEDULED and (not research_cycle_id or not scheduler_run_id):
        raise ValueError("scheduled provider requests require research_cycle_id and scheduler_run_id")
    if correlation_mode == CORRELATION_RESEARCH_CYCLE and not research_cycle_id:
        raise ValueError("research-cycle provider requests require research_cycle_id")

    conn.execute(
        "INSERT INTO evidence_provider_requests "
        "(request_id, provider, operation, symbol, requested_as_of, retrieved_at, "
        "provider_response_timestamp, http_status, content_hash, normalized_record_hash, "
        "cache_status, rate_limited, retry_count, latency_ms, success, error_code, retryable, "
        "licensing_classification, raw_payload_stored, raw_payload_json, correlation_mode, "
        "research_cycle_id, scheduler_run_id, research_run_id, symbol_attempt_id, provider_request_group_id, "
        "transport_failure_category, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            request_id, record.provider, record.operation, record.symbol.upper(),
            record.requested_as_of.isoformat(), record.retrieved_at.isoformat(),
            record.provider_response_timestamp.isoformat() if record.provider_response_timestamp else None,
            record.http_status, record.content_hash, record.normalized_record_hash,
            record.cache_status, int(record.rate_limited), record.retry_count, record.latency_ms,
            int(record.success), record.error_code,
            None if record.retryable is None else int(record.retryable),
            record.licensing_classification, int(raw_payload_stored), raw_payload_json,
            correlation_mode, research_cycle_id, scheduler_run_id, research_run_id, symbol_attempt_id,
            provider_request_group_id, record.transport_failure_category,
            record.retrieved_at.isoformat(),
        ),
    )
    conn.commit()
    return request_id


def list_provider_requests(conn: sqlite3.Connection, *, provider: str | None = None) -> list[dict]:
    if provider:
        rows = conn.execute(
            "SELECT * FROM evidence_provider_requests WHERE provider = ? ORDER BY created_at", (provider,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM evidence_provider_requests ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def list_provider_requests_in_window(
    conn: sqlite3.Connection, *, symbols: tuple[str, ...], window_start_iso: str, window_end_iso: str,
) -> list[dict]:
    """Milestone 11.3.1 Item 8 Part A: the authoritative provider-request
    telemetry for one research cycle — every persisted request row for the
    cycle's own symbol set whose `created_at` falls within the cycle's own
    [start, end) wall-clock window. This is the real per-request count
    `shadow/scheduler.py` uses in place of `symbols_attempted` (one symbol
    can produce zero, one, or many provider requests/retries)."""
    if not symbols:
        return []
    placeholders = ",".join("?" for _ in symbols)
    rows = conn.execute(
        f"SELECT * FROM evidence_provider_requests WHERE symbol IN ({placeholders}) "
        "AND created_at >= ? AND created_at < ? ORDER BY created_at",
        (*symbols, window_start_iso, window_end_iso),
    ).fetchall()
    return [dict(r) for r in rows]


def list_provider_requests_for_cycle(conn: sqlite3.Connection, research_cycle_id: str) -> list[dict]:
    """Exact immutable ownership query. Legacy uncorrelated rows never match."""
    rows = conn.execute(
        "SELECT * FROM evidence_provider_requests WHERE research_cycle_id = ? "
        "ORDER BY created_at, request_id",
        (research_cycle_id,),
    ).fetchall()
    return [dict(row) for row in rows]
