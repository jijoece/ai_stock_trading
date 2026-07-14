"""Immutable, authoritative research-cycle telemetry (docs/milestone-7.1.md
Step 16). `ResearchCycleTelemetry` is derived entirely from already-persisted
`research_attempts`/`research_attempt_failures` rows for a specific set of
`research_run_id`s — never a duplicated in-memory counter kept alongside the
orchestrator's own bookkeeping. `storage/research_repositories.py::
compute_cycle_telemetry` is the one function that builds this from real rows;
this module only defines the shape and status vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

STATUS_COMPLETE = "COMPLETE"
STATUS_PARTIAL = "PARTIAL"
STATUS_UNAVAILABLE = "UNAVAILABLE"

TELEMETRY_STATUSES = (STATUS_COMPLETE, STATUS_PARTIAL, STATUS_UNAVAILABLE)


class CycleTelemetryError(ValueError):
    pass


@dataclass(frozen=True)
class ResearchCycleTelemetry:
    status: str
    research_run_ids: tuple[str, ...]
    attempt_count: int
    successful_attempt_count: int
    failed_attempt_count: int
    retry_count: int
    retry_exhaustion_count: int
    required_role_failure_count: int
    provider_failure_count: int
    unsupported_claim_count: int
    output_truncation_count: int
    budget_skipped_attempt_count: int
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    priced_usage_cost_usd: Decimal | None
    pricing_status: str
    missing_usage_record_count: int

    def __post_init__(self) -> None:
        if self.status not in TELEMETRY_STATUSES:
            raise CycleTelemetryError(f"ResearchCycleTelemetry.status={self.status!r} must be one of {TELEMETRY_STATUSES}")
        if self.attempt_count != self.successful_attempt_count + self.failed_attempt_count:
            raise CycleTelemetryError("attempt_count must equal successful_attempt_count + failed_attempt_count")
