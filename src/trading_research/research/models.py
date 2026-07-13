"""Point-in-time evidence and research-role contracts (Milestone 5, docs/milestone-5.md
Steps 4 and 7).

Conventions match the rest of the repository (see `models/trading_models.py`):
`@dataclass(frozen=True)`, UTC-aware datetimes enforced in `__post_init__`,
`Decimal` for money/confidence-like numeric fields that need exact
comparison. Nothing in this module imports the Anthropic SDK or talks to a
database — these are pure, immutable, framework-neutral value objects.

Claude is a research provider, not the trading-desk authority: nothing in
this module (or anywhere else in `research/`) can construct an order,
compute a share quantity, or mutate broker/ledger state. See
docs/adr/0003-claude-research-boundary.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from .errors import EvidenceValidationError

RATINGS = ("BUY", "OVERWEIGHT", "HOLD", "UNDERWEIGHT", "SELL", "ANALYSIS_INCOMPLETE")
STANCES = ("BULLISH", "BEARISH", "NEUTRAL", "ANALYSIS_INCOMPLETE")
CONFIDENCE_LEVELS = ("high", "medium", "low")
CLAIM_IMPORTANCE_LEVELS = ("high", "medium", "low")
OVERLAY_ACTIONS = ("ALLOW_BASELINE", "DOWNGRADE_TO_WATCH", "FORCE_NO_ACTION", "ANALYSIS_INCOMPLETE")
EXPERIMENT_ARMS = ("BASELINE", "ENHANCED")

# Cost-estimation statuses (docs/milestone-5.md Step 17) — usage is never invented.
COST_CALCULATED = "CALCULATED"
COST_PRICING_NOT_CONFIGURED = "PRICING_NOT_CONFIGURED"
COST_USAGE_NOT_RETURNED = "USAGE_NOT_RETURNED"
COST_NOT_APPLICABLE = "NOT_APPLICABLE"
COST_STATUSES = (COST_CALCULATED, COST_PRICING_NOT_CONFIGURED, COST_USAGE_NOT_RETURNED, COST_NOT_APPLICABLE)


def _require_tz_aware(value: datetime | None, name: str) -> None:
    if value is not None and value.tzinfo is None:
        raise EvidenceValidationError(f"{name} must be timezone-aware")


def _require_enum(value: str, allowed: tuple[str, ...], name: str) -> None:
    if value not in allowed:
        raise EvidenceValidationError(f"{name}={value!r} is not one of {allowed}")


@dataclass(frozen=True)
class SourceRecord:
    """One retrieved piece of raw source material, with the four timestamps
    needed to prevent look-ahead bias: when it was published, when it became
    effective, when the strategy could actually have seen it, and when this
    pipeline retrieved it. `available_at` is the field screening/scoring
    must respect — a document `published_at` in the past but only
    `available_at` after the evidence snapshot's `as_of` may not be used."""

    source_id: str
    source_type: str
    provider: str
    source_locator: str | None
    retrieved_at: datetime
    published_at: datetime | None
    effective_at: datetime | None
    available_at: datetime | None
    content_hash: str
    status: str
    is_stale: bool
    point_in_time_safe: bool
    error_code: str | None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_tz_aware(self.retrieved_at, f"SourceRecord({self.source_id}).retrieved_at")
        _require_tz_aware(self.published_at, f"SourceRecord({self.source_id}).published_at")
        _require_tz_aware(self.effective_at, f"SourceRecord({self.source_id}).effective_at")
        _require_tz_aware(self.available_at, f"SourceRecord({self.source_id}).available_at")
        if not self.source_id:
            raise EvidenceValidationError("SourceRecord.source_id must be non-empty")


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    source_id: str
    category: str
    title: str
    summary: str
    normalized_values: Mapping[str, Any]
    as_of: datetime
    confidence: str
    stale: bool
    conflict_group: str | None = None

    def __post_init__(self) -> None:
        _require_tz_aware(self.as_of, f"EvidenceItem({self.evidence_id}).as_of")
        _require_enum(self.confidence, CONFIDENCE_LEVELS, f"EvidenceItem({self.evidence_id}).confidence")
        if not self.evidence_id:
            raise EvidenceValidationError("EvidenceItem.evidence_id must be non-empty")


@dataclass(frozen=True)
class EvidenceSnapshot:
    """Immutable, point-in-time bundle of evidence for one symbol. Once a
    research run has referenced a snapshot_id, that snapshot must never be
    mutated — the orchestrator only ever constructs a *new* snapshot for a
    changed input set (see `research/evidence.py::build_evidence_snapshot`)."""

    snapshot_id: str
    symbol: str
    as_of: datetime
    created_at: datetime
    source_records: tuple[SourceRecord, ...]
    evidence_items: tuple[EvidenceItem, ...]
    deterministic_factors: Mapping[str, float]
    sentiment_metrics: Mapping[str, Any]
    portfolio_context: Mapping[str, Any] | None
    missing_data_reasons: tuple[str, ...]
    conflict_reasons: tuple[str, ...]
    point_in_time_safe: bool
    config_hash: str
    git_sha: str

    def __post_init__(self) -> None:
        _require_tz_aware(self.as_of, "EvidenceSnapshot.as_of")
        _require_tz_aware(self.created_at, "EvidenceSnapshot.created_at")
        if not self.symbol:
            raise EvidenceValidationError("EvidenceSnapshot.symbol must be non-empty")
        source_ids = {s.source_id for s in self.source_records}
        for item in self.evidence_items:
            if item.source_id not in source_ids:
                raise EvidenceValidationError(
                    f"EvidenceItem {item.evidence_id!r} references unknown source_id {item.source_id!r} "
                    f"— every evidence item must cite a source_record in the same snapshot"
                )

    def evidence_ids(self) -> frozenset[str]:
        return frozenset(item.evidence_id for item in self.evidence_items)

    def evidence_by_id(self, evidence_id: str) -> EvidenceItem | None:
        for item in self.evidence_items:
            if item.evidence_id == evidence_id:
                return item
        return None


@dataclass(frozen=True)
class ResearchClaim:
    claim_id: str
    claim_type: str
    statement: str
    evidence_ids: tuple[str, ...]
    numeric_value: Decimal | None
    unit: str | None
    importance: str

    def __post_init__(self) -> None:
        _require_enum(self.importance, CLAIM_IMPORTANCE_LEVELS, f"ResearchClaim({self.claim_id}).importance")
        if not self.claim_id:
            raise EvidenceValidationError("ResearchClaim.claim_id must be non-empty")


@dataclass(frozen=True)
class RoleResearchReport:
    report_id: str
    research_run_id: str
    role: str
    symbol: str
    snapshot_id: str
    stance: str
    summary: str
    claims: tuple[ResearchClaim, ...]
    catalysts: tuple[str, ...]
    risks: tuple[str, ...]
    uncertainties: tuple[str, ...]
    missing_data_reasons: tuple[str, ...]
    model_name: str
    prompt_version: str

    def __post_init__(self) -> None:
        _require_enum(self.stance, STANCES, f"RoleResearchReport({self.report_id}).stance")


@dataclass(frozen=True)
class ResearchDecision:
    decision_id: str
    research_run_id: str
    symbol: str
    snapshot_id: str
    rating: str
    confidence: Decimal
    thesis: str
    bull_case: str
    bear_case: str
    catalysts: tuple[str, ...]
    risks: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    claims: tuple[ResearchClaim, ...]
    evidence_ids: tuple[str, ...]
    missing_data_reasons: tuple[str, ...]
    model_name: str
    prompt_version: str

    def __post_init__(self) -> None:
        _require_enum(self.rating, RATINGS, f"ResearchDecision({self.decision_id}).rating")
        if not (Decimal("0") <= self.confidence <= Decimal("1")):
            raise EvidenceValidationError(
                f"ResearchDecision({self.decision_id}).confidence must be in [0, 1], got {self.confidence}"
            )
        if self.rating != "ANALYSIS_INCOMPLETE" and not self.bear_case.strip():
            raise EvidenceValidationError(f"ResearchDecision({self.decision_id}) must not have an empty bear_case")
        if self.rating != "ANALYSIS_INCOMPLETE" and not self.bull_case.strip():
            raise EvidenceValidationError(f"ResearchDecision({self.decision_id}) must not have an empty bull_case")


@dataclass(frozen=True)
class ResearchOverlayDecision:
    overlay_id: str
    research_decision_id: str
    baseline_score: Decimal | None
    action: str
    reasons: tuple[str, ...]
    critical_risks: tuple[str, ...]
    policy_version: str

    def __post_init__(self) -> None:
        _require_enum(self.action, OVERLAY_ACTIONS, f"ResearchOverlayDecision({self.overlay_id}).action")


@dataclass(frozen=True)
class ExperimentAssignment:
    experiment_id: str
    candidate_run_id: str
    symbol: str
    as_of: datetime
    arm: str
    baseline_recommendation_id: str | None
    enhanced_recommendation_id: str | None
    assignment_policy_version: str

    def __post_init__(self) -> None:
        _require_tz_aware(self.as_of, "ExperimentAssignment.as_of")
        _require_enum(self.arm, EXPERIMENT_ARMS, f"ExperimentAssignment({self.experiment_id}).arm")


@dataclass(frozen=True)
class UsageRecord:
    """Per-attempt usage/cost metadata (docs/milestone-5.md Step 17). Never
    fabricated: absent token counts stay `None`, and `cost_status` explains
    exactly why `estimated_cost` is or is not populated."""

    provider: str
    model_name: str
    role: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    latency_ms: int | None
    provider_request_id: str | None
    retry_count: int
    success: bool
    pricing_version: str | None
    estimated_cost: Decimal | None
    cost_status: str

    def __post_init__(self) -> None:
        _require_enum(self.cost_status, COST_STATUSES, "UsageRecord.cost_status")
        if self.cost_status == COST_CALCULATED and self.estimated_cost is None:
            raise EvidenceValidationError("UsageRecord.cost_status=CALCULATED requires estimated_cost")
        if self.cost_status != COST_CALCULATED and self.estimated_cost is not None:
            raise EvidenceValidationError(f"UsageRecord.cost_status={self.cost_status!r} must not carry estimated_cost")


@dataclass(frozen=True)
class ResearchModelRequest:
    """Framework-neutral request passed to `ResearchModelProvider.generate_structured`.

    `evidence_snapshot` is included so a provider implementation can build
    its own prompt, but no provider is permitted to add evidence beyond what
    the snapshot already contains (docs/milestone-5.md Step 10: "retries may
    include validation feedback but may not add new evidence silently")."""

    role: str
    research_run_id: str
    snapshot: EvidenceSnapshot
    system_prompt: str
    user_prompt: str
    json_schema: Mapping[str, Any]
    model_name: str
    max_output_tokens: int
    temperature: float
    prompt_name: str
    prompt_version: str
    prompt_hash: str
    system_prompt_hash: str
    schema_version: str
    attempt_number: int
    validation_feedback: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResearchModelResponse:
    """Framework-neutral response from a provider. `parsed_json` is the
    already-JSON-decoded structured payload (never raw prose) — providers
    that cannot guarantee this must raise `MalformedOutputError` instead of
    returning a response with `parsed_json=None`."""

    role: str
    provider: str
    model_name: str
    parsed_json: Mapping[str, Any]
    raw_text: str
    usage: UsageRecord
    provider_request_id: str | None
