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
from dataclasses import dataclass
from datetime import datetime

MAX_RAW_PAYLOAD_BYTES = 200_000

LICENSE_PUBLIC_DOMAIN = "PUBLIC_DOMAIN"       # SEC EDGAR
LICENSE_ACCOUNT_LINKED = "ACCOUNT_LINKED"     # Alpaca market data (this account's own use only)
LICENSE_RESTRICTED = "RESTRICTED"             # anything not yet clsasified as safe to store raw

CACHE_HIT = "HIT"
CACHE_MISS = "MISS"
CACHE_BYPASS = "BYPASS"  # caching not applicable to this operation


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


def save_provider_request(conn: sqlite3.Connection, record: ProviderRequestRecord) -> str:
    request_id = str(uuid.uuid4())
    raw_payload_json = None
    raw_payload_stored = False
    if record.raw_payload is not None and record.licensing_classification == LICENSE_PUBLIC_DOMAIN:
        serialized = json.dumps(record.raw_payload)
        if len(serialized.encode()) <= MAX_RAW_PAYLOAD_BYTES:
            raw_payload_json = serialized
            raw_payload_stored = True

    conn.execute(
        "INSERT INTO evidence_provider_requests "
        "(request_id, provider, operation, symbol, requested_as_of, retrieved_at, "
        "provider_response_timestamp, http_status, content_hash, normalized_record_hash, "
        "cache_status, rate_limited, retry_count, latency_ms, success, error_code, retryable, "
        "licensing_classification, raw_payload_stored, raw_payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            request_id, record.provider, record.operation, record.symbol.upper(),
            record.requested_as_of.isoformat(), record.retrieved_at.isoformat(),
            record.provider_response_timestamp.isoformat() if record.provider_response_timestamp else None,
            record.http_status, record.content_hash, record.normalized_record_hash,
            record.cache_status, int(record.rate_limited), record.retry_count, record.latency_ms,
            int(record.success), record.error_code,
            None if record.retryable is None else int(record.retryable),
            record.licensing_classification, int(raw_payload_stored), raw_payload_json,
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
