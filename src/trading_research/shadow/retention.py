"""Deterministic data-retention planning (docs/milestone-7.md Step 26, ADR
0005 Decision 11).

Per ADR 0005 Decision 11 and this task's explicit instruction: no destructive
cleanup exists anywhere in this repository as of Milestone 7. `retention-plan`
prints a plan (what tier each table falls in, what would eventually be
affected) with NO action taken — it does not even count rows that would be
affected beyond a simple `SELECT COUNT(*)` for visibility. `retention-apply
--dry-run` prints a dry-run diff (row counts that WOULD be summarized/deleted
under the plan) — also strictly read-only. Calling `retention_apply` WITHOUT
`dry_run=True` raises `NotImplementedError` unconditionally; this is
intentional, not a bug to be "fixed" by implementing real deletion in a
later change without a dedicated, separately-reviewed task.

Retention-tier vocabulary (this module's own, versioned choice — the
milestone doc names the *requirement* for tiers but not their exact names):

    PERMANENT_AUDIT
        Never eligible for deletion or summarization by this module, ever.
        Append-only audit trails whose absence would break auditability
        itself (pause-state history, operator actions, alerts).

    RETAIN_N_DAYS
        Eligible for deletion after `retention_days` once real deletion is
        implemented (it is not, in this milestone) — operational/diagnostic
        data whose long-term value is low once evaluation has consumed it.

    RETAIN_N_DAYS_THEN_HASH_ONLY
        Eligible, after `retention_days`, to have bulk content replaced by
        its already-persisted content hash while the hash/provenance record
        itself is retained permanently — raw filing/article text whose
        licensing or storage-cost profile favors not keeping full content
        forever, while still being able to prove "we saw exactly this".

    RETAIN_INDEFINITELY_ACTIVE_EVALUATION
        No automatic eligibility at all in this milestone — data that active
        evaluation/replay/promotion logic may still need. A future task
        would need to prove nothing currently depends on a row before this
        tier's data could ever move to `RETAIN_N_DAYS`.

No tier here is wired to any actual `DELETE`/`UPDATE` statement. This module
only classifies, counts (read-only), and reports.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

POLICY_VERSION = "retention/v1"

TIER_PERMANENT_AUDIT = "PERMANENT_AUDIT"
TIER_RETAIN_N_DAYS = "RETAIN_N_DAYS"
TIER_RETAIN_N_DAYS_THEN_HASH_ONLY = "RETAIN_N_DAYS_THEN_HASH_ONLY"
TIER_RETAIN_INDEFINITELY_ACTIVE_EVALUATION = "RETAIN_INDEFINITELY_ACTIVE_EVALUATION"

ALL_TIERS = (
    TIER_PERMANENT_AUDIT,
    TIER_RETAIN_N_DAYS,
    TIER_RETAIN_N_DAYS_THEN_HASH_ONLY,
    TIER_RETAIN_INDEFINITELY_ACTIVE_EVALUATION,
)


class RetentionPolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class TableRetentionRule:
    table_name: str
    tier: str
    retention_days: int | None
    created_at_column: str | None
    rationale: str

    def __post_init__(self) -> None:
        if self.tier not in ALL_TIERS:
            raise RetentionPolicyError(f"tier {self.tier!r} is not one of {ALL_TIERS} — fails closed")
        if self.tier in (TIER_RETAIN_N_DAYS, TIER_RETAIN_N_DAYS_THEN_HASH_ONLY):
            if self.retention_days is None or self.retention_days <= 0:
                raise RetentionPolicyError(f"table {self.table_name!r}: tier {self.tier!r} requires retention_days > 0")
        if self.tier == TIER_PERMANENT_AUDIT and self.retention_days is not None:
            raise RetentionPolicyError(f"table {self.table_name!r}: PERMANENT_AUDIT must not carry retention_days")


# --- Retention plan: covers this milestone's own tables plus the named
# Milestone 1-6.1 tables from docs/milestone-7.md Step 26's classification
# list (immutable evidence, raw SEC filing documents, provider request
# metadata, account-linked normalized market data, research attempts,
# structured failures, scheduler runs, alerts, budget reservations, delivery
# logs). Every rule below is documented with a rationale, not a bare guess.

RETENTION_PLAN: tuple[TableRetentionRule, ...] = (
    # --- Append-only audit trails: never eligible, full stop. ---------------
    TableRetentionRule(
        "shadow_pause_state", TIER_PERMANENT_AUDIT, None, "created_at",
        "Append-only pause/kill-switch history — the audit trail proving when and why the system was ever paused or killed.",
    ),
    TableRetentionRule(
        "shadow_operator_actions", TIER_PERMANENT_AUDIT, None, "created_at",
        "Append-only record of every operator override (pause/resume/kill/force-clear-kill/force-release) — required audit trail.",
    ),
    TableRetentionRule(
        "shadow_alerts", TIER_PERMANENT_AUDIT, None, "created_at",
        "Operational-alert history — needed for incident review and readiness-report alert-delivery-health accounting indefinitely.",
    ),
    TableRetentionRule(
        "shadow_alert_deliveries", TIER_PERMANENT_AUDIT, None, "created_at",
        "Delivery-attempt log tied 1:1 to shadow_alerts — retained on the same permanent basis as its parent alert.",
    ),

    # --- Immutable evidence / provenance: retained for active evaluation, no
    # automatic eligibility introduced in this milestone. -------------------
    TableRetentionRule(
        "research_evidence_snapshots", TIER_RETAIN_INDEFINITELY_ACTIVE_EVALUATION, None, "created_at",
        "Immutable, content-hashed point-in-time evidence snapshots — replay/reproducibility validation (research/replay) depends on these existing indefinitely; no expiry policy is safe to assume without an explicit downstream audit.",
    ),
    TableRetentionRule(
        "corporate_status_evidence", TIER_RETAIN_INDEFINITELY_ACTIVE_EVALUATION, None, "created_at",
        "Immutable corporate-status evidence with full source provenance — same replay/reproducibility dependency as research_evidence_snapshots.",
    ),
    TableRetentionRule(
        "sec_filings", TIER_RETAIN_N_DAYS_THEN_HASH_ONLY, 730, "filed_at",
        "Raw SEC filing metadata/documents — SEC public filings may be retained per docs/milestone-7.md Step 6, but bulk content is a reasonable hash-only-after-N-days candidate once the underlying accession number and content hash remain queryable; the metadata itself (accession numbers, form types, dates) is not deleted.",
    ),

    # --- Provider request metadata / operational telemetry. -----------------
    TableRetentionRule(
        "evidence_provider_requests", TIER_RETAIN_N_DAYS, 365, "requested_at",
        "Provider request/response metadata (timing, cache status, success/failure) used by provider-health reporting — one year is ample for health-trend analysis; no raw payload is stored here (already excluded per the provider-persistence layer's own design).",
    ),
    TableRetentionRule(
        "evidence_provider_health_snapshots", TIER_RETAIN_N_DAYS, 365, "computed_at",
        "Periodic provider-health rollups — derived data, cheap to regenerate from evidence_provider_requests within the same retention window.",
    ),

    # --- Account-linked normalized market data. ------------------------------
    TableRetentionRule(
        "price_bars", TIER_RETAIN_INDEFINITELY_ACTIVE_EVALUATION, None, "as_of",
        "Normalized historical price bars — required for ongoing forward-performance evaluation (evaluation/evaluation_service.py) at arbitrary future horizons; no safe expiry without breaking in-flight or future evaluation windows.",
    ),

    # --- Research attempts / structured failures. -----------------------------
    TableRetentionRule(
        "research_attempts", TIER_RETAIN_N_DAYS, 730, "started_at",
        "Per-role Claude-attempt records (token/latency/cost usage) — retained two years for readiness/promotion trend analysis; not immutable evidence, so eligible for eventual pruning once implemented.",
    ),
    TableRetentionRule(
        "research_attempt_failures", TIER_RETAIN_N_DAYS, 730, "occurred_at",
        "Structured research-failure taxonomy records — same retention window as their parent research_attempts row.",
    ),
    TableRetentionRule(
        "research_failures", TIER_RETAIN_N_DAYS, 730, "occurred_at",
        "Structured research-run-level failure records — same retention window as research_attempt_failures.",
    ),

    # --- Scheduler runs / this milestone's own operational tables. ----------
    TableRetentionRule(
        "shadow_scheduler_runs", TIER_RETAIN_N_DAYS, 730, "created_at",
        "Per-invocation scheduler run records — readiness reporting (shadow/readiness.py) consumes recent history; two years is well beyond any realistic readiness lookback window while still allowing eventual pruning.",
    ),
    TableRetentionRule(
        "shadow_run_summaries", TIER_RETAIN_N_DAYS, 730, "created_at",
        "Per-run health/evaluation summary rows — same retention window as shadow_scheduler_runs, its 1:1 parent.",
    ),

    # --- Budget reservations. -------------------------------------------------
    TableRetentionRule(
        "shadow_budget_reservations", TIER_RETAIN_N_DAYS, 730, "created_at",
        "Budget reservation/settlement records — financial-control audit value diminishes after the evaluation window closes; two years matches the scheduler-run retention window for consistent joins.",
    ),
    TableRetentionRule(
        "shadow_budget_usage", TIER_RETAIN_N_DAYS, 730, "recorded_at",
        "Actual-usage records tied to shadow_budget_reservations — same retention window as its parent.",
    ),

    # --- Delivery logs (alert deliveries already listed as PERMANENT_AUDIT
    # above since they are 1:1 with shadow_alerts; no separate delivery-log
    # table exists elsewhere in this repository as of Milestone 7).
)


@dataclass(frozen=True)
class TablePlanEntry:
    rule: TableRetentionRule
    table_exists: bool
    current_row_count: int | None  # None when table_exists is False


@dataclass(frozen=True)
class RetentionPlanReport:
    policy_version: str
    as_of: datetime
    entries: tuple[TablePlanEntry, ...]

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise RetentionPolicyError("RetentionPlanReport.as_of must be timezone-aware")


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table_name,)
    ).fetchone()
    return row is not None


def _row_count(conn: sqlite3.Connection, table_name: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table_name}").fetchone()
    return int(row["n"] if isinstance(row, sqlite3.Row) else row[0])


def build_retention_plan(conn: sqlite3.Connection, as_of: datetime) -> RetentionPlanReport:
    """Read-only: `SELECT`s only (existence check + `COUNT(*)`), never a
    `DELETE`/`UPDATE`. Reports every table in `RETENTION_PLAN` whether or not
    it currently exists in this database (a fresh/partial database is a
    valid, honestly-reported state, not an error)."""
    entries = []
    for rule in RETENTION_PLAN:
        exists = _table_exists(conn, rule.table_name)
        count = _row_count(conn, rule.table_name) if exists else None
        entries.append(TablePlanEntry(rule=rule, table_exists=exists, current_row_count=count))
    return RetentionPlanReport(policy_version=POLICY_VERSION, as_of=as_of, entries=tuple(entries))


@dataclass(frozen=True)
class DryRunTableDiff:
    table_name: str
    tier: str
    table_exists: bool
    current_row_count: int | None
    eligible_row_count: int | None  # rows older than retention_days as of `as_of`; None when not time-boundable or table absent
    action_if_applied: str  # human-readable description; never executed


@dataclass(frozen=True)
class RetentionDryRunReport:
    policy_version: str
    as_of: datetime
    diffs: tuple[DryRunTableDiff, ...]

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise RetentionPolicyError("RetentionDryRunReport.as_of must be timezone-aware")


def _eligible_row_count(conn: sqlite3.Connection, rule: TableRetentionRule, as_of: datetime) -> int | None:
    if rule.tier not in (TIER_RETAIN_N_DAYS, TIER_RETAIN_N_DAYS_THEN_HASH_ONLY):
        return None
    if rule.created_at_column is None or rule.retention_days is None:
        return None
    cutoff = as_of.timestamp() - rule.retention_days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    try:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {rule.table_name} WHERE {rule.created_at_column} < ?", (cutoff_iso,)
        ).fetchone()
    except sqlite3.OperationalError:
        # Column name mismatch (schema drift) — fails closed to "unknown",
        # never a fabricated count.
        return None
    return int(row["n"] if isinstance(row, sqlite3.Row) else row[0])


def build_retention_dry_run(conn: sqlite3.Connection, as_of: datetime) -> RetentionDryRunReport:
    """Strictly read-only diff of what WOULD be affected if real deletion
    existed — still no `DELETE`/`UPDATE` anywhere in this function. Proves
    read-only-ness is testable: calling this twice back-to-back must never
    change any table's row count."""
    diffs = []
    for rule in RETENTION_PLAN:
        exists = _table_exists(conn, rule.table_name)
        count = _row_count(conn, rule.table_name) if exists else None
        eligible = _eligible_row_count(conn, rule, as_of) if exists else None
        if rule.tier == TIER_PERMANENT_AUDIT:
            action = "NEVER ELIGIBLE — permanent audit trail"
        elif rule.tier == TIER_RETAIN_INDEFINITELY_ACTIVE_EVALUATION:
            action = "NOT CURRENTLY ELIGIBLE — retained indefinitely pending future explicit review"
        elif rule.tier == TIER_RETAIN_N_DAYS_THEN_HASH_ONLY:
            action = f"WOULD hash-only rows older than {rule.retention_days} days (not implemented — dry-run only)"
        else:
            action = f"WOULD delete rows older than {rule.retention_days} days (not implemented — dry-run only)"
        diffs.append(
            DryRunTableDiff(
                table_name=rule.table_name, tier=rule.tier, table_exists=exists, current_row_count=count,
                eligible_row_count=eligible, action_if_applied=action,
            )
        )
    return RetentionDryRunReport(policy_version=POLICY_VERSION, as_of=as_of, diffs=tuple(diffs))


def apply_retention(conn: sqlite3.Connection, as_of: datetime, *, dry_run: bool) -> RetentionDryRunReport:
    """Calling this WITHOUT `dry_run=True` raises `NotImplementedError`
    unconditionally — intentional per ADR 0005 Decision 11 and this task's
    explicit instruction not to implement real deletion. `dry_run=True`
    delegates to `build_retention_dry_run` (still fully read-only)."""
    if not dry_run:
        raise NotImplementedError(
            "retention-apply without --dry-run is not implemented (ADR 0005 Decision 11): "
            "this repository does not perform destructive retention cleanup. "
            "Use `retention-apply --dry-run` to preview, or `retention-plan` to see the classification."
        )
    return build_retention_dry_run(conn, as_of)
