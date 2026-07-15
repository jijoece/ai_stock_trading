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

# Deferred import (inside functions below): `storage.research_cycle_repositories`
# itself imports `SymbolCycleResult` from this package's `scheduled_cycle`
# module, which imports this module — a module-level import here would be
# circular.

CLASSIFICATION_VERSION = "provider-provenance/v1"

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
_REAL_CLAUDE_PROVIDER_NAMES = ("anthropic",)
_FIXTURE_CLAUDE_PROVIDER_NAMES = ("deterministic", "scripted")


class ProviderProvenanceClassification(str, Enum):
    FIXTURE_ONLY = "FIXTURE_ONLY"
    REAL_EVIDENCE_ONLY = "REAL_EVIDENCE_ONLY"
    REAL_CLAUDE_ONLY = "REAL_CLAUDE_ONLY"
    REAL_EVIDENCE_AND_CLAUDE = "REAL_EVIDENCE_AND_CLAUDE"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


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
        "status": status, "observed_at": observed_at.isoformat(), "classification_version": CLASSIFICATION_VERSION,
        "created_at": observed_at.isoformat(),
    }


def claude_provider_row(
    *, cycle_id: str, research_run_id: str, symbol: str, provider_name: str, observed_at: datetime,
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
        "status": "ok", "observed_at": observed_at.isoformat(), "classification_version": CLASSIFICATION_VERSION,
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


def compute_real_provider_history(conn, as_of: datetime) -> ProviderProvenanceSummary:
    """Aggregates every cycle with persisted provenance at-or-before `as_of`
    into the bounded, queryable counts Section 4 requires. Cycles never
    given a provenance record (e.g. predating this milestone) are simply
    absent — never counted anywhere, never fabricated as UNKNOWN."""
    from ..storage import research_cycle_repositories as cycle_repo

    rows = cycle_repo.list_provider_provenance_upto(conn, as_of.isoformat())
    by_cycle: dict[str, list[dict]] = {}
    for row in rows:
        by_cycle.setdefault(row["cycle_id"], []).append(row)

    counts = {c: 0 for c in ProviderProvenanceClassification}
    for cycle_rows in by_cycle.values():
        counts[_classify_rows(cycle_rows)] += 1

    real_count = sum(counts[c] for c in REAL_CLASSIFICATIONS)
    return ProviderProvenanceSummary(
        as_of=as_of, policy_version=CLASSIFICATION_VERSION, total_classified_cycles=len(by_cycle),
        real_provider_cycle_count=real_count,
        fixture_only_cycle_count=counts[ProviderProvenanceClassification.FIXTURE_ONLY],
        real_evidence_only_cycle_count=counts[ProviderProvenanceClassification.REAL_EVIDENCE_ONLY],
        real_claude_only_cycle_count=counts[ProviderProvenanceClassification.REAL_CLAUDE_ONLY],
        real_evidence_and_claude_cycle_count=counts[ProviderProvenanceClassification.REAL_EVIDENCE_AND_CLAUDE],
        mixed_cycle_count=counts[ProviderProvenanceClassification.MIXED],
        unknown_cycle_count=counts[ProviderProvenanceClassification.UNKNOWN],
    )
