"""Authoritative provider-provenance classification (Milestone 9.2,
docs/milestone-9.2.md Sections 1-4).

Replaces `shadow_run_summaries.cost_usd > 0` — a real accrued cost proves
spending, not identity — with an explicit, persisted record of which
provider category (evidence-adapter categories, plus Claude) actually
produced each research cycle's evidence/decision, sourced from data this
repository already treats as authoritative:

* evidence categories: `research_cycles.provider_mode` (Milestone 6) is a
  single, whole-cycle "fixture"/"real" flag. `cli.py::
  _build_evidence_provider_registry` never mixes a fixture raw client into a
  "real" mode cycle (each category is either a real client or entirely
  absent) — so, combined with which categories a symbol's evidence snapshot
  actually populated, this is sufficient to classify each evidence category
  without adding a second provider-request-level table.
* Claude: `research_committee_runs.provider`/`research_attempts.provider`
  (Milestone 5) already records the real taxonomy
  (`"anthropic"` = real, `"deterministic"`/`"scripted"` = fixture/scripted —
  `shadow/budget.py::REAL_CLAUDE_PROVIDER`/`PRICING_EXEMPT_PROVIDERS`), never
  conflated with cost.

`record_cycle_provider_provenance` persists one row per (cycle_id, symbol,
provider_category) actually observed — never a category that wasn't
present, never a guess for missing metadata. `classify_cycle`/
`compute_real_provider_history` are pure aggregation over those persisted
facts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..utc import TimestampError, canonical_utc, parse_aware_utc

# Deferred import (inside functions below): `storage.research_cycle_repositories`
# itself imports `SymbolCycleResult` from this package's `scheduled_cycle`
# module, which imports this module — a module-level import here would be
# circular.

CLASSIFICATION_VERSION = "provider-provenance/v2"

# Evidence-category source types this module classifies (mirrors
# `research/models.py::SourceRecord.source_type` values produced by
# `evidence_providers/evidence_adapters.py`). "claude" is a distinct,
# non-evidence category tracked separately below.
EVIDENCE_PROVIDER_CATEGORIES = ("fundamentals", "filing", "market", "news", "sentiment", "corporate_status")
CLAUDE_PROVIDER_CATEGORY = "claude"

PROVIDER_MODE_FIXTURE = "FIXTURE"
PROVIDER_MODE_REAL = "REAL"
PROVIDER_MODE_UNKNOWN = "UNKNOWN"

# Claude provider-name taxonomy (research/configuration.py::KNOWN_PROVIDERS,
# shadow/budget.py::REAL_CLAUDE_PROVIDER/PRICING_EXEMPT_PROVIDERS) — the
# axis this module reuses verbatim rather than re-deriving.
_REAL_CLAUDE_PROVIDER_NAMES = ("anthropic", "claude_code", "codex")
_FIXTURE_CLAUDE_PROVIDER_NAMES = ("deterministic", "scripted")


class ProviderProvenanceClassification(str, Enum):
    FIXTURE_ONLY = "FIXTURE_ONLY"
    REAL_EVIDENCE_ONLY = "REAL_EVIDENCE_ONLY"
    REAL_CLAUDE_ONLY = "REAL_CLAUDE_ONLY"
    REAL_EVIDENCE_AND_CLAUDE = "REAL_EVIDENCE_AND_CLAUDE"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class ProviderOutcome(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    ATTEMPTED = "ATTEMPTED"
    UNKNOWN = "UNKNOWN"


SUCCESSFUL_OUTCOMES = (ProviderOutcome.SUCCEEDED,)
FAILURE_OUTCOMES = (ProviderOutcome.FAILED, ProviderOutcome.SOURCE_UNAVAILABLE)

_EVIDENCE_SUCCESS = {"ok", "success", "succeeded", "complete", "completed", "available"}
_EVIDENCE_PARTIAL = {"partial", "partially_complete", "incomplete", "stale"}
_EVIDENCE_UNAVAILABLE = {"missing", "source_unavailable", "unavailable", "not_available"}
_EVIDENCE_FAILED = {
    "error", "failed", "failure", "timeout", "timed_out", "invalid", "invalid_response",
    "rate_limited", "exhausted",
}
_CLAUDE_SUCCESS = {"completed", "succeeded", "ok"}
_CLAUDE_PARTIAL = {"analysis_incomplete", "analyst_reports_complete_no_manager", "partial", "partially_complete"}
_CLAUDE_FAILED = {"failed", "failure", "timeout", "timed_out", "exhausted", "invalid_response", "error"}


REAL_CLASSIFICATIONS = (
    ProviderProvenanceClassification.REAL_EVIDENCE_ONLY,
    ProviderProvenanceClassification.REAL_CLAUDE_ONLY,
    ProviderProvenanceClassification.REAL_EVIDENCE_AND_CLAUDE,
    ProviderProvenanceClassification.MIXED,
)


@dataclass(frozen=True)
class ProviderProvenanceSummary:
    as_of: datetime
    policy_version: str
    total_classified_cycles: int
    real_provider_cycle_count: int
    fixture_only_cycle_count: int
    real_evidence_only_cycle_count: int
    real_claude_only_cycle_count: int
    real_evidence_and_claude_cycle_count: int
    mixed_cycle_count: int
    unknown_cycle_count: int
    completed_cycle_count: int = 0
    real_provider_attempt_cycle_count: int = 0
    real_provider_success_cycle_count: int = 0
    real_provider_failure_cycle_count: int = 0
    partial_provider_cycle_count: int = 0
    excluded_partial_cycle_count: int = 0
    excluded_failed_cycle_count: int = 0
    excluded_running_cycle_count: int = 0
    qualifying_real_provider_cycle_count: int = 0


def normalize_evidence_outcome(status: str | None) -> ProviderOutcome:
    value = (status or "").strip().lower()
    if value in _EVIDENCE_SUCCESS:
        return ProviderOutcome.SUCCEEDED
    if value in _EVIDENCE_PARTIAL:
        return ProviderOutcome.PARTIAL
    if value in _EVIDENCE_UNAVAILABLE:
        return ProviderOutcome.SOURCE_UNAVAILABLE
    if value in _EVIDENCE_FAILED:
        return ProviderOutcome.FAILED
    if value in {"attempted", "pending", "running"}:
        return ProviderOutcome.ATTEMPTED
    return ProviderOutcome.UNKNOWN


def normalize_claude_outcome(status: str | None) -> ProviderOutcome:
    value = (status or "").strip().lower()
    if value in _CLAUDE_SUCCESS:
        return ProviderOutcome.SUCCEEDED
    if value in _CLAUDE_PARTIAL:
        return ProviderOutcome.PARTIAL
    if value in _CLAUDE_FAILED:
        return ProviderOutcome.FAILED
    if value in {"attempted", "pending", "running"}:
        return ProviderOutcome.ATTEMPTED
    return ProviderOutcome.UNKNOWN


def aggregate_evidence_status(statuses: list[str]) -> str:
    """Aggregate same-category SourceRecord statuses conservatively."""
    outcomes = {normalize_evidence_outcome(status) for status in statuses}
    if outcomes == {ProviderOutcome.SUCCEEDED}:
        return "ok"
    if ProviderOutcome.SUCCEEDED in outcomes or ProviderOutcome.PARTIAL in outcomes:
        return "partial"
    if ProviderOutcome.FAILED in outcomes:
        return "failed"
    if ProviderOutcome.SOURCE_UNAVAILABLE in outcomes:
        return "source_unavailable"
    if ProviderOutcome.ATTEMPTED in outcomes:
        return "attempted"
    return "unknown"


def _row_outcome(row: dict) -> str:
    stored = row.get("normalized_outcome")
    if stored and stored != ProviderOutcome.UNKNOWN.value:
        return stored
    normalizer = normalize_claude_outcome if row.get("provider_category") == CLAUDE_PROVIDER_CATEGORY else normalize_evidence_outcome
    return normalizer(row.get("status")).value


def evidence_provider_row(
    *, cycle_id: str, research_run_id: str | None, symbol: str, provider_category: str, provider_name: str,
    request_or_source_id: str | None, status: str, cycle_provider_mode: str, observed_at: datetime,
) -> dict:
    """Builds one `research_cycle_provider_provenance` row for an evidence
    category actually present in a symbol's evidence snapshot. `is_real`/
    `is_fixture` come straight from the whole-cycle `provider_mode`
    (`"fixture"`/`"real"`, `research/scheduled_cycle.py::
    PROVIDER_MODE_FIXTURE`/`PROVIDER_MODE_REAL`) — never from `cost_usd`."""
    if cycle_provider_mode == "real":
        mode, is_fixture, is_real = PROVIDER_MODE_REAL, False, True
    elif cycle_provider_mode == "fixture":
        mode, is_fixture, is_real = PROVIDER_MODE_FIXTURE, True, False
    else:
        mode, is_fixture, is_real = PROVIDER_MODE_UNKNOWN, False, False
    return {
        "cycle_id": cycle_id, "research_run_id": research_run_id, "symbol": symbol,
        "provider_category": provider_category, "provider_name": provider_name, "provider_mode": mode,
        "is_fixture": int(is_fixture), "is_real": int(is_real), "request_or_source_id": request_or_source_id,
        "status": status, "normalized_outcome": normalize_evidence_outcome(status).value,
        "observed_at": observed_at.isoformat(), "classification_version": CLASSIFICATION_VERSION,
        "created_at": observed_at.isoformat(),
    }


def claude_provider_row(
    *, cycle_id: str, research_run_id: str, symbol: str, provider_name: str, observed_at: datetime,
    status: str = "COMPLETED",
) -> dict:
    """Builds the `claude` provenance row from `research_provider_name`
    (the actual configured value passed to `analyze_with_research_committee`
    — never `research_cycles.provider_mode`, which is the separate
    evidence-provider axis, per `shadow/scheduler.py`'s own documented
    distinction)."""
    if provider_name in _REAL_CLAUDE_PROVIDER_NAMES:
        mode, is_fixture, is_real = PROVIDER_MODE_REAL, False, True
    elif provider_name in _FIXTURE_CLAUDE_PROVIDER_NAMES:
        mode, is_fixture, is_real = PROVIDER_MODE_FIXTURE, True, False
    else:
        mode, is_fixture, is_real = PROVIDER_MODE_UNKNOWN, False, False
    return {
        "cycle_id": cycle_id, "research_run_id": research_run_id, "symbol": symbol,
        "provider_category": CLAUDE_PROVIDER_CATEGORY, "provider_name": provider_name, "provider_mode": mode,
        "is_fixture": int(is_fixture), "is_real": int(is_real), "request_or_source_id": research_run_id,
        "status": status, "normalized_outcome": normalize_claude_outcome(status).value,
        "observed_at": observed_at.isoformat(), "classification_version": CLASSIFICATION_VERSION,
        "created_at": observed_at.isoformat(),
    }


def record_cycle_provider_provenance(conn, rows: list[dict]) -> int:
    """Persists every row via `save_provider_provenance` (insert-or-ignore,
    immutable) — returns the count actually inserted (0 on a pure replay)."""
    from ..storage import research_cycle_repositories as cycle_repo

    return sum(1 for row in rows if cycle_repo.save_provider_provenance(conn, row))


def _classify_rows(rows: list[dict]) -> ProviderProvenanceClassification:
    if not rows:
        return ProviderProvenanceClassification.UNKNOWN

    # Provider identity alone never invents success. Category classification
    # is based only on rows whose normalized outcome is explicitly SUCCEEDED.
    rows = [r for r in rows if _row_outcome(r) == ProviderOutcome.SUCCEEDED.value]
    if not rows:
        return ProviderProvenanceClassification.UNKNOWN

    evidence_rows = [r for r in rows if r["provider_category"] != CLAUDE_PROVIDER_CATEGORY]
    claude_rows = [r for r in rows if r["provider_category"] == CLAUDE_PROVIDER_CATEGORY]

    if all(r["is_fixture"] for r in rows):
        return ProviderProvenanceClassification.FIXTURE_ONLY

    any_real_evidence = any(r["is_real"] for r in evidence_rows)
    any_real_claude = any(r["is_real"] for r in claude_rows)
    evidence_mixed = any(r["is_fixture"] for r in evidence_rows) and any_real_evidence
    claude_mixed = any(r["is_fixture"] for r in claude_rows) and any_real_claude

    if not any_real_evidence and not any_real_claude:
        # No fixture-only sweep matched above (some row's mode is UNKNOWN)
        # and nothing real was observed either — an honest UNKNOWN rather
        # than a fabricated FIXTURE_ONLY.
        return ProviderProvenanceClassification.UNKNOWN

    if evidence_mixed or claude_mixed:
        return ProviderProvenanceClassification.MIXED
    if any_real_evidence and any_real_claude:
        return ProviderProvenanceClassification.REAL_EVIDENCE_AND_CLAUDE
    if any_real_evidence:
        return ProviderProvenanceClassification.REAL_EVIDENCE_ONLY
    return ProviderProvenanceClassification.REAL_CLAUDE_ONLY


def classify_cycle(conn, cycle_id: str) -> ProviderProvenanceClassification:
    """Deterministic classification for one cycle from its persisted
    `research_cycle_provider_provenance` rows (across every symbol in the
    cycle — a cycle counts once, per Section 4's own "count a cycle once,
    even when it contains several real providers")."""
    from ..storage import research_cycle_repositories as cycle_repo

    rows = cycle_repo.list_provider_provenance_for_cycle(conn, cycle_id)
    return _classify_rows(rows)


def compute_real_provider_history(
    conn, as_of: datetime, *, cycle_ids: set[str] | tuple[str, ...] | list[str] | None = None,
) -> ProviderProvenanceSummary:
    """Aggregate completed cycles at-or-before ``as_of``.

    Only ``research_cycles.status == COMPLETED`` participates in the category
    invariant. Missing provenance is explicitly UNKNOWN. PARTIALLY_COMPLETE,
    FAILED, and still-running cycles are excluded and reported separately.
    The successful-provider floor uses only explicit SUCCEEDED real activity.
    """
    cutoff = canonical_utc(as_of)
    rows = []
    for raw in conn.execute(
        "SELECT p.*, c.as_of AS cycle_as_of FROM research_cycle_provider_provenance p "
        "JOIN research_cycles c ON c.cycle_id = p.cycle_id"
    ).fetchall():
        row = dict(raw)
        try:
            if parse_aware_utc(row["cycle_as_of"]) <= cutoff and parse_aware_utc(row["observed_at"]) <= cutoff:
                rows.append(row)
        except TimestampError:
            continue
    cycles = []
    for raw in conn.execute(
        "SELECT cycle_id, as_of, status, completed_at FROM research_cycles"
    ).fetchall():
        cycle = dict(raw)
        try:
            if parse_aware_utc(cycle["as_of"]) > cutoff:
                continue
            if cycle["status"] == "COMPLETED" and (
                not cycle["completed_at"] or parse_aware_utc(cycle["completed_at"]) > cutoff
            ):
                continue
        except TimestampError:
            continue
        cycles.append(cycle)
    by_cycle: dict[str, list[dict]] = {}
    for row in rows:
        by_cycle.setdefault(row["cycle_id"], []).append(row)

    selected_ids = set(cycle_ids) if cycle_ids is not None else None
    if selected_ids is not None:
        cycles = [c for c in cycles if c["cycle_id"] in selected_ids]
    completed = [c for c in cycles if c["status"] == "COMPLETED"]
    counts = {c: 0 for c in ProviderProvenanceClassification}
    attempt_count = success_count = failure_count = partial_count = qualifying_count = 0
    for cycle in completed:
        cycle_rows = by_cycle.get(cycle["cycle_id"], [])
        counts[_classify_rows(cycle_rows)] += 1
        real_rows = [r for r in cycle_rows if r["is_real"]]
        outcomes = {_row_outcome(r) for r in real_rows}
        if real_rows:
            attempt_count += 1
        if ProviderOutcome.SUCCEEDED.value in outcomes:
            success_count += 1
        if outcomes.intersection(o.value for o in FAILURE_OUTCOMES):
            failure_count += 1
        if ProviderOutcome.PARTIAL.value in outcomes or (
            ProviderOutcome.SUCCEEDED.value in outcomes and outcomes.intersection(o.value for o in FAILURE_OUTCOMES)
        ):
            partial_count += 1
        # No authoritative configured category set is persisted today. The
        # mandated conservative fallback therefore qualifies only a cycle
        # with explicit real activity where every observed real-provider row
        # succeeded. A single PARTIAL/FAILED/UNAVAILABLE/ATTEMPTED/UNKNOWN
        # row disqualifies the whole cycle.
        if real_rows and all(_row_outcome(r) == ProviderOutcome.SUCCEEDED.value for r in real_rows):
            qualifying_count += 1

    real_count = success_count
    return ProviderProvenanceSummary(
        as_of=as_of, policy_version=CLASSIFICATION_VERSION, total_classified_cycles=len(completed),
        real_provider_cycle_count=real_count,
        fixture_only_cycle_count=counts[ProviderProvenanceClassification.FIXTURE_ONLY],
        real_evidence_only_cycle_count=counts[ProviderProvenanceClassification.REAL_EVIDENCE_ONLY],
        real_claude_only_cycle_count=counts[ProviderProvenanceClassification.REAL_CLAUDE_ONLY],
        real_evidence_and_claude_cycle_count=counts[ProviderProvenanceClassification.REAL_EVIDENCE_AND_CLAUDE],
        mixed_cycle_count=counts[ProviderProvenanceClassification.MIXED],
        unknown_cycle_count=counts[ProviderProvenanceClassification.UNKNOWN],
        completed_cycle_count=len(completed), real_provider_attempt_cycle_count=attempt_count,
        real_provider_success_cycle_count=success_count, real_provider_failure_cycle_count=failure_count,
        partial_provider_cycle_count=partial_count,
        excluded_partial_cycle_count=sum(c["status"] == "PARTIALLY_COMPLETE" for c in cycles),
        excluded_failed_cycle_count=sum(c["status"] == "FAILED" for c in cycles),
        excluded_running_cycle_count=sum(c["status"] == "RUNNING" for c in cycles),
        qualifying_real_provider_cycle_count=qualifying_count,
    )
