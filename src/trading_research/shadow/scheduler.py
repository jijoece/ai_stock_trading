"""External-scheduler-compatible single-run entry point (docs/milestone-7.md
Step 18, ADR 0005 Decisions 1-3). `run_due_shadow_cycle` performs **at most
one intended scheduled cycle** per call and returns — no loop, no daemon, no
self-installing OS schedule anywhere in this module.

Flow (mirrors `docs/milestone-7.md`'s "Core architecture" ASCII diagram and
Step 18's numbered behavior list):

    1. Load `config/shadow_operations.yaml`. `shadow_operations.enabled`
       false -> DISABLED no-op, before anything else is touched.
    2. Resolve intended schedule via `shadow/schedule.py::resolve_due_status`,
       consulting `shadow_scheduler_runs` for the last completed intended
       time. Not due -> the matching no-op status.
    3. Check pause/kill via `shadow/pause.py::current_state`. Blocking ->
       that explicit status, before any lease/provider/Claude call.
    4. Acquire a lease (`shadow/lease.py`) keyed on the intended-schedule
       identity. Conflict -> LEASE_HELD, a successful no-op.
    5. Reserve budget (`shadow/budget.py`) for the bounded symbol set. Reject
       -> release the lease, record BUDGET_REJECTED.
    6. Record the run start in `shadow_scheduler_runs`.
    7. Call the existing, unmodified `run_scheduled_research_cycle(...)` for
       the due symbol batch.
    8. Record actual usage and settle the budget reservation.
    9. Update `shadow_scheduler_runs` with final status/counts/timings.
    10. Release the lease — always, via try/finally.
    11. Return `ShadowCycleRunResult`, printable as JSON by a future thin
        CLI wrapper.

Scope boundary (explicit, matches this task's assignment and ADR 0005
Decision 6): this module does NOT modify `research/scheduled_cycle.py` and
does NOT wire per-role runtime budget checks into
`analyze_with_research_committee`'s internals. Cycle-level budget is
reserved before the cycle runs (Step 5 above) and the cycle is skipped
entirely if that reservation fails; `shadow/role_budget.py::check_role_budget`
exists and is unit-tested but is not called from inside
`run_scheduled_research_cycle` in this task, since that would require
modifying `research/scheduled_cycle.py`, which is explicitly out of scope.
This is a known, intentionally documented limitation — see this task's
scratchpad "Known limitations" entry.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..research import usage as usage_mod
from ..research.scheduled_cycle import ResearchCycleResult, ScheduledResearchConfiguration
from ..storage.research_repositories import compute_cycle_telemetry
from ..storage.shadow_alerts_repositories import save_run_summary
from ..storage.shadow_operations_repositories import (
    find_scheduler_run_by_intended_schedule,
    save_scheduler_run,
    update_scheduler_run,
)
from . import alerts as alerts_mod
from . import attempt_controller as attempt_controller_mod
from . import budget as budget_mod
from . import health as health_mod
from . import lease as lease_mod
from . import pause as pause_mod
from . import schedule as schedule_mod
from .config import ShadowOperationsConfiguration

Clock = Callable[[], datetime]

SCHEDULER_LEASE_KEY_PREFIX = "shadow-scheduler"
DEPLOYMENT_SOURCE_MANUAL = "manual-invocation"

# --- Outcome statuses ---------------------------------------------------------
# Superset of docs/milestone-7.md Step 19's schedule statuses plus the
# orchestration-only statuses this module itself determines (LEASE_HELD,
# PAUSED_*, KILLED, BUDGET_REJECTED, COMPLETED, PARTIALLY_COMPLETE, FAILED).

STATUS_DISABLED = "DISABLED"
STATUS_NOT_DUE = schedule_mod.STATUS_NOT_DUE
STATUS_MARKET_HOLIDAY = schedule_mod.STATUS_MARKET_HOLIDAY
STATUS_OUTSIDE_RUN_WINDOW = schedule_mod.STATUS_OUTSIDE_RUN_WINDOW
STATUS_MISSED_TOO_OLD = schedule_mod.STATUS_MISSED_TOO_OLD
STATUS_ALREADY_COMPLETED = schedule_mod.STATUS_ALREADY_COMPLETED
STATUS_KILLED = "KILLED"
STATUS_PAUSED = "PAUSED"
STATUS_LEASE_HELD = "LEASE_HELD"
STATUS_BUDGET_REJECTED = "BUDGET_REJECTED"
STATUS_COMPLETED = "COMPLETED"
STATUS_PARTIALLY_COMPLETE = "PARTIALLY_COMPLETE"
STATUS_FAILED = "FAILED"

# Statuses that represent a successful no-op — exit-code-friendly, never an
# exception (docs/milestone-7.md Step 18: "not-due is a successful no-op").
NO_OP_STATUSES = (
    STATUS_DISABLED, STATUS_NOT_DUE, STATUS_MARKET_HOLIDAY, STATUS_OUTSIDE_RUN_WINDOW,
    STATUS_MISSED_TOO_OLD, STATUS_ALREADY_COMPLETED, STATUS_LEASE_HELD,
)
# Blocking-but-not-erroring statuses (explicit status is returned, not an
# exception, but nothing ran).
BLOCKED_STATUSES = (STATUS_KILLED, STATUS_PAUSED, STATUS_BUDGET_REJECTED)


class SchedulerError(RuntimeError):
    pass


@dataclass(frozen=True)
class ShadowCycleRunResult:
    """Every field a CLI would need to print as JSON (docs/milestone-7.md
    Step 18: "Return structured JSON and exit code" — this dataclass is the
    structured payload; the exit-code mapping is the future thin CLI
    wrapper's job, using `is_successful_no_op`/`is_error` below)."""

    status: str
    scheduler_run_id: str | None
    intended_schedule_id: str | None
    intended_schedule_time: datetime | None
    cycle_id: str | None
    symbols_attempted: int
    symbols_completed: int
    symbols_skipped: int
    budget_reservation_id: str | None
    budget_reserved_usd: str | None
    budget_consumed_usd: str | None
    failure_reason: str | None
    lease_owner: str | None
    reason: str

    @property
    def is_successful_no_op(self) -> bool:
        return self.status in NO_OP_STATUSES

    @property
    def is_blocked(self) -> bool:
        return self.status in BLOCKED_STATUSES

    @property
    def is_error(self) -> bool:
        return self.status == STATUS_FAILED


def _no_op(status: str, reason: str, *, intended_schedule_id: str | None = None,
           intended_schedule_time: datetime | None = None) -> ShadowCycleRunResult:
    return ShadowCycleRunResult(
        status=status, scheduler_run_id=None, intended_schedule_id=intended_schedule_id,
        intended_schedule_time=intended_schedule_time, cycle_id=None, symbols_attempted=0, symbols_completed=0,
        symbols_skipped=0, budget_reservation_id=None, budget_reserved_usd=None, budget_consumed_usd=None,
        failure_reason=None, lease_owner=None, reason=reason,
    )


def _last_completed_intended_time(conn: sqlite3.Connection, config: ShadowOperationsConfiguration, today) -> datetime | None:
    """Looks up the most recent COMPLETED scheduler run's intended time,
    scanning back from today through the schedule's own candidate days so
    this is a real point lookup, not a full-table scan. Returns `None` if
    no completed run for any candidate slot is found (a first-ever run)."""
    latest: datetime | None = None
    all_candidate_days = (
        schedule_mod.actionable_window_days(today, config.shadow_operations.max_catch_up_cycles)
        + schedule_mod.too_old_margin_days(today, config.shadow_operations.max_catch_up_cycles)
    )
    for day in all_candidate_days:
        intended_time = schedule_mod.intended_schedule_time_for_day(day, config)
        sid = schedule_mod.intended_schedule_id(intended_time)
        row = find_scheduler_run_by_intended_schedule(conn, sid)
        if row is not None and row["status"] == STATUS_COMPLETED:
            if latest is None or intended_time > latest:
                latest = intended_time
    return latest


def _default_alert_sinks() -> tuple[alerts_mod.AlertSink, ...]:
    """Persistence-only plus a structured-log sink — no webhook/email sink
    exists in this repository yet (ADR 0005 Decision 8). A caller wanting a
    different sink set can bypass this default by calling
    `run_due_shadow_cycle`'s internals directly; this orchestrator itself has
    no configuration surface for sink selection, matching the "provider-
    neutral, no third-party dependency without justification" instruction."""
    return (alerts_mod.PersistenceOnlyAlertSink(), alerts_mod.LogAlertSink())


def _raise(
    conn: sqlite3.Connection, *, severity: str, alert_type: str, message: str, context: dict, clock: Clock,
) -> None:
    """Thin wrapper around `alerts.raise_alert` used throughout this module
    — swallows nothing about the underlying alert (it is always persisted
    first per `raise_alert`'s own contract) but keeps every call site here
    to one line."""
    alerts_mod.raise_alert(
        conn,
        alerts_mod.OperationalAlert(severity=severity, alert_type=alert_type, message=message, context=context, created_at=clock()),
        _default_alert_sinks(), clock,
    )


def _save_no_op_summary(
    conn: sqlite3.Connection, *, scheduler_run_id: str | None, intended_schedule_id: str | None, status: str,
    reason: str, clock: Clock,
) -> None:
    """Every invocation — including lightweight successful no-ops
    (DISABLED/NOT_DUE/MARKET_HOLIDAY/etc.) — gets a `shadow_run_summaries`
    row (this task's explicit requirement: "ALL invocations should still get
    a shadow_run_summaries row"). A no-op that never reached lease/budget
    stages has no scheduler_run_id yet (no `shadow_scheduler_runs` row was
    ever written for it, by design — see `_no_op`), so a synthetic
    deterministic id derived from the intended slot (or a fresh uuid when
    even that is unknown, e.g. DISABLED before schedule resolution) is used
    instead as the summary's primary key. No health evaluation is performed
    for these lightweight paths — that would force a full health-eval cycle
    onto a NOT_DUE no-op, which this task explicitly says not to do."""
    summary_id = scheduler_run_id or f"shadow-no-op-{intended_schedule_id or uuid.uuid4().hex}"
    now = clock()
    save_run_summary(
        conn,
        {
            "scheduler_run_id": summary_id, "intended_schedule_id": intended_schedule_id or "unknown",
            "policy_version": "no-op", "health_status": "NOT_EVALUATED",
            "health_reasons_json": json.dumps([f"{status}: {reason}"]),
            "provider_success_rate": None, "evidence_completeness_rate": None, "claude_role_success_rate": None,
            "retry_rate": None, "retry_exhaustion_rate": None, "unsupported_claim_rate": None,
            "output_truncation_rate": None, "latency_seconds": None, "input_tokens": None, "output_tokens": None,
            "cost_usd": None, "paper_reconciliation_mismatch": 0, "duplicate_prevention_violation": 0,
            "cycle_duration_seconds": None, "created_at": now.isoformat(),
        },
    )


def _build_health_inputs_from_cycle_result(
    conn: sqlite3.Connection, cycle_result: ResearchCycleResult | None, *, symbols_attempted: int,
    cycle_duration_seconds: float,
) -> health_mod.CycleHealthInputs:
    """Computes real aggregate rates from `ResearchCycleResult.symbol_results`
    plus, since docs/milestone-7.1.md Step 18, a real `ResearchCycleTelemetry`
    join against `research_attempts`/`research_attempt_failures` for every
    `research_run_id` the cycle actually produced (`storage/
    research_repositories.py::compute_cycle_telemetry` — the one authoritative
    query, not a duplicated in-memory counter).

    Fields that still stay `None` are `None` because the underlying data
    genuinely does not exist for this cycle (e.g. every symbol failed before
    a research_run_id was ever created) — never a fabricated 0.0/0:
      * `provider_success_rate`: `symbols_completed / symbols_attempted`.
      * `evidence_completeness_rate`: fraction of symbol results whose
        `evidence_outcome` is a "COMPLETE*" outcome.
      * `claude_role_success_rate`/`retry_rate`/`retry_exhaustion_rate`/
        `unsupported_claim_rate`/`output_truncation_rate`/`input_tokens`/
        `output_tokens`/`cost_usd`: derived from `ResearchCycleTelemetry`,
        `None` when `telemetry.attempt_count == 0` (no Claude attempt ran
        this cycle at all, e.g. every symbol was completeness-blocked).
      * `pricing_configured`: `False` only when `telemetry.pricing_status ==
        "PRICING_NOT_CONFIGURED"` — every other status (including
        `NOT_APPLICABLE`/`NO_DATA` for deterministic/scripted cycles, which
        never require pricing) is conservatively `True`.
    """
    if cycle_result is None or symbols_attempted == 0:
        return health_mod.CycleHealthInputs(
            provider_success_rate=None, evidence_completeness_rate=None, claude_role_success_rate=None,
            retry_rate=None, retry_exhaustion_rate=None, unsupported_claim_rate=None, output_truncation_rate=None,
            latency_seconds=None, input_tokens=None, output_tokens=None, cost_usd=None, pricing_configured=True,
            paper_reconciliation_mismatch=False, duplicate_prevention_violation=False,
            cycle_duration_seconds=cycle_duration_seconds,
        )

    completed = sum(1 for r in cycle_result.symbol_results if r.status == "COMPLETED")
    provider_success_rate = completed / symbols_attempted if symbols_attempted > 0 else None

    outcomes = [r.evidence_outcome for r in cycle_result.symbol_results if r.evidence_outcome is not None]
    if outcomes:
        complete_count = sum(1 for o in outcomes if o.startswith("COMPLETE"))
        evidence_completeness_rate = complete_count / len(outcomes)
    else:
        evidence_completeness_rate = None

    research_run_ids = tuple(
        r.research_run_id for r in cycle_result.symbol_results if r.research_run_id is not None
    )
    telemetry = compute_cycle_telemetry(conn, research_run_ids)

    if telemetry.attempt_count > 0:
        claude_role_success_rate = telemetry.successful_attempt_count / telemetry.attempt_count
        retry_rate = telemetry.retry_count / telemetry.attempt_count
        unsupported_claim_rate = telemetry.unsupported_claim_count / telemetry.attempt_count
        output_truncation_rate = telemetry.output_truncation_count / telemetry.attempt_count
    else:
        claude_role_success_rate = None
        retry_rate = None
        unsupported_claim_rate = None
        output_truncation_rate = None
    retry_exhaustion_rate = (
        telemetry.retry_exhaustion_count / len(research_run_ids) if research_run_ids else None
    )
    pricing_configured = telemetry.pricing_status != "PRICING_NOT_CONFIGURED"

    return health_mod.CycleHealthInputs(
        provider_success_rate=provider_success_rate, evidence_completeness_rate=evidence_completeness_rate,
        claude_role_success_rate=claude_role_success_rate, retry_rate=retry_rate,
        retry_exhaustion_rate=retry_exhaustion_rate, unsupported_claim_rate=unsupported_claim_rate,
        output_truncation_rate=output_truncation_rate,
        latency_seconds=(telemetry.latency_ms / 1000) if telemetry.latency_ms is not None else None,
        input_tokens=telemetry.input_tokens, output_tokens=telemetry.output_tokens,
        cost_usd=telemetry.priced_usage_cost_usd, pricing_configured=pricing_configured,
        paper_reconciliation_mismatch=False, duplicate_prevention_violation=False,
        cycle_duration_seconds=cycle_duration_seconds,
    )


def _save_health_summary(
    conn: sqlite3.Connection, *, scheduler_run_id: str, intended_schedule_id: str, health_result: health_mod.HealthResult,
    inputs: health_mod.CycleHealthInputs, clock: Clock,
) -> None:
    now = clock()
    save_run_summary(
        conn,
        {
            "scheduler_run_id": scheduler_run_id, "intended_schedule_id": intended_schedule_id,
            "policy_version": health_result.policy_version, "health_status": health_result.status,
            "health_reasons_json": json.dumps(list(health_result.reasons)),
            "provider_success_rate": inputs.provider_success_rate,
            "evidence_completeness_rate": inputs.evidence_completeness_rate,
            "claude_role_success_rate": inputs.claude_role_success_rate, "retry_rate": inputs.retry_rate,
            "retry_exhaustion_rate": inputs.retry_exhaustion_rate,
            "unsupported_claim_rate": inputs.unsupported_claim_rate,
            "output_truncation_rate": inputs.output_truncation_rate, "latency_seconds": inputs.latency_seconds,
            "input_tokens": inputs.input_tokens, "output_tokens": inputs.output_tokens,
            "cost_usd": str(inputs.cost_usd) if inputs.cost_usd is not None else None,
            "paper_reconciliation_mismatch": int(inputs.paper_reconciliation_mismatch),
            "duplicate_prevention_violation": int(inputs.duplicate_prevention_violation),
            "cycle_duration_seconds": inputs.cycle_duration_seconds, "created_at": now.isoformat(),
        },
    )


def run_due_shadow_cycle(
    *,
    now: datetime,
    conn: sqlite3.Connection,
    shadow_config: ShadowOperationsConfiguration,
    cycle_configuration: ScheduledResearchConfiguration,
    candidate_symbols: Callable[[], tuple[str, ...]],
    run_cycle: Callable[..., ResearchCycleResult],
    cycle_kwargs_builder: Callable[[tuple[str, ...], datetime], dict[str, Any]],
    pricing_entries: tuple[Any, ...],
    clock: Clock,
    research_provider_name: str = "deterministic",
    research_model_name: str | None = None,
    research_roles: tuple[str, ...] | None = None,
    owner: str | None = None,
    deployment_source: str = DEPLOYMENT_SOURCE_MANUAL,
) -> ShadowCycleRunResult:
    """Single-invocation orchestrator wrapping `run_scheduled_research_cycle`
    (injected as `run_cycle` so this module never imports it directly,
    keeping this orchestrator's own test surface fast/mockable — the real
    CLI wrapper, added in a later task, passes the real function).

    `candidate_symbols`: zero-arg callable returning the bounded symbol
    tuple for this cycle (the caller already knows the universe/candidate
    selection; this module only bounds *when* and *whether* to run, not
    *which* symbols — matching ADR 0005 Decision 2's "does not reimplement
    cycle logic").

    `cycle_kwargs_builder(symbols, as_of) -> dict`: builds the remaining
    keyword arguments `run_cycle` needs (evidence providers, repositories,
    research provider, etc.) — kept as a builder rather than pre-built
    kwargs so real provider construction (which may need `conn`) happens
    only if the cycle actually proceeds past all preflight gates, never
    speculatively.

    `research_provider_name`/`research_model_name` (docs/milestone-7.1.md
    Step 11): the ACTUAL configured research provider/model — the same
    values the caller passes into `cycle_kwargs_builder`'s own
    `research_provider_name`/`research_model_name` kwargs for `run_cycle`,
    and the single source of truth for `CycleIntent.provider`/`.model_name`
    below (REPLACES the previous `cycle_configuration.provider_mode`-based
    guess entirely — that mapping conflated the evidence-provider mode
    "fixture"/"real" with the Claude-provider taxonomy
    "deterministic"/"scripted"/"anthropic", which are two different axes).
    Defaults preserve every existing caller's behavior (`"deterministic"`,
    `None`) exactly.

    `research_roles` (docs/milestone-7.1.md Steps 12-14, default `None`):
    the exact `research_configuration.roles` tuple the caller's
    `cycle_kwargs_builder`/`run_cycle` will invoke. When supplied, every
    role attempt is budget-checked immediately before its provider call via
    a per-symbol `shadow.attempt_controller.ShadowResearchAttemptController`
    (threaded into `run_cycle` as `attempt_controller_factory`). `None`
    (every existing caller) disables per-attempt enforcement exactly as
    before this task — only the cycle-level reservation gates the run.
    """
    # --- Step 1: enabled check — before anything else is touched. ----------
    if not shadow_config.shadow_operations.enabled:
        result = _no_op(STATUS_DISABLED, "shadow_operations.enabled is false — no-op before any state was touched")
        _save_no_op_summary(
            conn, scheduler_run_id=None, intended_schedule_id=None, status=result.status, reason=result.reason,
            clock=clock,
        )
        return result

    # --- Step 2: resolve intended schedule. ---------------------------------
    today = now.astimezone(ZoneInfo(shadow_config.shadow_operations.run_window_timezone)).date()
    last_completed = _last_completed_intended_time(conn, shadow_config, today)
    resolution = schedule_mod.resolve_due_status(now, last_completed, shadow_config, clock)
    if not resolution.is_actionable:
        result = _no_op(
            resolution.status, resolution.reason, intended_schedule_id=resolution.intended_schedule_id,
            intended_schedule_time=resolution.intended_schedule_time,
        )
        if resolution.status == schedule_mod.STATUS_MISSED_TOO_OLD:
            _raise(
                conn, severity=alerts_mod.SEVERITY_WARNING, alert_type=alerts_mod.ALERT_TYPE_SCHEDULER_MISSED_RUN,
                message=f"scheduled shadow cycle missed and aged out of the catch-up window: {resolution.reason}",
                context={"intended_schedule_id": resolution.intended_schedule_id or "unknown", "status": resolution.status},
                clock=clock,
            )
        _save_no_op_summary(
            conn, scheduler_run_id=None, intended_schedule_id=resolution.intended_schedule_id,
            status=result.status, reason=result.reason, clock=clock,
        )
        return result

    intended_schedule_id = resolution.intended_schedule_id
    intended_schedule_time = resolution.intended_schedule_time
    assert intended_schedule_id is not None and intended_schedule_time is not None

    # Idempotency: a scheduler run row already exists (and is COMPLETED) for
    # this exact intended slot -> no-op, even if resolve_due_status somehow
    # returned actionable (defense in depth; ADR 0005 Decision 3: lease and
    # idempotency are separate, complementary mechanisms).
    existing_run = find_scheduler_run_by_intended_schedule(conn, intended_schedule_id)
    if existing_run is not None and existing_run["status"] == STATUS_COMPLETED:
        result = _no_op(
            STATUS_ALREADY_COMPLETED, f"scheduler run {existing_run['scheduler_run_id']} already completed",
            intended_schedule_id=intended_schedule_id, intended_schedule_time=intended_schedule_time,
        )
        _save_no_op_summary(
            conn, scheduler_run_id=None, intended_schedule_id=intended_schedule_id, status=result.status,
            reason=result.reason, clock=clock,
        )
        return result

    # --- Step 3: pause/kill check — before lease/provider/Claude. -----------
    pause_state = pause_mod.current_state(conn)
    if pause_state.is_killed:
        result = _no_op(
            STATUS_KILLED, f"system is KILLED: {pause_state.reason}", intended_schedule_id=intended_schedule_id,
            intended_schedule_time=intended_schedule_time,
        )
        _raise(
            conn, severity=alerts_mod.SEVERITY_CRITICAL, alert_type=alerts_mod.ALERT_TYPE_KILL_SWITCH_ACTIVATED,
            message=f"scheduled shadow cycle blocked: system is KILLED ({pause_state.reason})",
            context={"intended_schedule_id": intended_schedule_id, "pause_reason": pause_state.reason}, clock=clock,
        )
        _save_no_op_summary(
            conn, scheduler_run_id=None, intended_schedule_id=intended_schedule_id, status=result.status,
            reason=result.reason, clock=clock,
        )
        return result
    if pause_state.is_blocking:
        result = _no_op(
            STATUS_PAUSED, f"system is {pause_state.state}: {pause_state.reason}",
            intended_schedule_id=intended_schedule_id, intended_schedule_time=intended_schedule_time,
        )
        _raise(
            conn, severity=alerts_mod.SEVERITY_WARNING, alert_type=alerts_mod.ALERT_TYPE_PAUSE_ACTIVATED,
            message=f"scheduled shadow cycle blocked: system is {pause_state.state} ({pause_state.reason})",
            context={
                "intended_schedule_id": intended_schedule_id, "pause_state": pause_state.state,
                "pause_reason": pause_state.reason,
            },
            clock=clock,
        )
        _save_no_op_summary(
            conn, scheduler_run_id=None, intended_schedule_id=intended_schedule_id, status=result.status,
            reason=result.reason, clock=clock,
        )
        return result

    # --- Step 4: acquire lease. ----------------------------------------------
    lease_key = f"{SCHEDULER_LEASE_KEY_PREFIX}:{intended_schedule_id}"
    lease_owner = owner or lease_mod.generate_owner_token()
    lease_result = lease_mod.acquire(
        conn, lease_key, lease_owner, shadow_config.shadow_operations.lease_ttl_seconds, clock,
    )
    if isinstance(lease_result, lease_mod.LeaseConflict):
        result = _no_op(
            STATUS_LEASE_HELD, f"lease {lease_key!r} held by {lease_result.held_by!r} until {lease_result.expires_at.isoformat()}",
            intended_schedule_id=intended_schedule_id, intended_schedule_time=intended_schedule_time,
        )
        _raise(
            conn, severity=alerts_mod.SEVERITY_WARNING, alert_type=alerts_mod.ALERT_TYPE_LEASE_CONFLICT,
            message=f"lease {lease_key!r} held by another owner — scheduled cycle skipped this invocation",
            context={
                "intended_schedule_id": intended_schedule_id, "lease_key": lease_key,
                "held_by": lease_result.held_by, "expires_at": lease_result.expires_at.isoformat(),
            },
            clock=clock,
        )
        _save_no_op_summary(
            conn, scheduler_run_id=None, intended_schedule_id=intended_schedule_id, status=result.status,
            reason=result.reason, clock=clock,
        )
        return result
    lease_handle = lease_result

    scheduler_run_id = f"shadow-run-{uuid.uuid4().hex}"
    try:
        # --- Step 5: reserve budget. -----------------------------------------
        symbols = candidate_symbols()
        bounded_symbols = tuple(symbols[: shadow_config.budgets.max_symbols_per_cycle])

        intent = budget_mod.CycleIntent(
            provider=research_provider_name, model_name=research_model_name,
            max_symbols_per_cycle=shadow_config.budgets.max_symbols_per_cycle,
            max_roles_per_symbol=shadow_config.budgets.max_roles_per_symbol,
            max_attempts_per_role=shadow_config.budgets.max_attempts_per_role,
            max_output_tokens_per_cycle=shadow_config.budgets.max_output_tokens_per_cycle,
            max_input_tokens_per_cycle=shadow_config.budgets.max_input_tokens_per_cycle,
            max_latency_seconds_per_cycle=shadow_config.budgets.max_latency_seconds_per_cycle,
        )
        # `intent.provider`/`.model_name` come directly from the caller's
        # `research_provider_name`/`research_model_name` — the actual
        # configured Claude provider/model (docs/milestone-7.1.md Step 11),
        # not derived from `cycle_configuration.provider_mode` (that field is
        # the evidence-provider mode "fixture"/"real", a different axis).
        # A caller wiring a real-Claude scheduled run supplies
        # `research_provider_name="anthropic"` and the real model name so
        # this reservation's pricing lookup and the pre-call role-budget
        # checks (Step 13) both key off the SAME provider/model identity as
        # every downstream Claude call.
        try:
            estimate = budget_mod.estimate_cycle_cost(intent, pricing_entries, now.date().isoformat())
        except budget_mod.BudgetConfigError as exc:
            _save_failed_run(
                conn, scheduler_run_id=scheduler_run_id, intended_schedule_id=intended_schedule_id,
                intended_schedule_time=intended_schedule_time, configuration_hash=cycle_configuration.config_hash,
                mode=shadow_config.shadow_operations.mode, lease_handle=lease_handle, status=STATUS_BUDGET_REJECTED,
                failure_reason=str(exc), deployment_source=deployment_source, clock=clock,
            )
            _raise(
                conn, severity=alerts_mod.SEVERITY_ERROR, alert_type=alerts_mod.ALERT_TYPE_BUDGET_EXCEEDED,
                message=f"scheduled shadow cycle blocked: pricing required but not configured ({exc})",
                context={"intended_schedule_id": intended_schedule_id, "scheduler_run_id": scheduler_run_id},
                clock=clock,
            )
            _save_no_op_summary(
                conn, scheduler_run_id=scheduler_run_id, intended_schedule_id=intended_schedule_id,
                status=STATUS_BUDGET_REJECTED, reason="pricing required but not configured", clock=clock,
            )
            return ShadowCycleRunResult(
                status=STATUS_BUDGET_REJECTED, scheduler_run_id=scheduler_run_id,
                intended_schedule_id=intended_schedule_id, intended_schedule_time=intended_schedule_time,
                cycle_id=None, symbols_attempted=0, symbols_completed=0, symbols_skipped=0,
                budget_reservation_id=None, budget_reserved_usd=None, budget_consumed_usd=None,
                failure_reason=str(exc), lease_owner=lease_owner, reason="pricing required but not configured",
            )

        reservation_result = budget_mod.reserve_budget(
            conn, f"shadow-budget:{intended_schedule_id}", intent, estimate,
            max_actual_cost_per_day_usd=Decimal(str(shadow_config.budgets.max_actual_cost_per_day_usd)),
            max_actual_cost_per_month_usd=Decimal(str(shadow_config.budgets.max_actual_cost_per_month_usd)),
            clock=clock,
        )
        if isinstance(reservation_result, budget_mod.BudgetRejected):
            _save_failed_run(
                conn, scheduler_run_id=scheduler_run_id, intended_schedule_id=intended_schedule_id,
                intended_schedule_time=intended_schedule_time, configuration_hash=cycle_configuration.config_hash,
                mode=shadow_config.shadow_operations.mode, lease_handle=lease_handle, status=STATUS_BUDGET_REJECTED,
                failure_reason=reservation_result.reason, deployment_source=deployment_source, clock=clock,
            )
            _raise(
                conn, severity=alerts_mod.SEVERITY_ERROR, alert_type=alerts_mod.ALERT_TYPE_BUDGET_EXCEEDED,
                message=f"scheduled shadow cycle blocked: budget rejected ({reservation_result.cap_name}: {reservation_result.reason})",
                context={
                    "intended_schedule_id": intended_schedule_id, "scheduler_run_id": scheduler_run_id,
                    "cap_name": reservation_result.cap_name, "remaining": str(reservation_result.remaining),
                    "requested": str(reservation_result.requested),
                },
                clock=clock,
            )
            _save_no_op_summary(
                conn, scheduler_run_id=scheduler_run_id, intended_schedule_id=intended_schedule_id,
                status=STATUS_BUDGET_REJECTED, reason=f"budget rejected: {reservation_result.cap_name}", clock=clock,
            )
            return ShadowCycleRunResult(
                status=STATUS_BUDGET_REJECTED, scheduler_run_id=scheduler_run_id,
                intended_schedule_id=intended_schedule_id, intended_schedule_time=intended_schedule_time,
                cycle_id=None, symbols_attempted=0, symbols_completed=0, symbols_skipped=0,
                budget_reservation_id=None, budget_reserved_usd=None, budget_consumed_usd=None,
                failure_reason=reservation_result.reason, lease_owner=lease_owner,
                reason=f"budget rejected: {reservation_result.cap_name}",
            )
        reservation = reservation_result

        # docs/milestone-7.1.md Steps 12-14: build one attempt-controller
        # factory for this cycle, reusing the EXACT same pricing entry
        # `estimate_cycle_cost` already selected for the reservation above
        # (same provider/model/as_of-date inputs to `select_pricing` — never
        # a second, potentially-inconsistent lookup). Only activated when
        # the caller supplies `research_roles` (Step 20's CLI wiring does);
        # `research_roles=None` (every existing caller/test) preserves prior
        # behavior exactly — no per-attempt enforcement, `attempt_controller_
        # factory=None` passed to `run_cycle` unchanged from before this task.
        attempt_controller_factory = None
        if research_roles is not None:
            pricing_for_roles = usage_mod.select_pricing(
                pricing_entries, intent.provider, intent.model_name or "", now.date().isoformat(),
            )

            def attempt_controller_factory(symbol: str) -> attempt_controller_mod.ShadowResearchAttemptController:
                return attempt_controller_mod.ShadowResearchAttemptController(
                    conn=conn, reservation=reservation, provider=intent.provider, allowed_roles=research_roles,
                    max_roles_per_symbol=shadow_config.budgets.max_roles_per_symbol,
                    max_attempts_per_role=shadow_config.budgets.max_attempts_per_role,
                    max_output_tokens_per_role=shadow_config.budgets.max_output_tokens_per_cycle,
                    max_input_tokens_per_role=shadow_config.budgets.max_input_tokens_per_cycle,
                    max_latency_seconds_per_role=shadow_config.budgets.max_latency_seconds_per_cycle,
                    pricing=pricing_for_roles, clock=clock, scheduler_run_id=scheduler_run_id, cycle_id=None,
                )

        # --- Step 6: record run start. ---------------------------------------
        start_time = clock()
        save_scheduler_run(
            conn,
            {
                "scheduler_run_id": scheduler_run_id, "intended_schedule_id": intended_schedule_id,
                "scheduled_time": intended_schedule_time.isoformat(), "actual_start_at": start_time.isoformat(),
                "actual_finish_at": None, "cycle_id": None, "configuration_hash": cycle_configuration.config_hash,
                "mode": shadow_config.shadow_operations.mode, "lease_owner": lease_owner,
                "lease_expires_at": lease_handle.expires_at.isoformat(), "status": "RUNNING", "pause_state": pause_state.state,
                "budget_reservation_id": reservation.reservation_id,
                "budget_reserved_usd": str(reservation.reserved_estimated_cost_usd), "budget_consumed_usd": "0",
                "symbols_attempted": 0, "symbols_completed": 0, "symbols_skipped": 0, "provider_failures": 0,
                "research_failures": 0, "paper_submissions": 0, "alert_count": 0, "failure_reason": None,
                "operator_action": None, "deployment_source": deployment_source, "created_at": start_time.isoformat(),
            },
        )

        # --- Step 7: call the existing, unmodified scheduled-cycle service. --
        cycle_result: ResearchCycleResult | None = None
        failure_reason: str | None = None
        try:
            cycle_kwargs = cycle_kwargs_builder(bounded_symbols, intended_schedule_time)
            cycle_result = run_cycle(
                as_of=intended_schedule_time, symbols=bounded_symbols, configuration=cycle_configuration, conn=conn,
                clock=clock, attempt_controller_factory=attempt_controller_factory, **cycle_kwargs,
            )
        except Exception as exc:  # cycle-level crash — visible as a partial/failed run, never silently lost
            failure_reason = str(exc)

        finish_time = clock()

        # --- Step 8: record actual usage and settle. -------------------------
        # No real usage metadata is available from a fixture/deterministic
        # cycle_result (ResearchCycleResult carries no token/cost fields) —
        # actual usage is recorded as zero for non-anthropic providers,
        # matching Milestone 6's existing "only real Claude usage is priced"
        # scope (ADR 0005 Decision 5). A real-Claude scheduled run's actual
        # token/cost capture is the caller's `cycle_kwargs_builder`/
        # `run_cycle` responsibility to surface — this orchestrator only
        # settles the reservation, it does not itself parse Claude response
        # metadata.
        budget_mod.record_actual_usage(
            conn, reservation.reservation_id, actual_cost_usd=Decimal("0"), actual_input_tokens=0,
            actual_output_tokens=0, actual_latency_seconds=int((finish_time - start_time).total_seconds()),
            provider=intent.provider, model_name=intent.model_name, clock=clock,
        )
        budget_mod.settle_reservation(conn, reservation.reservation_id, clock)
        updated_reservation = budget_mod.remaining_reservation_budget(conn, reservation.reservation_id)

        # --- Step 9: update shadow_scheduler_runs with final status. ---------
        if cycle_result is not None:
            symbols_completed = sum(1 for r in cycle_result.symbol_results if r.status == "COMPLETED")
            symbols_failed = sum(1 for r in cycle_result.symbol_results if r.status == "FAILED")
            symbols_skipped = sum(1 for r in cycle_result.symbol_results if r.status == "SKIPPED")
            final_status = STATUS_COMPLETED if cycle_result.status == "COMPLETED" else (
                STATUS_PARTIALLY_COMPLETE if cycle_result.status == "PARTIALLY_COMPLETE" else STATUS_FAILED
            )
            cycle_id = cycle_result.cycle_id
        else:
            symbols_completed = 0
            symbols_failed = len(bounded_symbols)
            symbols_skipped = 0
            final_status = STATUS_FAILED
            cycle_id = None

        update_scheduler_run(
            conn, scheduler_run_id,
            {
                "actual_finish_at": finish_time.isoformat(), "cycle_id": cycle_id, "status": final_status,
                "symbols_attempted": len(bounded_symbols), "symbols_completed": symbols_completed,
                "symbols_skipped": symbols_skipped, "provider_failures": symbols_failed,
                "research_failures": symbols_failed,
                "budget_consumed_usd": str(reservation.reserved_estimated_cost_usd - updated_reservation["remaining_cost_usd"]),
                "failure_reason": failure_reason,
            },
        )

        # --- Step 10 (docs/milestone-7.md Step 18 items 10-11): evaluate
        # operational health and emit alerts for a cycle that actually ran
        # (COMPLETED/PARTIALLY_COMPLETE/FAILED — never for the lightweight
        # no-op paths above, which already returned before reaching here).
        health_inputs = _build_health_inputs_from_cycle_result(
            conn, cycle_result, symbols_attempted=len(bounded_symbols),
            cycle_duration_seconds=(finish_time - start_time).total_seconds(),
        )
        health_config = health_mod.HealthPolicyConfig.from_shadow_config(shadow_config)
        health_result = health_mod.evaluate_cycle_health(health_inputs, health_config)
        health_mod.apply_health_result(conn, health_result, health_config, clock)
        _save_health_summary(
            conn, scheduler_run_id=scheduler_run_id, intended_schedule_id=intended_schedule_id,
            health_result=health_result, inputs=health_inputs, clock=clock,
        )

        if final_status == STATUS_FAILED:
            _raise(
                conn, severity=alerts_mod.SEVERITY_ERROR, alert_type=alerts_mod.ALERT_TYPE_CYCLE_FAILED,
                message=f"scheduled shadow cycle {scheduler_run_id} failed: {failure_reason}",
                context={
                    "scheduler_run_id": scheduler_run_id, "intended_schedule_id": intended_schedule_id,
                    "failure_reason": failure_reason or "unknown",
                },
                clock=clock,
            )
        elif final_status == STATUS_PARTIALLY_COMPLETE:
            _raise(
                conn, severity=alerts_mod.SEVERITY_WARNING, alert_type=alerts_mod.ALERT_TYPE_CYCLE_PARTIALLY_COMPLETE,
                message=(
                    f"scheduled shadow cycle {scheduler_run_id} partially completed: "
                    f"{symbols_completed}/{len(bounded_symbols)} symbols completed"
                ),
                context={
                    "scheduler_run_id": scheduler_run_id, "intended_schedule_id": intended_schedule_id,
                    "symbols_completed": symbols_completed, "symbols_attempted": len(bounded_symbols),
                },
                clock=clock,
            )

        return ShadowCycleRunResult(
            status=final_status, scheduler_run_id=scheduler_run_id, intended_schedule_id=intended_schedule_id,
            intended_schedule_time=intended_schedule_time, cycle_id=cycle_id, symbols_attempted=len(bounded_symbols),
            symbols_completed=symbols_completed, symbols_skipped=symbols_skipped,
            budget_reservation_id=reservation.reservation_id, budget_reserved_usd=str(reservation.reserved_estimated_cost_usd),
            budget_consumed_usd=str(reservation.reserved_estimated_cost_usd - updated_reservation["remaining_cost_usd"]),
            failure_reason=failure_reason, lease_owner=lease_owner,
            reason=f"cycle {final_status.lower()}",
        )
    finally:
        # --- Step 10: release the lease — always. -----------------------------
        current = lease_mod.current_lease(conn, lease_key)
        if current is not None and current["status"] == lease_mod.LEASE_STATUS_HELD and current["owner"] == lease_owner:
            lease_mod.release(conn, lease_key, lease_owner, clock)


def _save_failed_run(
    conn: sqlite3.Connection, *, scheduler_run_id: str, intended_schedule_id: str, intended_schedule_time: datetime,
    configuration_hash: str, mode: str, lease_handle: lease_mod.LeaseHandle, status: str, failure_reason: str,
    deployment_source: str, clock: Clock,
) -> None:
    now = clock()
    save_scheduler_run(
        conn,
        {
            "scheduler_run_id": scheduler_run_id, "intended_schedule_id": intended_schedule_id,
            "scheduled_time": intended_schedule_time.isoformat(), "actual_start_at": now.isoformat(),
            "actual_finish_at": now.isoformat(), "cycle_id": None, "configuration_hash": configuration_hash,
            "mode": mode, "lease_owner": lease_handle.owner, "lease_expires_at": lease_handle.expires_at.isoformat(),
            "status": status, "pause_state": None, "budget_reservation_id": None, "budget_reserved_usd": None,
            "budget_consumed_usd": None, "symbols_attempted": 0, "symbols_completed": 0, "symbols_skipped": 0,
            "provider_failures": 0, "research_failures": 0, "paper_submissions": 0, "alert_count": 0,
            "failure_reason": failure_reason, "operator_action": None, "deployment_source": deployment_source,
            "created_at": now.isoformat(),
        },
    )
