"""Combined controlled-soak activation readiness (Milestone 9.1,
docs/milestone9-1-controlled-soak-readiness.md).

Milestone 9's own `paper_book_soak_readiness_cli`/`evaluate_paper_soak_readiness`
(paper-book-scoped: completed cycles, market days, lifecycle failures,
reconciliation, valuation) and Milestone 7.2's `shadow/readiness.py::
evaluate_activation_readiness` (shadow-operations-scoped: pause/kill,
unexplained PAUSE_REQUIRED, pricing, provider health, real-provider-cycle
history) each already answer half the question an operator actually needs
before considering any recurring paper-soak activation: "is EITHER half of
this system unsafe to leave running unattended." This module never
re-derives either half's own logic — it calls both, adds the two checks
neither one owns (unresolved CRITICAL operational alerts; a cross-book
reconciliation/isolation-violation signal), and produces one fail-closed,
advisory-only verdict in the Milestone 9.1 vocabulary.

`READY_FOR_RECURRING_ACTIVATION_REVIEW` (like Milestone 9's and Milestone
7.2's own same-shaped statuses) NEVER activates or schedules anything.
Milestone 9.2 closed the cross-book gap this docstring used to describe as
permanent: it is now reachable once a `PASSED` `cross_book_verification.py`
result is persisted at-or-before `as_of` and every other gate clears.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from ..research import provider_provenance
from ..shadow import readiness as shadow_readiness
from ..shadow import pause as pause_mod
from ..shadow.config import ShadowOperationsConfiguration
from ..storage import paper_books_repositories as pb_repo
from ..storage import shadow_alerts_repositories as alerts_repo
from .cli_support import evaluate_paper_soak_readiness
from .config import PaperBooksConfiguration

POLICY_VERSION = "controlled-soak-readiness/v2"

STATUS_NOT_READY_PAPER_SOAK = "NOT_READY_PAPER_SOAK"
STATUS_NOT_READY_SHADOW_PAUSED = "NOT_READY_SHADOW_PAUSED"
STATUS_NOT_READY_SHADOW_KILLED = "NOT_READY_SHADOW_KILLED"
STATUS_NOT_READY_HEALTH_UNEXPLAINED = "NOT_READY_HEALTH_UNEXPLAINED"
STATUS_NOT_READY_CRITICAL_ALERTS = "NOT_READY_CRITICAL_ALERTS"
STATUS_NOT_READY_PROVIDER_HISTORY = "NOT_READY_PROVIDER_HISTORY"
STATUS_NOT_READY_RECONCILIATION = "NOT_READY_RECONCILIATION"
STATUS_NOT_READY_VALUATION = "NOT_READY_VALUATION"
STATUS_NOT_READY_CROSS_BOOK = "NOT_READY_CROSS_BOOK"
STATUS_READY_FOR_MANUAL_SOAK = "READY_FOR_MANUAL_SOAK"
STATUS_READY_FOR_EXTENDED_MANUAL_SOAK = "READY_FOR_EXTENDED_MANUAL_SOAK"
STATUS_READY_FOR_RECURRING_ACTIVATION_REVIEW = "READY_FOR_RECURRING_ACTIVATION_REVIEW"

CONTROLLED_SOAK_STATUSES = (
    STATUS_NOT_READY_PAPER_SOAK, STATUS_NOT_READY_SHADOW_PAUSED, STATUS_NOT_READY_SHADOW_KILLED,
    STATUS_NOT_READY_HEALTH_UNEXPLAINED, STATUS_NOT_READY_CRITICAL_ALERTS, STATUS_NOT_READY_PROVIDER_HISTORY,
    STATUS_NOT_READY_RECONCILIATION, STATUS_NOT_READY_VALUATION, STATUS_NOT_READY_CROSS_BOOK,
    STATUS_READY_FOR_MANUAL_SOAK, STATUS_READY_FOR_EXTENDED_MANUAL_SOAK, STATUS_READY_FOR_RECURRING_ACTIVATION_REVIEW,
)

CLASSIFICATION_AUTHORITATIVE = "AUTHORITATIVE"
CLASSIFICATION_DERIVED = "DERIVED"
CLASSIFICATION_NOT_APPLICABLE = "NOT_APPLICABLE"
CLASSIFICATION_MISSING = "MISSING"
CHECK_CLASSIFICATIONS = (
    CLASSIFICATION_AUTHORITATIVE, CLASSIFICATION_DERIVED, CLASSIFICATION_NOT_APPLICABLE, CLASSIFICATION_MISSING,
)

# Milestone 9.2 closes the Milestone 9.1 Section 4 gap: `cross_book_verification.py`
# now persists an authoritative PASSED/FAILED/INSUFFICIENT_DATA signal
# (`paper_book_cross_book_verifications`), and this module reads the latest
# one at-or-before `as_of` below instead of a hardcoded "always MISSING"
# constant.


class ControlledSoakReadinessError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    classification: str
    passed: bool | None
    observed_value: str | None
    threshold_value: str | None
    source: str
    reason: str

    def __post_init__(self) -> None:
        if self.classification not in CHECK_CLASSIFICATIONS:
            raise ControlledSoakReadinessError(
                f"check {self.name!r} classification {self.classification!r} is not one of "
                f"{CHECK_CLASSIFICATIONS} — fails closed"
            )


@dataclass(frozen=True)
class ControlledSoakReadinessResult:
    status: str
    reasons: tuple[str, ...]
    paper_soak_status: str
    shadow_activation_status: str
    checks: tuple[ReadinessCheck, ...]
    policy_version: str

    def __post_init__(self) -> None:
        if self.status not in CONTROLLED_SOAK_STATUSES:
            raise ControlledSoakReadinessError(f"status {self.status!r} is not one of {CONTROLLED_SOAK_STATUSES} — fails closed")


def evaluate_controlled_soak_readiness(
    conn, as_of: datetime, paper_books_config: PaperBooksConfiguration,
    shadow_config: ShadowOperationsConfiguration, *,
    shadow_thresholds: shadow_readiness.ReadinessThresholds | None = None,
) -> ControlledSoakReadinessResult:
    checks: list[ReadinessCheck] = []
    failures: dict[str, tuple[str, tuple[str, ...]]] = {}

    def add(name: str, *, classification: str, passed: bool | None, observed: str | None,
            threshold: str | None, source: str, reason: str) -> None:
        checks.append(ReadinessCheck(name, classification, passed, observed, threshold, source, reason))

    pause_state = pause_mod.current_state(conn)
    add("shadow_kill_state", classification=CLASSIFICATION_AUTHORITATIVE, passed=not pause_state.is_killed,
        observed=pause_state.state, threshold="!= KILLED", source="shadow_pause_state", reason=pause_state.reason)
    if pause_state.is_killed:
        failures["kill"] = (STATUS_NOT_READY_SHADOW_KILLED, (f"shadow kill switch is active: {pause_state.reason}",))
    add("shadow_pause_state", classification=CLASSIFICATION_AUTHORITATIVE, passed=not pause_state.is_blocking,
        observed=pause_state.state, threshold="== ACTIVE", source="shadow_pause_state", reason=pause_state.reason)
    if pause_state.is_blocking and not pause_state.is_killed:
        failures["pause"] = (STATUS_NOT_READY_SHADOW_PAUSED, (f"shadow pause state is {pause_state.state}: {pause_state.reason}",))

    unexplained = shadow_readiness._unexplained_pause_required_runs(conn)
    add("unexplained_pause_required", classification=CLASSIFICATION_AUTHORITATIVE, passed=not unexplained,
        observed=str(len(unexplained)), threshold="0", source="shadow_run_summaries/shadow_run_health_checks",
        reason=f"scheduler runs with unexplained PAUSE_REQUIRED: {unexplained}" if unexplained else "none")
    if unexplained:
        failures["unexplained"] = (STATUS_NOT_READY_HEALTH_UNEXPLAINED,
            (f"scheduler run(s) {unexplained} report PAUSE_REQUIRED with no persisted health-check explanation",))

    critical_alerts = alerts_repo.list_alerts(conn, severity="CRITICAL", unresolved_only=True)
    add("unresolved_critical_alerts", classification=CLASSIFICATION_AUTHORITATIVE, passed=not critical_alerts,
        observed=str(len(critical_alerts)), threshold="0", source="shadow_alerts",
        reason=f"unresolved CRITICAL alert_ids: {[a['alert_id'] for a in critical_alerts]}" if critical_alerts else "none")
    if critical_alerts:
        failures["alerts"] = (STATUS_NOT_READY_CRITICAL_ALERTS,
            (f"{len(critical_alerts)} unresolved CRITICAL alert(s) — investigate before considering activation",))

    paper_soak = evaluate_paper_soak_readiness(conn, as_of, paper_books_config)
    paper_ready = paper_soak["result"] in ("READY_FOR_MORE_MANUAL_SOAK", "READY_FOR_RECURRING_ACTIVATION_REVIEW")
    add("paper_soak_readiness", classification=CLASSIFICATION_DERIVED, passed=paper_ready,
        observed=paper_soak["result"], threshold="READY_*", source="paper_books.evaluate_paper_soak_readiness",
        reason="; ".join(paper_soak["reasons"]) if paper_soak["reasons"] else "none")
    paper_failed = paper_soak.get("failed_checks", [])
    if not paper_failed and not paper_ready:
        mapping = {
            "NOT_READY_RECONCILIATION": "paper_reconciliation", "NOT_READY_VALUATION": "paper_valuation",
            "NOT_READY_LIFECYCLE_FAILURES": "lifecycle_failures",
            "NOT_READY_INSUFFICIENT_CYCLES": "minimum_completed_cycles",
            "NOT_READY_INSUFFICIENT_MARKET_DAYS": "minimum_market_days",
        }
        paper_failed = [{"name": mapping.get(paper_soak["result"], "paper_soak_readiness"),
                         "result": paper_soak["result"], "reasons": paper_soak["reasons"]}]
    for failure in paper_failed:
        add(failure["name"], classification=CLASSIFICATION_AUTHORITATIVE, passed=False,
            observed=failure["result"], threshold="ready", source="paper_books.evaluate_paper_soak_readiness",
            reason="; ".join(failure["reasons"]))
        if failure["name"] == "paper_reconciliation":
            failures["reconciliation"] = (STATUS_NOT_READY_RECONCILIATION, tuple(failure["reasons"]))
        elif failure["name"] == "paper_valuation":
            failures["valuation"] = (STATUS_NOT_READY_VALUATION, tuple(failure["reasons"]))
        elif failure["name"] == "lifecycle_failures":
            failures["lifecycle"] = (STATUS_NOT_READY_PAPER_SOAK, tuple(failure["reasons"]))
        else:
            failures.setdefault("paper_history", (STATUS_NOT_READY_PAPER_SOAK, tuple(failure["reasons"])))

    from .cross_book_verification import verification_is_stale
    latest_verification = pb_repo.latest_cross_book_verification_upto(conn, as_of.isoformat())
    stale = bool(latest_verification and verification_is_stale(conn, latest_verification, as_of))
    cross_book_status = "STALE" if stale else (latest_verification["status"] if latest_verification else "INSUFFICIENT_DATA")
    classification = CLASSIFICATION_MISSING if not latest_verification or stale else CLASSIFICATION_AUTHORITATIVE
    passed = None if classification == CLASSIFICATION_MISSING else cross_book_status == "PASSED"
    add("cross_book_violation_signal", classification=classification, passed=passed, observed=cross_book_status,
        threshold="PASSED", source="paper_book_cross_book_verifications" if latest_verification else "none",
        reason=(f"verification_id={latest_verification['verification_id']}, violation_count={latest_verification['violation_count']}"
                + ("; source state changed after verification" if stale else "")) if latest_verification
               else "no cross-book verification has been persisted at or before as_of")
    if cross_book_status == "FAILED":
        failures["cross_book"] = (STATUS_NOT_READY_CROSS_BOOK,
            (f"cross-book verification {latest_verification['verification_id']} is FAILED "
             f"({latest_verification['violation_count']} violation(s))",))

    provenance_summary = provider_provenance.compute_real_provider_history(conn, as_of)
    configured_thresholds = shadow_thresholds or shadow_readiness.ReadinessThresholds()
    real_provider_threshold = configured_thresholds.min_real_provider_cycles_for_ready
    real_provider_history_sufficient = provenance_summary.real_provider_success_cycle_count >= real_provider_threshold
    add("real_provider_success_cycle_count", classification=CLASSIFICATION_AUTHORITATIVE,
        passed=real_provider_history_sufficient, observed=str(provenance_summary.real_provider_success_cycle_count),
        threshold=str(real_provider_threshold), source="research_cycle_provider_provenance",
        reason="completed cycles with explicit SUCCEEDED real-provider provenance; cost_usd is never provider identity")
    for name in (
        "real_provider_attempt_cycle_count", "real_provider_failure_cycle_count", "partial_provider_cycle_count",
        "fixture_only_cycle_count", "real_evidence_only_cycle_count", "real_claude_only_cycle_count",
        "real_evidence_and_claude_cycle_count", "mixed_cycle_count", "unknown_cycle_count",
    ):
        add(name, classification=CLASSIFICATION_AUTHORITATIVE, passed=None,
            observed=str(getattr(provenance_summary, name)), threshold=None,
            source="research_cycle_provider_provenance", reason="informational provider-history breakdown")
    if not real_provider_history_sufficient:
        failures["provider_history"] = (STATUS_NOT_READY_PROVIDER_HISTORY,
            (f"real_provider_success_cycle_count {provenance_summary.real_provider_success_cycle_count} < minimum "
             f"{real_provider_threshold} (authoritative provenance, never cost_usd)",))

    # Split the legacy cost-derived provider floor out of controlled
    # readiness. Legacy callers retain it; this path sets that one floor to
    # zero and applies the authoritative successful-provenance floor above.
    activation_thresholds = replace(configured_thresholds, min_real_provider_cycles_for_ready=0)
    activation = shadow_readiness.evaluate_activation_readiness(
        conn, as_of, shadow_config, thresholds=activation_thresholds,
    )
    activation_ready = activation.status in (
        shadow_readiness.ACTIVATION_READY_FOR_MANUAL_SHADOW_RUNS,
        shadow_readiness.ACTIVATION_READY_FOR_LIMITED_RECURRING_SHADOW,
    )
    add("shadow_activation_readiness", classification=CLASSIFICATION_DERIVED, passed=activation_ready,
        observed=activation.status, threshold="READY_*", source="shadow.readiness.evaluate_activation_readiness",
        reason="; ".join(activation.reasons))
    if not activation_ready:
        if activation.status == shadow_readiness.ACTIVATION_NOT_READY_PAUSE_ACTIVE:
            failures.setdefault("reconciliation", (STATUS_NOT_READY_RECONCILIATION, activation.reasons))
        elif activation.status == shadow_readiness.ACTIVATION_NOT_READY_HEALTH_UNEXPLAINED:
            failures.setdefault("unexplained", (STATUS_NOT_READY_HEALTH_UNEXPLAINED, activation.reasons))
        else:
            failures.setdefault("inherited_shadow", (STATUS_NOT_READY_PROVIDER_HISTORY, activation.reasons))

    priority = (
        "kill", "pause", "unexplained", "alerts", "lifecycle", "reconciliation", "valuation",
        "paper_history", "cross_book", "provider_history", "inherited_shadow",
    )
    primary = next((failures[key] for key in priority if key in failures), None)
    if primary:
        status, reasons = primary
    else:
        minimum_market_days = paper_books_config.lifecycle.soak.minimum_market_days
        market_days_covered = paper_soak["market_days_covered"]
        if market_days_covered < minimum_market_days * 2:
            status = STATUS_READY_FOR_MANUAL_SOAK
            reasons = (f"market_days_covered {market_days_covered} has not reached 2x minimum {minimum_market_days}",)
        elif cross_book_status != "PASSED":
            status = STATUS_READY_FOR_EXTENDED_MANUAL_SOAK
            reasons = (f"cross-book verification is {cross_book_status}; continue manual soak",)
        else:
            status = STATUS_READY_FOR_RECURRING_ACTIVATION_REVIEW
            reasons = ("every gate cleared; a human may review recurring activation, but nothing is activated",)

    return ControlledSoakReadinessResult(
        status=status, reasons=tuple(reasons), paper_soak_status=paper_soak["result"],
        shadow_activation_status=activation.status, checks=tuple(checks), policy_version=POLICY_VERSION,
    )
