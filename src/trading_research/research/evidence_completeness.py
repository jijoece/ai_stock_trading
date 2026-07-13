"""Deterministic, versioned evidence-completeness policy (docs/milestone-7.md
Step 11; ADR 0005 Decision 10).

Sits *beside* `evidence_providers/normalization.py::classify_snapshot_outcome`
(unchanged), composing its single-snapshot outcome with the new
`evidence_providers/corporate_status.py::CorporateStatusEvidence` result
into a distinct screening-vs-research pair of completeness statuses. Does
not duplicate snapshot-level classification logic (missing-category
detection, point-in-time-safety, staleness) — all of that stays owned by
`classify_snapshot_outcome`/`validate_snapshot_preconditions`.

Pure deterministic code: no model/Claude input influences this function's
output, and none of the values here originate from `research/orchestration.py`
or any Claude response.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..evidence_providers.corporate_status import CorporateStatusEvidence
from ..evidence_providers.normalization import (
    BLOCKING_OUTCOMES,
    OUTCOME_COMPLETE,
    OUTCOME_COMPLETE_WITH_CONFLICTS,
    OUTCOME_INCOMPLETE_REQUIRED_DATA,
    OUTCOME_POINT_IN_TIME_UNSAFE,
    OUTCOME_PROVIDER_UNAVAILABLE,
    OUTCOME_STALE_REQUIRED_DATA,
)

POLICY_VERSION = "evidence-completeness-v1"

# --- Status vocabulary (docs/milestone-7.md Step 11) --------------------

STATUS_COMPLETE_FOR_SCREENING = "COMPLETE_FOR_SCREENING"
STATUS_COMPLETE_FOR_RESEARCH = "COMPLETE_FOR_RESEARCH"
STATUS_PARTIAL_NONCRITICAL = "PARTIAL_NONCRITICAL"
STATUS_MISSING_CRITICAL_CORPORATE_STATUS = "MISSING_CRITICAL_CORPORATE_STATUS"
STATUS_MISSING_CRITICAL_MARKET_DATA = "MISSING_CRITICAL_MARKET_DATA"
STATUS_MISSING_CRITICAL_FUNDAMENTALS = "MISSING_CRITICAL_FUNDAMENTALS"
STATUS_MISSING_NEWS = "MISSING_NEWS"
STATUS_MISSING_SENTIMENT = "MISSING_SENTIMENT"
STATUS_CONFLICTING_CRITICAL_DATA = "CONFLICTING_CRITICAL_DATA"
STATUS_POINT_IN_TIME_UNSAFE = "POINT_IN_TIME_UNSAFE"
STATUS_PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

ALL_STATUS_VALUES = (
    STATUS_COMPLETE_FOR_SCREENING,
    STATUS_COMPLETE_FOR_RESEARCH,
    STATUS_PARTIAL_NONCRITICAL,
    STATUS_MISSING_CRITICAL_CORPORATE_STATUS,
    STATUS_MISSING_CRITICAL_MARKET_DATA,
    STATUS_MISSING_CRITICAL_FUNDAMENTALS,
    STATUS_MISSING_NEWS,
    STATUS_MISSING_SENTIMENT,
    STATUS_CONFLICTING_CRITICAL_DATA,
    STATUS_POINT_IN_TIME_UNSAFE,
    STATUS_PROVIDER_UNAVAILABLE,
)

# Statuses that block SCREENING completeness specifically (fail-closed set).
_SCREENING_BLOCKING_STATUSES = (
    STATUS_MISSING_CRITICAL_CORPORATE_STATUS,
    STATUS_MISSING_CRITICAL_MARKET_DATA,
    STATUS_MISSING_CRITICAL_FUNDAMENTALS,
    STATUS_CONFLICTING_CRITICAL_DATA,
    STATUS_POINT_IN_TIME_UNSAFE,
    STATUS_PROVIDER_UNAVAILABLE,
)


@dataclass(frozen=True)
class EvidenceCompletenessResult:
    symbol: str
    screening_completeness: str
    research_completeness: str
    blocking_categories: tuple[str, ...]
    policy_version: str

    def __post_init__(self) -> None:
        if self.screening_completeness not in ALL_STATUS_VALUES:
            raise ValueError(f"screening_completeness={self.screening_completeness!r} invalid")
        if self.research_completeness not in ALL_STATUS_VALUES:
            raise ValueError(f"research_completeness={self.research_completeness!r} invalid")

    @property
    def screening_blocked(self) -> bool:
        return self.screening_completeness in _SCREENING_BLOCKING_STATUSES


def evaluate_completeness(
    *,
    symbol: str,
    snapshot_outcome: str,
    snapshot_reasons: tuple[str, ...] = (),
    corporate_status: CorporateStatusEvidence | None,
    news_present: bool = True,
    sentiment_present: bool = True,
) -> EvidenceCompletenessResult:
    """Composes an existing `classify_snapshot_outcome` result with the new
    `CorporateStatusEvidence` result (optional — `None` when corporate
    status was never fetched, distinct from a fetched-but-uncertain
    result).

    Requirements enforced here (Step 11):

    * news/sentiment absence alone never blocks screening completeness;
    * corporate-status `UNKNOWN`/`CONFLICTING`/`SOURCE_UNAVAILABLE` (i.e.
      `CorporateStatusEvidence.has_any_critical_uncertainty()`) always
      blocks screening completeness — fail-closed;
    * `policy_version` is always stamped on the result for persistence;
    * `blocking_categories` names every category currently blocking, not
      just the first one found.
    """
    blocking: list[str] = []

    # --- Corporate-status: fail-closed on any critical uncertainty, and
    # equally fail-closed when it was never fetched at all (Step 11: "no
    # unsupported healthy-company default" applies here too — an unfetched
    # corporate-status result must not be silently treated as fine).
    corporate_status_blocks = corporate_status is None or corporate_status.has_any_critical_uncertainty()
    if corporate_status_blocks:
        blocking.append(STATUS_MISSING_CRITICAL_CORPORATE_STATUS)

    # --- Snapshot-outcome-derived blocking categories.
    if snapshot_outcome == OUTCOME_POINT_IN_TIME_UNSAFE:
        blocking.append(STATUS_POINT_IN_TIME_UNSAFE)
    elif snapshot_outcome == OUTCOME_PROVIDER_UNAVAILABLE:
        blocking.append(STATUS_PROVIDER_UNAVAILABLE)
    elif snapshot_outcome in (OUTCOME_INCOMPLETE_REQUIRED_DATA, OUTCOME_STALE_REQUIRED_DATA):
        reasons_lower = " ".join(snapshot_reasons).lower()
        if "market" in reasons_lower:
            blocking.append(STATUS_MISSING_CRITICAL_MARKET_DATA)
        if "fundamentals" in reasons_lower:
            blocking.append(STATUS_MISSING_CRITICAL_FUNDAMENTALS)
        if not any(k in reasons_lower for k in ("market", "fundamentals")):
            # Missing required data whose category couldn't be identified
            # from the reason text still fails closed — treated as a
            # fundamentals gap by default classification bucket, since
            # fundamentals is this repository's other always-required
            # deterministic-screening category (mirrors
            # classify_snapshot_outcome's own "never silently pass"
            # posture rather than inventing a new, undocumented status).
            blocking.append(STATUS_MISSING_CRITICAL_FUNDAMENTALS)
    elif snapshot_outcome == OUTCOME_COMPLETE_WITH_CONFLICTS:
        # Conflicts in required categories are conflicting *critical* data;
        # this policy treats any conflict on an otherwise-complete snapshot
        # as critical-blocking, matching the "fail closed on CONFLICTING"
        # posture Step 4/11 apply to corporate-status signals.
        blocking.append(STATUS_CONFLICTING_CRITICAL_DATA)

    # --- Non-critical categories: news/sentiment absence recorded but never
    # added to the screening-blocking set.
    non_critical_missing: list[str] = []
    if not news_present:
        non_critical_missing.append(STATUS_MISSING_NEWS)
    if not sentiment_present:
        non_critical_missing.append(STATUS_MISSING_SENTIMENT)

    if blocking:
        # Deterministic ordering: fixed vocabulary order, not insertion
        # order, so the same inputs always produce the same tuple.
        ordered_blocking = tuple(s for s in ALL_STATUS_VALUES if s in set(blocking))
        screening_completeness = ordered_blocking[0]
        research_completeness = ordered_blocking[0]
        blocking_categories = ordered_blocking + tuple(non_critical_missing)
        return EvidenceCompletenessResult(
            symbol=symbol, screening_completeness=screening_completeness,
            research_completeness=research_completeness,
            blocking_categories=blocking_categories, policy_version=POLICY_VERSION,
        )

    # No critical blocker: screening is complete regardless of news/sentiment.
    screening_completeness = STATUS_COMPLETE_FOR_SCREENING

    if non_critical_missing:
        research_completeness = STATUS_PARTIAL_NONCRITICAL
    elif snapshot_outcome == OUTCOME_COMPLETE:
        research_completeness = STATUS_COMPLETE_FOR_RESEARCH
    else:
        # OUTCOME_COMPLETE_WITH_CONFLICTS without a critical corporate-status
        # or reasons-derived blocker already returned above; anything else
        # reaching here with no blockers and no missing-noncritical data is
        # complete for research too.
        research_completeness = STATUS_COMPLETE_FOR_RESEARCH

    return EvidenceCompletenessResult(
        symbol=symbol, screening_completeness=screening_completeness,
        research_completeness=research_completeness,
        blocking_categories=tuple(non_critical_missing), policy_version=POLICY_VERSION,
    )
