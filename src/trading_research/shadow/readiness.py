"""Shadow readiness report (docs/milestone-7.md Step 23). Answers: "is the
system safe and stable enough to continue recurring shadow operation?" by
aggregating real, already-persisted data across categories — never a
fabricated `READY` from a single good data point (docs/milestone-7.md: "Do
not equate one successful smoke test with production readiness").

Queries `shadow_scheduler_runs`, `shadow_run_summaries`,
`shadow_alerts`/`shadow_alert_deliveries`,
`research_attempts`/`research_attempt_failures`,
`evidence_provider_health_snapshots` — whichever tables actually carry the
relevant data for a category. If a category's underlying data genuinely
doesn't exist yet (e.g. very few real cycles have ever run), its status is
`INSUFFICIENT_DATA`, never a fabricated `READY`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..research.failure_metrics import (
    METRIC_STATUS_OK,
    compute_research_failure_metrics,
)
from ..storage.research_repositories import list_all_attempt_failures, list_attempt_rows_for_metrics
from ..storage.shadow_alerts_repositories import list_alert_deliveries, list_alerts, list_run_summaries
from ..storage.shadow_operations_repositories import list_scheduler_runs
from .config import ShadowOperationsConfiguration

POLICY_VERSION = "readiness/v1"

STATUS_READY = "READY"
STATUS_READY_WITH_WARNINGS = "READY_WITH_WARNINGS"
STATUS_NOT_READY = "NOT_READY"
STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
STATUS_ENVIRONMENTALLY_BLOCKED = "ENVIRONMENTALLY_BLOCKED"

READINESS_STATUSES = (
    STATUS_READY, STATUS_READY_WITH_WARNINGS, STATUS_NOT_READY, STATUS_INSUFFICIENT_DATA,
    STATUS_ENVIRONMENTALLY_BLOCKED,
)

_SEVERITY_ORDER = {
    STATUS_READY: 0, STATUS_READY_WITH_WARNINGS: 1, STATUS_NOT_READY: 2,
    STATUS_INSUFFICIENT_DATA: 3, STATUS_ENVIRONMENTALLY_BLOCKED: 4,
}

# Hard floor: below this many completed cycles, the overall report can never
# be READY or READY_WITH_WARNINGS regardless of how good the observed sample
# looks (docs/milestone-7.md Step 23: "Do not equate one successful smoke
# test with production readiness" — explicit test required for this).
DEFAULT_MIN_COMPLETED_CYCLES_FOR_READY = 10
DEFAULT_MIN_REAL_PROVIDER_CYCLES_FOR_READY = 5


class ReadinessPolicyError(RuntimeError):
    pass


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY_ORDER[a] >= _SEVERITY_ORDER[b] else b


@dataclass(frozen=True)
class ReadinessThresholds:
    """Readiness-specific minimum-sample thresholds, additive to
    `config/shadow_operations.yaml` rather than reusing `budgets`/`safety`
    (those govern per-cycle enforcement, not "is the aggregate history
    trustworthy yet")."""

    policy_version: str = POLICY_VERSION
    min_completed_cycles_for_ready: int = DEFAULT_MIN_COMPLETED_CYCLES_FOR_READY
    min_real_provider_cycles_for_ready: int = DEFAULT_MIN_REAL_PROVIDER_CYCLES_FOR_READY
    max_scheduler_miss_rate: float = 0.10
    max_lease_conflict_rate: float = 0.10

    def __post_init__(self) -> None:
        if self.min_completed_cycles_for_ready < 1:
            raise ReadinessPolicyError("min_completed_cycles_for_ready must be >= 1")
        if self.min_real_provider_cycles_for_ready < 0:
            raise ReadinessPolicyError("min_real_provider_cycles_for_ready must be >= 0")


@dataclass(frozen=True)
class CategoryReadiness:
    category: str
    status: str
    reasons: tuple[str, ...]
    metrics: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in READINESS_STATUSES:
            raise ReadinessPolicyError(f"category status {self.status!r} is not one of {READINESS_STATUSES} — fails closed")


@dataclass(frozen=True)
class ReadinessReport:
    as_of: datetime
    policy_version: str
    overall_status: str
    categories: tuple[CategoryReadiness, ...]
    completed_cycle_count: int
    real_provider_cycle_count: int
    evidence_completeness_rate: float | None
    role_completion_rate: float | None
    retry_exhaustion_rate: float | None
    unsupported_claim_rate: float | None
    provider_failure_rate: float | None
    cost_per_completed_cycle_usd: Decimal | None
    average_cycle_duration_seconds: float | None
    scheduler_miss_count: int
    lease_conflict_count: int
    reconciliation_mismatch_count: int
    alert_delivery_failure_count: int
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.overall_status not in READINESS_STATUSES:
            raise ReadinessPolicyError(f"overall status {self.overall_status!r} is not one of {READINESS_STATUSES} — fails closed")


def _category(category: str, status: str, reasons: tuple[str, ...], metrics: dict | None = None) -> CategoryReadiness:
    return CategoryReadiness(category=category, status=status, reasons=reasons, metrics=metrics or {})


def _evidence_readiness(runs: list[dict], summaries: list[dict]) -> CategoryReadiness:
    rates = [s["evidence_completeness_rate"] for s in summaries if s["evidence_completeness_rate"] is not None]
    if not rates:
        return _category("evidence", STATUS_INSUFFICIENT_DATA, ("no run summaries with evidence_completeness_rate yet",))
    avg = sum(rates) / len(rates)
    if avg >= 0.95:
        status, reason = STATUS_READY, f"average evidence_completeness_rate {avg:.3f} >= 0.95"
    elif avg >= 0.80:
        status, reason = STATUS_READY_WITH_WARNINGS, f"average evidence_completeness_rate {avg:.3f} in [0.80, 0.95)"
    else:
        status, reason = STATUS_NOT_READY, f"average evidence_completeness_rate {avg:.3f} < 0.80"
    return _category("evidence", status, (reason,), {"average_evidence_completeness_rate": avg, "sample_size": len(rates)})


def _provider_readiness(summaries: list[dict]) -> CategoryReadiness:
    rates = [s["provider_success_rate"] for s in summaries if s["provider_success_rate"] is not None]
    if not rates:
        return _category("provider", STATUS_INSUFFICIENT_DATA, ("no run summaries with provider_success_rate yet",))
    failure_rate = 1.0 - (sum(rates) / len(rates))
    if failure_rate <= 0.05:
        status, reason = STATUS_READY, f"average provider_failure_rate {failure_rate:.3f} <= 0.05"
    elif failure_rate <= 0.20:
        status, reason = STATUS_READY_WITH_WARNINGS, f"average provider_failure_rate {failure_rate:.3f} in (0.05, 0.20]"
    else:
        status, reason = STATUS_NOT_READY, f"average provider_failure_rate {failure_rate:.3f} > 0.20"
    return _category("provider", status, (reason,), {"average_provider_failure_rate": failure_rate, "sample_size": len(rates)})


def _research_readiness(conn) -> CategoryReadiness:
    attempt_rows = list_attempt_rows_for_metrics(conn)
    failures = list_all_attempt_failures(conn)
    if not attempt_rows:
        return _category("research", STATUS_INSUFFICIENT_DATA, ("no research_attempts recorded yet",))
    metrics = compute_research_failure_metrics(attempt_rows=attempt_rows, failures=failures)
    retry_exhaustion = metrics.get("retry_exhaustion_rate", {})
    unsupported_claim = metrics.get("unsupported_claim_rate", {})
    reasons = []
    status = STATUS_READY
    retry_value = None
    if retry_exhaustion.get("status") == METRIC_STATUS_OK:
        retry_value = retry_exhaustion["value"]
        if retry_value > 0.50:
            status = _worse(status, STATUS_NOT_READY)
            reasons.append(f"retry_exhaustion_rate {retry_value:.3f} > 0.50")
        elif retry_value > 0.25:
            status = _worse(status, STATUS_READY_WITH_WARNINGS)
            reasons.append(f"retry_exhaustion_rate {retry_value:.3f} in (0.25, 0.50]")
    unsupported_value = None
    if unsupported_claim.get("status") == METRIC_STATUS_OK:
        unsupported_value = unsupported_claim["value"]
        if unsupported_value > 0.25:
            status = _worse(status, STATUS_NOT_READY)
            reasons.append(f"unsupported_claim_rate {unsupported_value:.3f} > 0.25")
        elif unsupported_value > 0.10:
            status = _worse(status, STATUS_READY_WITH_WARNINGS)
            reasons.append(f"unsupported_claim_rate {unsupported_value:.3f} in (0.10, 0.25]")
    if not reasons:
        reasons.append("retry-exhaustion and unsupported-claim rates within bounds (or undefined with no denominator)")
    return _category(
        "research", status, tuple(reasons),
        {"retry_exhaustion_rate": retry_value, "unsupported_claim_rate": unsupported_value, "sample_size": len(attempt_rows)},
    )


def _budget_readiness(summaries: list[dict]) -> CategoryReadiness:
    costs = [Decimal(s["cost_usd"]) for s in summaries if s.get("cost_usd") is not None]
    if not summaries:
        return _category("budget", STATUS_INSUFFICIENT_DATA, ("no run summaries recorded yet",))
    if not costs:
        return _category(
            "budget", STATUS_INSUFFICIENT_DATA,
            ("no run summaries carry a recorded cost_usd — cost per completed cycle is unknown",),
        )
    avg_cost = sum(costs) / len(costs)
    return _category("budget", STATUS_READY, (f"average cost_usd per summarized run {avg_cost}",), {"average_cost_usd": str(avg_cost), "sample_size": len(costs)})


def _scheduler_readiness(runs: list[dict], thresholds: ReadinessThresholds) -> tuple[CategoryReadiness, int]:
    if not runs:
        return _category("scheduler", STATUS_INSUFFICIENT_DATA, ("no scheduler runs recorded yet",)), 0
    missed_statuses = {"MISSED_TOO_OLD", "FAILED"}
    misses = sum(1 for r in runs if r["status"] in missed_statuses)
    miss_rate = misses / len(runs)
    if miss_rate > thresholds.max_scheduler_miss_rate:
        status = STATUS_NOT_READY
        reason = f"scheduler_miss_rate {miss_rate:.3f} > max {thresholds.max_scheduler_miss_rate:.3f}"
    elif miss_rate > 0:
        status = STATUS_READY_WITH_WARNINGS
        reason = f"scheduler_miss_rate {miss_rate:.3f} > 0 but within max {thresholds.max_scheduler_miss_rate:.3f}"
    else:
        status = STATUS_READY
        reason = "no missed scheduler runs observed"
    return _category("scheduler", status, (reason,), {"miss_count": misses, "total_runs": len(runs)}), misses


def _paper_readiness(runs: list[dict]) -> tuple[CategoryReadiness, int]:
    reconciliation_mismatches = sum(1 for r in runs if r.get("failure_reason") and "reconcil" in r["failure_reason"].lower())
    if not runs:
        return _category("paper", STATUS_INSUFFICIENT_DATA, ("no scheduler runs recorded yet",)), 0
    if reconciliation_mismatches > 0:
        return _category(
            "paper", STATUS_NOT_READY, (f"{reconciliation_mismatches} run(s) show a reconciliation-mismatch failure reason",),
        ), reconciliation_mismatches
    return _category("paper", STATUS_READY_WITH_WARNINGS, ("no reconciliation mismatches observed; baseline-paper submission scope is limited in this milestone",)), 0


def _evaluation_readiness(completed_cycle_count: int, thresholds: ReadinessThresholds) -> CategoryReadiness:
    if completed_cycle_count < thresholds.min_completed_cycles_for_ready:
        return _category(
            "evaluation", STATUS_INSUFFICIENT_DATA,
            (f"completed_cycle_count {completed_cycle_count} < minimum {thresholds.min_completed_cycles_for_ready}",),
        )
    return _category("evaluation", STATUS_READY_WITH_WARNINGS, ("sufficient completed-cycle sample size for evaluation; see promotion.py for the actual promotion decision",))


def _operational_readiness(conn, thresholds: ReadinessThresholds) -> tuple[CategoryReadiness, int]:
    alerts = list_alerts(conn)
    if not alerts:
        return _category("operational", STATUS_INSUFFICIENT_DATA, ("no alerts recorded yet",)), 0
    failed_deliveries = 0
    for alert in alerts:
        deliveries = list_alert_deliveries(conn, alert["alert_id"])
        if deliveries and not any(d["success"] for d in deliveries):
            failed_deliveries += 1
    critical_alerts = [a for a in alerts if a["severity"] == "CRITICAL"]
    if failed_deliveries > 0:
        status = STATUS_NOT_READY
        reason = f"{failed_deliveries} alert(s) failed delivery on every attempted sink"
    elif critical_alerts:
        status = STATUS_READY_WITH_WARNINGS
        reason = f"{len(critical_alerts)} CRITICAL alert(s) recorded (all delivered successfully)"
    else:
        status = STATUS_READY
        reason = "no delivery failures; no CRITICAL alerts"
    return _category("operational", status, (reason,), {"total_alerts": len(alerts), "failed_delivery_alerts": failed_deliveries}), failed_deliveries


def build_readiness_report(
    conn, as_of: datetime, config: ShadowOperationsConfiguration, *,
    thresholds: ReadinessThresholds | None = None,
) -> ReadinessReport:
    """Aggregates across categories from real, already-persisted data.
    `config` is accepted (and required by this signature, per this task's
    spec) for forward compatibility with config-sourced thresholds beyond
    `ReadinessThresholds`'s defaults — current category logic only reads
    `thresholds`, but the shadow-operations config object is threaded
    through so a future slice can fold e.g. `budgets.max_estimated_cost_per_cycle_usd`
    into a category check without changing this function's signature."""
    del config  # reserved for future threshold sourcing; see docstring
    thresholds = thresholds or ReadinessThresholds()

    runs = list_scheduler_runs(conn)
    summaries = list_run_summaries(conn)
    completed_runs = [r for r in runs if r["status"] == "COMPLETED"]
    real_provider_summaries = [
        s for s in summaries if s.get("cost_usd") is not None and Decimal(s["cost_usd"]) > 0
    ]

    evidence_cat = _evidence_readiness(runs, summaries)
    provider_cat = _provider_readiness(summaries)
    research_cat = _research_readiness(conn)
    budget_cat = _budget_readiness(summaries)
    scheduler_cat, scheduler_misses = _scheduler_readiness(runs, thresholds)
    paper_cat, reconciliation_mismatches = _paper_readiness(runs)
    evaluation_cat = _evaluation_readiness(len(completed_runs), thresholds)
    operational_cat, alert_delivery_failures = _operational_readiness(conn, thresholds)

    lease_conflicts = sum(1 for r in runs if r["status"] == "LEASE_HELD")

    categories = (
        evidence_cat, provider_cat, research_cat, budget_cat, scheduler_cat, paper_cat, evaluation_cat, operational_cat,
    )

    overall_reasons: list[str] = []
    overall_status = STATUS_READY
    for cat in categories:
        overall_status = _worse(overall_status, cat.status)
        overall_reasons.extend(f"{cat.category}: {r}" for r in cat.reasons)

    # Hard floor (docs/milestone-7.md Step 23 explicit requirement): a
    # single successful cycle — or any count below the configured minimum —
    # can never produce an overall READY/READY_WITH_WARNINGS, regardless of
    # how good every category above looks.
    if len(completed_runs) < thresholds.min_completed_cycles_for_ready:
        overall_status = _worse(overall_status, STATUS_INSUFFICIENT_DATA)
        overall_reasons.append(
            f"overall: completed_cycle_count {len(completed_runs)} < minimum "
            f"{thresholds.min_completed_cycles_for_ready} required for READY — insufficient sample size"
        )
    if len(real_provider_summaries) < thresholds.min_real_provider_cycles_for_ready:
        if overall_status in (STATUS_READY, STATUS_READY_WITH_WARNINGS):
            overall_status = _worse(overall_status, STATUS_INSUFFICIENT_DATA)
        overall_reasons.append(
            f"overall: real_provider_cycle_count {len(real_provider_summaries)} < minimum "
            f"{thresholds.min_real_provider_cycles_for_ready} required for READY"
        )

    evidence_rate = evidence_cat.metrics.get("average_evidence_completeness_rate")
    provider_failure_rate = provider_cat.metrics.get("average_provider_failure_rate")
    retry_exhaustion_rate = research_cat.metrics.get("retry_exhaustion_rate")
    unsupported_claim_rate = research_cat.metrics.get("unsupported_claim_rate")
    cost_values = [Decimal(s["cost_usd"]) for s in summaries if s.get("cost_usd") is not None]
    cost_per_cycle = (sum(cost_values) / len(cost_values)) if cost_values else None
    duration_values = [s["cycle_duration_seconds"] for s in summaries if s.get("cycle_duration_seconds") is not None]
    avg_duration = (sum(duration_values) / len(duration_values)) if duration_values else None
    role_success_values = [s["claude_role_success_rate"] for s in summaries if s.get("claude_role_success_rate") is not None]
    role_completion_rate = (sum(role_success_values) / len(role_success_values)) if role_success_values else None

    return ReadinessReport(
        as_of=as_of, policy_version=thresholds.policy_version, overall_status=overall_status, categories=categories,
        completed_cycle_count=len(completed_runs), real_provider_cycle_count=len(real_provider_summaries),
        evidence_completeness_rate=evidence_rate, role_completion_rate=role_completion_rate,
        retry_exhaustion_rate=retry_exhaustion_rate, unsupported_claim_rate=unsupported_claim_rate,
        provider_failure_rate=provider_failure_rate, cost_per_completed_cycle_usd=cost_per_cycle,
        average_cycle_duration_seconds=avg_duration, scheduler_miss_count=scheduler_misses,
        lease_conflict_count=lease_conflicts, reconciliation_mismatch_count=reconciliation_mismatches,
        alert_delivery_failure_count=alert_delivery_failures, reasons=tuple(overall_reasons),
    )
