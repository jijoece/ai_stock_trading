"""Evidence acquisition and deterministic snapshot hashing (docs/milestone-5.md
Steps 4-5). Evidence providers are small Protocols (Step 5) rather than one
unrestricted research tool; the first vertical slice is fixture-backed
(`fixtures.py`), but any real external provider plugs in through the same
Protocol without changing `build_evidence_snapshot`.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol, Sequence

from .errors import EvidenceValidationError
from .models import EvidenceItem, EvidenceSnapshot, SourceRecord

DEFAULT_MAX_EVIDENCE_ITEMS = 100
DEFAULT_MAX_ITEMS_PER_CATEGORY = 25


@dataclass(frozen=True)
class EvidenceBundle:
    """What one evidence provider contributes to a snapshot build."""

    source_records: tuple[SourceRecord, ...] = ()
    evidence_items: tuple[EvidenceItem, ...] = ()
    missing_data_reasons: tuple[str, ...] = ()
    conflict_reasons: tuple[str, ...] = ()


class FundamentalsEvidenceProvider(Protocol):
    def fetch(self, symbol: str, as_of: datetime) -> EvidenceBundle: ...


class MarketEvidenceProvider(Protocol):
    def fetch(self, symbol: str, as_of: datetime) -> EvidenceBundle: ...


class NewsEvidenceProvider(Protocol):
    def fetch(self, symbol: str, as_of: datetime) -> EvidenceBundle: ...


class SentimentEvidenceProvider(Protocol):
    def fetch(self, symbol: str, as_of: datetime) -> EvidenceBundle: ...


class FilingEvidenceProvider(Protocol):
    def fetch(self, symbol: str, as_of: datetime) -> EvidenceBundle: ...


class PortfolioContextProvider(Protocol):
    def fetch(self, symbol: str, as_of: datetime) -> Mapping[str, Any] | None: ...


def _canonical_source_record(sr: SourceRecord) -> dict:
    return {
        "source_id": sr.source_id,
        "source_type": sr.source_type,
        "provider": sr.provider,
        "source_locator": sr.source_locator,
        "retrieved_at": sr.retrieved_at.isoformat(),
        "published_at": sr.published_at.isoformat() if sr.published_at else None,
        "effective_at": sr.effective_at.isoformat() if sr.effective_at else None,
        "available_at": sr.available_at.isoformat() if sr.available_at else None,
        "content_hash": sr.content_hash,
        "status": sr.status,
        "is_stale": sr.is_stale,
        "point_in_time_safe": sr.point_in_time_safe,
        "error_code": sr.error_code,
        "metadata": dict(sr.metadata),
    }


def _canonical_evidence_item(ei: EvidenceItem) -> dict:
    return {
        "evidence_id": ei.evidence_id,
        "source_id": ei.source_id,
        "category": ei.category,
        "title": ei.title,
        "summary": ei.summary,
        "normalized_values": dict(ei.normalized_values),
        "as_of": ei.as_of.isoformat(),
        "confidence": ei.confidence,
        "stale": ei.stale,
        "conflict_group": ei.conflict_group,
    }


def canonical_snapshot_payload(
    *,
    symbol: str,
    as_of: datetime,
    source_records: Sequence[SourceRecord],
    evidence_items: Sequence[EvidenceItem],
    deterministic_factors: Mapping[str, float],
    sentiment_metrics: Mapping[str, Any],
    portfolio_context: Mapping[str, Any] | None,
    missing_data_reasons: Sequence[str],
    conflict_reasons: Sequence[str],
    point_in_time_safe: bool,
    config_hash: str,
    git_sha: str,
) -> dict:
    """Canonical, deterministically-ordered JSON-able payload used to derive
    `snapshot_id`. Deliberately excludes `created_at` (wall-clock persistence
    time is not "content") so rebuilding a snapshot from the same inputs on
    a different day still reuses the same snapshot_id — required for the
    orchestrator's "reuse an existing completed run" idempotency check."""
    return {
        "symbol": symbol,
        "as_of": as_of.isoformat(),
        "source_records": sorted(
            (_canonical_source_record(s) for s in source_records), key=lambda d: d["source_id"]
        ),
        "evidence_items": sorted(
            (_canonical_evidence_item(e) for e in evidence_items), key=lambda d: d["evidence_id"]
        ),
        "deterministic_factors": dict(sorted(deterministic_factors.items())),
        "sentiment_metrics": dict(sorted(sentiment_metrics.items())),
        "portfolio_context": dict(sorted(portfolio_context.items())) if portfolio_context else None,
        "missing_data_reasons": sorted(missing_data_reasons),
        "conflict_reasons": sorted(conflict_reasons),
        "point_in_time_safe": point_in_time_safe,
        "config_hash": config_hash,
        "git_sha": git_sha,
    }


def compute_snapshot_id(payload: dict) -> str:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return f"snap-{digest[:32]}"


def _apply_limits(
    items: Sequence[EvidenceItem], max_total: int, max_per_category: int
) -> tuple[EvidenceItem, ...]:
    """Deterministic truncation: stable-sort by (category, evidence_id) so the
    same oversized input always drops the same items, in the same order,
    every run (docs/milestone-5.md Step 6: "deterministic truncation")."""
    ordered = sorted(items, key=lambda i: (i.category, i.evidence_id))
    per_category: dict[str, int] = {}
    kept: list[EvidenceItem] = []
    for item in ordered:
        count = per_category.get(item.category, 0)
        if count >= max_per_category:
            continue
        per_category[item.category] = count + 1
        kept.append(item)
    return tuple(kept[:max_total])


def build_evidence_snapshot(
    symbol: str,
    as_of: datetime,
    *,
    deterministic_factors: Mapping[str, float],
    sentiment_metrics: Mapping[str, Any],
    providers: Sequence[
        FundamentalsEvidenceProvider
        | MarketEvidenceProvider
        | NewsEvidenceProvider
        | SentimentEvidenceProvider
        | FilingEvidenceProvider
    ],
    portfolio_context_provider: PortfolioContextProvider | None,
    config_hash: str,
    git_sha: str,
    clock: Callable[[], datetime],
    max_evidence_items: int = DEFAULT_MAX_EVIDENCE_ITEMS,
    max_items_per_source_category: int = DEFAULT_MAX_ITEMS_PER_CATEGORY,
) -> EvidenceSnapshot:
    """Assemble one immutable, point-in-time evidence snapshot.

    Every provider is queried independently; a single provider raising is
    not caught here — callers that want partial-availability behavior must
    have their provider implementation return an `EvidenceBundle` with a
    `missing_data_reasons` entry instead of raising (mirrors the screener's
    "fail closed per gate, not per run" philosophy).
    """
    all_source_records: list[SourceRecord] = []
    all_evidence_items: list[EvidenceItem] = []
    missing: list[str] = []
    conflicts: list[str] = []

    for provider in providers:
        bundle = provider.fetch(symbol, as_of)
        all_source_records.extend(bundle.source_records)
        all_evidence_items.extend(bundle.evidence_items)
        missing.extend(bundle.missing_data_reasons)
        conflicts.extend(bundle.conflict_reasons)

    limited_items = _apply_limits(all_evidence_items, max_evidence_items, max_items_per_source_category)

    portfolio_context = portfolio_context_provider.fetch(symbol, as_of) if portfolio_context_provider else None

    point_in_time_safe = all(sr.point_in_time_safe for sr in all_source_records) and all(
        sr.available_at is None or sr.available_at <= as_of for sr in all_source_records
    )

    payload = canonical_snapshot_payload(
        symbol=symbol,
        as_of=as_of,
        source_records=all_source_records,
        evidence_items=limited_items,
        deterministic_factors=deterministic_factors,
        sentiment_metrics=sentiment_metrics,
        portfolio_context=portfolio_context,
        missing_data_reasons=missing,
        conflict_reasons=conflicts,
        point_in_time_safe=point_in_time_safe,
        config_hash=config_hash,
        git_sha=git_sha,
    )
    snapshot_id = compute_snapshot_id(payload)

    return EvidenceSnapshot(
        snapshot_id=snapshot_id,
        symbol=symbol,
        as_of=as_of,
        created_at=clock(),
        source_records=tuple(all_source_records),
        evidence_items=limited_items,
        deterministic_factors=dict(deterministic_factors),
        sentiment_metrics=dict(sentiment_metrics),
        portfolio_context=portfolio_context,
        missing_data_reasons=tuple(missing),
        conflict_reasons=tuple(conflicts),
        point_in_time_safe=point_in_time_safe,
        config_hash=config_hash,
        git_sha=git_sha,
    )


# --- Round-trip (de)serialization, shared by storage/research_repositories.py ---
# Reuses the same canonical dict shape used for hashing, so a persisted row
# and a freshly-built snapshot are byte-for-byte comparable.

def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def source_record_to_dict(sr: SourceRecord) -> dict:
    return _canonical_source_record(sr)


def source_record_from_dict(data: dict) -> SourceRecord:
    return SourceRecord(
        source_id=data["source_id"], source_type=data["source_type"], provider=data["provider"],
        source_locator=data["source_locator"], retrieved_at=_parse_dt(data["retrieved_at"]),
        published_at=_parse_dt(data["published_at"]), effective_at=_parse_dt(data["effective_at"]),
        available_at=_parse_dt(data["available_at"]), content_hash=data["content_hash"], status=data["status"],
        is_stale=data["is_stale"], point_in_time_safe=data["point_in_time_safe"], error_code=data["error_code"],
        metadata=data.get("metadata") or {},
    )


def evidence_item_to_dict(ei: EvidenceItem) -> dict:
    return _canonical_evidence_item(ei)


def evidence_item_from_dict(data: dict) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=data["evidence_id"], source_id=data["source_id"], category=data["category"],
        title=data["title"], summary=data["summary"], normalized_values=data.get("normalized_values") or {},
        as_of=_parse_dt(data["as_of"]), confidence=data["confidence"], stale=data["stale"],
        conflict_group=data.get("conflict_group"),
    )


def snapshot_to_row(snapshot: EvidenceSnapshot) -> dict:
    """Flat dict matching `storage/research_schema.py`'s
    `research_evidence_snapshots` columns (JSON-encoded sub-structures)."""
    return {
        "snapshot_id": snapshot.snapshot_id,
        "symbol": snapshot.symbol,
        "as_of": snapshot.as_of.isoformat(),
        "created_at": snapshot.created_at.isoformat(),
        "source_records_json": json.dumps([source_record_to_dict(s) for s in snapshot.source_records]),
        "evidence_items_json": json.dumps([evidence_item_to_dict(e) for e in snapshot.evidence_items]),
        "deterministic_factors_json": json.dumps(dict(snapshot.deterministic_factors)),
        "sentiment_metrics_json": json.dumps(dict(snapshot.sentiment_metrics)),
        "portfolio_context_json": json.dumps(dict(snapshot.portfolio_context)) if snapshot.portfolio_context else None,
        "missing_data_reasons_json": json.dumps(list(snapshot.missing_data_reasons)),
        "conflict_reasons_json": json.dumps(list(snapshot.conflict_reasons)),
        "point_in_time_safe": int(snapshot.point_in_time_safe),
        "config_hash": snapshot.config_hash,
        "git_sha": snapshot.git_sha,
    }


def snapshot_from_row(row: Mapping[str, Any]) -> EvidenceSnapshot:
    return EvidenceSnapshot(
        snapshot_id=row["snapshot_id"], symbol=row["symbol"], as_of=_parse_dt(row["as_of"]),
        created_at=_parse_dt(row["created_at"]),
        source_records=tuple(source_record_from_dict(d) for d in json.loads(row["source_records_json"])),
        evidence_items=tuple(evidence_item_from_dict(d) for d in json.loads(row["evidence_items_json"])),
        deterministic_factors=json.loads(row["deterministic_factors_json"]),
        sentiment_metrics=json.loads(row["sentiment_metrics_json"]),
        portfolio_context=json.loads(row["portfolio_context_json"]) if row["portfolio_context_json"] else None,
        missing_data_reasons=tuple(json.loads(row["missing_data_reasons_json"])),
        conflict_reasons=tuple(json.loads(row["conflict_reasons_json"])),
        point_in_time_safe=bool(row["point_in_time_safe"]),
        config_hash=row["config_hash"], git_sha=row["git_sha"],
    )
