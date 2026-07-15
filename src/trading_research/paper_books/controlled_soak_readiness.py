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
7.2's own same-shaped statuses) NEVER activates or schedules anything. It
is never returned today: `_CROSS_BOOK_SIGNAL_AVAILABLE` documents a known,
deliberate gap (Section 4 of the milestone spec) rather than fabricating a
`False`/clear result — see the module-level constant below.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..shadow import readiness as shadow_readiness
from ..shadow import pause as pause_mod
from ..shadow.config import ShadowOperationsConfiguration
from ..storage import shadow_alerts_repositories as alerts_repo
from .cli_support import evaluate_paper_soak_readiness
from .config import PaperBooksConfiguration

POLICY_VERSION = "controlled-soak-readiness/v1"

STATUS_NOT_READY_PAPER_SOAK = "NOT_READY_PAPER_SOAK"
STATUS_NOT_READY_SHADOW_PAUSED = "NOT_READY_SHADOW_PAUSED"
STATUS_NOT_READY_SHADOW_KILLED = "NOT_READY_SHADOW_KILLED"
STATUS_NOT_READY_HEALTH_UNEXPLAINED = "NOT_READY_HEALTH_UNEXPLAINED"
STATUS_NOT_READY_CRITICAL_ALERTS = "NOT_READY_CRITICAL_ALERTS"
STATUS_NOT_READY_PROVIDER_HISTORY = "NOT_READY_PROVIDER_HISTORY"
STATUS_NOT_READY_RECONCILIATION = "NOT_READY_RECONCILIATION"
STATUS_NOT_READY_VALUATION = "NOT_READY_VALUATION"
STATUS_READY_FOR_MANUAL_SOAK = "READY_FOR_MANUAL_SOAK"
STATUS_READY_FOR_EXTENDED_MANUAL_SOAK = "READY_FOR_EXTENDED_MANUAL_SOAK"
STATUS_READY_FOR_RECURRING_ACTIVATION_REVIEW = "READY_FOR_RECURRING_ACTIVATION_REVIEW"

CONTROLLED_SOAK_STATUSES = (
    STATUS_NOT_READY_PAPER_SOAK, STATUS_NOT_READY_SHADOW_PAUSED, STATUS_NOT_READY_SHADOW_KILLED,
    STATUS_NOT_READY_HEALTH_UNEXPLAINED, STATUS_NOT_READY_CRITICAL_ALERTS, STATUS_NOT_READY_PROVIDER_HISTORY,
    STATUS_NOT_READY_RECONCILIATION, STATUS_NOT_READY_VALUATION, STATUS_READY_FOR_MANUAL_SOAK,
    STATUS_READY_FOR_EXTENDED_MANUAL_SOAK, STATUS_READY_FOR_RECURRING_ACTIVATION_REVIEW,
)

CLASSIFICATION_AUTHORITATIVE = "AUTHORITATIVE"
CLASSIFICATION_DERIVED = "DERIVED"
CLASSIFICATION_NOT_APPLICABLE = "NOT_APPLICABLE"
CLASSIFICATION_MISSING = "MISSING"
CHECK_CLASSIFICATIONS = (
    CLASSIFICATION_AUTHORITATIVE, CLASSIFICATION_DERIVED, CLASSIFICATION_NOT_APPLICABLE, CLASSIFICATION_MISSING,
)

# Milestone 9.1 Section 4: no Milestone 8/9 module persists a dedicated
# cross-book reconciliation/isolation-violation signal distinct from each
# book's own `reconcile_book` status (per-book reconciliation is already an
# input above, at `_paper_soak_check` — this is deliberately a SEPARATE,
# narrower signal: "did book isolation itself ever leak," which nothing
# here computes). Documented as a permanent `MISSING` input rather than a
# fabricated `False` until a future milestone adds real detection.
_CROSS_BOOK_SIGNAL_AVAILABLE = False


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

    def finish(status: str, reasons: tuple[str, ...], *, paper_soak_status: str = "NOT_EVALUATED",
               shadow_activation_status: str = "NOT_EVALUATED") -> ControlledSoakReadinessResult:
        return ControlledSoakReadinessResult(
            status=status, reasons=reasons, paper_soak_status=paper_soak_status,
            shadow_activation_status=shadow_activation_status, checks=tuple(checks), policy_version=POLICY_VERSION,
        )

    # 1/2. Shadow kill/pause state — AUTHORITATIVE (shadow_pause_state is the
    # single current-state row Milestone 7 already persists).
    pause_state = pause_mod.current_state(conn)
    checks.append(ReadinessCheck(
        name="shadow_kill_state", classification=CLASSIFICATION_AUTHORITATIVE, passed=not pause_state.is_killed,
        observed_value=pause_state.state, threshold_value="!= KILLED", source="shadow_pause_state",
        reason=pause_state.reason,
    ))
    if pause_state.is_killed:
        return finish(STATUS_NOT_READY_SHADOW_KILLED, (f"shadow kill switch is active: {pause_state.reason}",))

    checks.append(ReadinessCheck(
        name="shadow_pause_state", classification=CLASSIFICATION_AUTHORITATIVE, passed=not pause_state.is_blocking,
        observed_value=pause_state.state, threshold_value="== ACTIVE", source="shadow_pause_state",
        reason=pause_state.reason,
    ))
    if pause_state.is_blocking:
        return finish(STATUS_NOT_READY_SHADOW_PAUSED, (f"shadow pause state is {pause_state.state}: {pause_state.reason}",))

    # 3. Unexplained PAUSE_REQUIRED — reuses Milestone 7.2's own detector
    # rather than re-deriving it from raw `shadow_run_summaries`/
    # `shadow_run_health_checks` rows.
    unexplained = shadow_readiness._unexplained_pause_required_runs(conn)
    checks.append(ReadinessCheck(
        name="unexplained_pause_required", classification=CLASSIFICATION_AUTHORITATIVE, passed=not unexplained,
        observed_value=str(len(unexplained)), threshold_value="0", source="shadow_run_summaries/shadow_run_health_checks",
        reason=f"scheduler runs with unexplained PAUSE_REQUIRED: {unexplained}" if unexplained else "none",
    ))
    if unexplained:
        return finish(
            STATUS_NOT_READY_HEALTH_UNEXPLAINED,
            (f"scheduler run(s) {unexplained} report PAUSE_REQUIRED with no persisted health-check explanation",),
        )

    # 4. Unresolved CRITICAL operational alerts — AUTHORITATIVE now that
    # `shadow_alerts` carries a real (additive) `resolved_at` column
    # (Milestone 9.1); every alert raised before that column existed reads
    # back as unresolved, never fabricated as resolved.
    critical_alerts = alerts_repo.list_alerts(conn, severity="CRITICAL", unresolved_only=True)
    checks.append(ReadinessCheck(
        name="unresolved_critical_alerts", classification=CLASSIFICATION_AUTHORITATIVE, passed=not critical_alerts,
        observed_value=str(len(critical_alerts)), threshold_value="0", source="shadow_alerts",
        reason=f"unresolved CRITICAL alert_ids: {[a['alert_id'] for a in critical_alerts]}" if critical_alerts else "none",
    ))
    if critical_alerts:
        return finish(
            STATUS_NOT_READY_CRITICAL_ALERTS,
            (f"{len(critical_alerts)} unresolved CRITICAL alert(s) — investigate before considering activation",),
        )

    # 5-7, 9-10. Paper-soak side: reconciliation, valuation, lifecycle
    # failures, minimum completed cycles, minimum market days — all already
    # computed by Milestone 9's own `evaluate_paper_soak_readiness`, reused
    # wholesale (never re-derived here).
    paper_soak = evaluate_paper_soak_readiness(conn, as_of, paper_books_config)
    checks.append(ReadinessCheck(
        name="paper_soak_readiness", classification=CLASSIFICATION_DERIVED,
        passed=paper_soak["result"] in ("READY_FOR_MORE_MANUAL_SOAK", "READY_FOR_RECURRING_ACTIVATION_REVIEW"),
        observed_value=paper_soak["result"], threshold_value="READY_*", source="paper_books.evaluate_paper_soak_readiness",
        reason="; ".join(paper_soak["reasons"]) if paper_soak["reasons"] else "none",
    ))
    if paper_soak["result"] == "NOT_READY_RECONCILIATION":
        return finish(
            STATUS_NOT_READY_RECONCILIATION, tuple(paper_soak["reasons"]), paper_soak_status=paper_soak["result"],
        )
    if paper_soak["result"] == "NOT_READY_VALUATION":
        return finish(
            STATUS_NOT_READY_VALUATION, tuple(paper_soak["reasons"]), paper_soak_status=paper_soak["result"],
        )
    if paper_soak["result"] in (
        "NOT_READY_INSUFFICIENT_CYCLES", "NOT_READY_INSUFFICIENT_MARKET_DAYS", "NOT_READY_LIFECYCLE_FAILURES",
    ):
        return finish(
            STATUS_NOT_READY_PAPER_SOAK, tuple(paper_soak["reasons"]), paper_soak_status=paper_soak["result"],
        )

    # 8. Cross-book violation signal — documented, permanent MISSING (see
    # module docstring / `_CROSS_BOOK_SIGNAL_AVAILABLE`). Never blocks manual
    # or extended-manual soak; only caps the final advisory tier below.
    checks.append(ReadinessCheck(
        name="cross_book_violation_signal", classification=CLASSIFICATION_MISSING, passed=None,
        observed_value=None, threshold_value="0 violations", source="none (not yet persisted — known gap)",
        reason="no Milestone 8/9 module persists a cross-book reconciliation/isolation-violation signal distinct "
               "from each book's own reconciliation status",
    ))

    # 11-12. Real-provider-cycle history + provider/pricing readiness —
    # reuses Milestone 7.2's `evaluate_activation_readiness` (which itself
    # reuses `build_readiness_report`'s real-provider-cycle counting and
    # provider-health category) rather than re-deriving either.
    activation = shadow_readiness.evaluate_activation_readiness(conn, as_of, shadow_config, thresholds=shadow_thresholds)
    checks.append(ReadinessCheck(
        name="shadow_activation_readiness", classification=CLASSIFICATION_DERIVED,
        passed=activation.status in (
            shadow_readiness.ACTIVATION_READY_FOR_MANUAL_SHADOW_RUNS,
            shadow_readiness.ACTIVATION_READY_FOR_LIMITED_RECURRING_SHADOW,
        ),
        observed_value=activation.status, threshold_value="READY_*", source="shadow.readiness.evaluate_activation_readiness",
        reason="; ".join(activation.reasons),
    ))
    checks.append(ReadinessCheck(
        name="real_provider_cycle_count", classification=CLASSIFICATION_AUTHORITATIVE,
        passed=activation.readiness_report.real_provider_cycle_count >= (
            shadow_thresholds.min_real_provider_cycles_for_ready if shadow_thresholds
            else shadow_readiness.DEFAULT_MIN_REAL_PROVIDER_CYCLES_FOR_READY
        ),
        observed_value=str(activation.readiness_report.real_provider_cycle_count),
        threshold_value=str(
            shadow_thresholds.min_real_provider_cycles_for_ready if shadow_thresholds
            else shadow_readiness.DEFAULT_MIN_REAL_PROVIDER_CYCLES_FOR_READY
        ),
        source="shadow_run_summaries (cost_usd > 0)", reason="real (non-fixture) shadow-cycle count",
    ))

    if activation.status in (
        shadow_readiness.ACTIVATION_NOT_READY_PRICING, shadow_readiness.ACTIVATION_NOT_READY_PROVIDER_HEALTH,
        shadow_readiness.ACTIVATION_NOT_READY_INSUFFICIENT_HISTORY, shadow_readiness.ACTIVATION_ENVIRONMENTALLY_BLOCKED,
    ):
        return finish(
            STATUS_NOT_READY_PROVIDER_HISTORY, activation.reasons, paper_soak_status=paper_soak["result"],
            shadow_activation_status=activation.status,
        )
    if activation.status == shadow_readiness.ACTIVATION_NOT_READY_PAUSE_ACTIVE:
        # Real pause/kill already ruled out above (checks 1-2) — this branch
        # is `evaluate_activation_readiness`'s OTHER trigger for the same
        # status: the most recent run's own persisted
        # paper_reconciliation_mismatch/duplicate_prevention_violation
        # safety flags. Mapped to the closest Milestone 9.1 vocabulary
        # entry rather than inventing a new status for one shared branch.
        return finish(
            STATUS_NOT_READY_RECONCILIATION, activation.reasons, paper_soak_status=paper_soak["result"],
            shadow_activation_status=activation.status,
        )
    if activation.status == shadow_readiness.ACTIVATION_NOT_READY_HEALTH_UNEXPLAINED:
        return finish(
            STATUS_NOT_READY_HEALTH_UNEXPLAINED, activation.reasons, paper_soak_status=paper_soak["result"],
            shadow_activation_status=activation.status,
        )

    # 13. Every gate cleared — advisory tier, never an activation.
    minimum_market_days = paper_books_config.lifecycle.soak.minimum_market_days
    market_days_covered = paper_soak["market_days_covered"]
    if market_days_covered < minimum_market_days * 2:
        status = STATUS_READY_FOR_MANUAL_SOAK
        reasons = (f"market_days_covered {market_days_covered} has not yet reached 2x minimum_market_days "
                   f"{minimum_market_days} — continue manual soak",)
    elif not _CROSS_BOOK_SIGNAL_AVAILABLE:
        status = STATUS_READY_FOR_EXTENDED_MANUAL_SOAK
        reasons = ("extended manual-soak history satisfied, but the cross-book violation signal is MISSING — "
                   "recurring-activation review is withheld until that signal is persisted",)
    else:
        status = STATUS_READY_FOR_RECURRING_ACTIVATION_REVIEW
        reasons = ("every controlled-soak and shadow-activation gate cleared — a human may now review recurring "
                   "activation; nothing here enables it",)

    return finish(status, reasons, paper_soak_status=paper_soak["result"], shadow_activation_status=activation.status)
