"""Persistence for the Milestone 5 research layer: evidence snapshots,
research runs/attempts/role-reports/decisions, overlay decisions, and
experiment assignments. Operates on `research_schema.py`'s tables, mirroring
`trading_repositories.py`'s conventions (idempotent inserts, full payload
JSON alongside a few flat/queryable columns).

`SQLiteResearchRepository` satisfies `research.orchestration.ResearchRepository`
structurally (duck typing) — `research/` never imports `storage/`.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal

from ..research.cycle_telemetry import ResearchCycleTelemetry, STATUS_COMPLETE, STATUS_PARTIAL, STATUS_UNAVAILABLE
from ..research.evidence import snapshot_from_row, snapshot_to_row
from ..research.failure_taxonomy import (
    CODE_BUDGET_EXHAUSTED,
    CODE_MISSING_REQUIRED_ROLE,
    CODE_OUTPUT_TRUNCATED,
    CODE_PROVIDER_CLIENT_ERROR,
    CODE_PROVIDER_RATE_LIMITED,
    CODE_PROVIDER_SERVER_ERROR,
    CODE_PROVIDER_TIMEOUT,
    CODE_PROVIDER_UNAVAILABLE,
    CODE_RETRY_EXHAUSTED,
    CODE_UNSUPPORTED_MATERIAL_CLAIM,
    CODE_UNSUPPORTED_NUMERIC_CLAIM,
    ResearchValidationFailure,
)
from ..research.models import (
    EvidenceSnapshot,
    ExperimentAssignment,
    ResearchClaim,
    ResearchDecision,
    ResearchOverlayDecision,
    RoleResearchReport,
)
from ..research.orchestration import ResearchAttemptRecord

_PROVIDER_FAILURE_CODES = frozenset(
    {CODE_PROVIDER_TIMEOUT, CODE_PROVIDER_RATE_LIMITED, CODE_PROVIDER_UNAVAILABLE, CODE_PROVIDER_CLIENT_ERROR, CODE_PROVIDER_SERVER_ERROR}
)
_UNSUPPORTED_CLAIM_CODES = frozenset({CODE_UNSUPPORTED_NUMERIC_CLAIM, CODE_UNSUPPORTED_MATERIAL_CLAIM})


def save_evidence_snapshot(conn: sqlite3.Connection, snapshot: EvidenceSnapshot) -> bool:
    """Idempotent: returns False (no-op) when this exact snapshot_id already
    exists — snapshots are immutable, so rebuilding identical content is
    never a conflict."""
    existing = conn.execute(
        "SELECT 1 FROM research_evidence_snapshots WHERE snapshot_id = ?", (snapshot.snapshot_id,)
    ).fetchone()
    if existing is not None:
        return False
    row = snapshot_to_row(snapshot)
    conn.execute(
        "INSERT INTO research_evidence_snapshots "
        "(snapshot_id, symbol, as_of, created_at, source_records_json, evidence_items_json, "
        "deterministic_factors_json, sentiment_metrics_json, portfolio_context_json, "
        "missing_data_reasons_json, conflict_reasons_json, point_in_time_safe, config_hash, git_sha) "
        "VALUES (:snapshot_id, :symbol, :as_of, :created_at, :source_records_json, :evidence_items_json, "
        ":deterministic_factors_json, :sentiment_metrics_json, :portfolio_context_json, "
        ":missing_data_reasons_json, :conflict_reasons_json, :point_in_time_safe, :config_hash, :git_sha)",
        row,
    )
    conn.commit()
    return True


def load_evidence_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> EvidenceSnapshot | None:
    row = conn.execute("SELECT * FROM research_evidence_snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
    if row is None:
        return None
    return snapshot_from_row(dict(row))


def _claim_to_dict(claim: ResearchClaim) -> dict:
    return {
        "claim_id": claim.claim_id, "claim_type": claim.claim_type, "statement": claim.statement,
        "evidence_ids": list(claim.evidence_ids),
        "numeric_value": str(claim.numeric_value) if claim.numeric_value is not None else None,
        "unit": claim.unit, "importance": claim.importance,
    }


def _claim_from_dict(data: dict) -> ResearchClaim:
    return ResearchClaim(
        claim_id=data["claim_id"], claim_type=data["claim_type"], statement=data["statement"],
        evidence_ids=tuple(data["evidence_ids"]),
        numeric_value=Decimal(data["numeric_value"]) if data.get("numeric_value") is not None else None,
        unit=data.get("unit"), importance=data["importance"],
    )


def _report_to_payload(report: RoleResearchReport) -> dict:
    return {
        "report_id": report.report_id, "research_run_id": report.research_run_id, "role": report.role,
        "symbol": report.symbol, "snapshot_id": report.snapshot_id, "stance": report.stance,
        "summary": report.summary, "claims": [_claim_to_dict(c) for c in report.claims],
        "catalysts": list(report.catalysts), "risks": list(report.risks),
        "uncertainties": list(report.uncertainties), "missing_data_reasons": list(report.missing_data_reasons),
        "model_name": report.model_name, "prompt_version": report.prompt_version,
    }


def _report_from_payload(payload: dict) -> RoleResearchReport:
    return RoleResearchReport(
        report_id=payload["report_id"], research_run_id=payload["research_run_id"], role=payload["role"],
        symbol=payload["symbol"], snapshot_id=payload["snapshot_id"], stance=payload["stance"],
        summary=payload["summary"], claims=tuple(_claim_from_dict(c) for c in payload["claims"]),
        catalysts=tuple(payload["catalysts"]), risks=tuple(payload["risks"]),
        uncertainties=tuple(payload["uncertainties"]), missing_data_reasons=tuple(payload["missing_data_reasons"]),
        model_name=payload["model_name"], prompt_version=payload["prompt_version"],
    )


def _decision_to_payload(decision: ResearchDecision) -> dict:
    return {
        "decision_id": decision.decision_id, "research_run_id": decision.research_run_id, "symbol": decision.symbol,
        "snapshot_id": decision.snapshot_id, "rating": decision.rating, "confidence": str(decision.confidence),
        "thesis": decision.thesis, "bull_case": decision.bull_case, "bear_case": decision.bear_case,
        "catalysts": list(decision.catalysts), "risks": list(decision.risks),
        "invalidation_conditions": list(decision.invalidation_conditions),
        "claims": [_claim_to_dict(c) for c in decision.claims], "evidence_ids": list(decision.evidence_ids),
        "missing_data_reasons": list(decision.missing_data_reasons), "model_name": decision.model_name,
        "prompt_version": decision.prompt_version,
    }


def _decision_from_payload(payload: dict) -> ResearchDecision:
    return ResearchDecision(
        decision_id=payload["decision_id"], research_run_id=payload["research_run_id"], symbol=payload["symbol"],
        snapshot_id=payload["snapshot_id"], rating=payload["rating"], confidence=Decimal(payload["confidence"]),
        thesis=payload["thesis"], bull_case=payload["bull_case"], bear_case=payload["bear_case"],
        catalysts=tuple(payload["catalysts"]), risks=tuple(payload["risks"]),
        invalidation_conditions=tuple(payload["invalidation_conditions"]),
        claims=tuple(_claim_from_dict(c) for c in payload["claims"]), evidence_ids=tuple(payload["evidence_ids"]),
        missing_data_reasons=tuple(payload["missing_data_reasons"]), model_name=payload["model_name"],
        prompt_version=payload["prompt_version"],
    )


class SQLiteResearchRepository:
    """Concrete storage backing `research.orchestration.ResearchRepository`."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get_run_status(self, research_run_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT status FROM research_committee_runs WHERE research_run_id = ?", (research_run_id,)
        ).fetchone()
        return row["status"] if row is not None else None

    def get_decision_for_run(self, research_run_id: str) -> ResearchDecision | None:
        row = self._conn.execute(
            "SELECT payload_json FROM research_decisions WHERE research_run_id = ?", (research_run_id,)
        ).fetchone()
        if row is None:
            return None
        return _decision_from_payload(json.loads(row["payload_json"]))

    def get_role_reports_for_run(self, research_run_id: str) -> tuple[RoleResearchReport, ...]:
        rows = self._conn.execute(
            "SELECT payload_json FROM research_role_reports WHERE research_run_id = ? ORDER BY role",
            (research_run_id,),
        ).fetchall()
        return tuple(_report_from_payload(json.loads(r["payload_json"])) for r in rows)

    def save_run_started(
        self, research_run_id: str, snapshot_id: str, provider: str, model_name: str, roles: tuple[str, ...],
        run_mode: str, config_hash: str, created_at: datetime,
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO research_committee_runs "
            "(research_run_id, snapshot_id, provider, model_name, roles_json, run_mode, status, config_hash, created_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?, NULL)",
            (research_run_id, snapshot_id, provider, model_name, json.dumps(list(roles)), run_mode, config_hash, created_at.isoformat()),
        )
        self._conn.commit()

    def save_attempt(self, attempt: ResearchAttemptRecord) -> None:
        usage = attempt.usage
        self._conn.execute(
            "INSERT INTO research_attempts "
            "(attempt_id, research_run_id, role, attempt_number, prompt_name, prompt_version, prompt_hash, "
            "system_prompt_hash, schema_version, provider, model_name, success, failure_reason, raw_response_json, "
            "validated_payload_json, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, latency_ms, "
            "provider_request_id, retry_count, pricing_version, estimated_cost, cost_status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attempt.attempt_id, attempt.research_run_id, attempt.role, attempt.attempt_number,
                attempt.prompt_name, attempt.prompt_version, attempt.prompt_hash, attempt.system_prompt_hash,
                attempt.schema_version, attempt.provider, attempt.model_name, int(attempt.success),
                attempt.failure_reason,
                json.dumps(attempt.raw_response_json) if attempt.raw_response_json is not None else None,
                json.dumps(attempt.validated_payload_json) if attempt.validated_payload_json is not None else None,
                usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens,
                usage.latency_ms, usage.provider_request_id, usage.retry_count, usage.pricing_version,
                str(usage.estimated_cost) if usage.estimated_cost is not None else None, usage.cost_status,
                attempt.created_at.isoformat(),
            ),
        )
        self._conn.commit()

    def save_attempt_failure(self, failure: ResearchValidationFailure) -> None:
        """Idempotent on `failure_id` (deterministic — see
        `research/failure_taxonomy.py::new_failure`): a retried write of the identical
        failure content is a no-op, never a duplicate row (Step 6: "idempotent insertion")."""
        self._conn.execute(
            "INSERT OR IGNORE INTO research_attempt_failures "
            "(failure_id, attempt_id, research_run_id, role, attempt_number, stage, code, message, "
            "field_path, claim_id, evidence_ids_json, retryable, model_name, prompt_version, "
            "schema_version, metadata_json, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                failure.failure_id, failure.attempt_id, failure.research_run_id, failure.role,
                failure.attempt_number, failure.stage, failure.code, failure.message, failure.field_path,
                failure.claim_id, json.dumps(list(failure.evidence_ids)), int(failure.retryable),
                failure.model_name, failure.prompt_version, failure.schema_version,
                json.dumps(dict(failure.metadata)), failure.occurred_at.isoformat(),
            ),
        )
        self._conn.commit()

    def save_attempt_failures(self, failures: "tuple[ResearchValidationFailure, ...]") -> None:
        for failure in failures:
            self.save_attempt_failure(failure)

    def list_run_failures(self, research_run_id: str) -> "tuple[ResearchValidationFailure, ...]":
        return list_run_failures(self._conn, research_run_id)

    def save_role_report(self, report: RoleResearchReport, attempt_id: str, created_at: datetime) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO research_role_reports "
            "(report_id, research_run_id, role, symbol, snapshot_id, stance, payload_json, attempt_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report.report_id, report.research_run_id, report.role, report.symbol, report.snapshot_id,
                report.stance, json.dumps(_report_to_payload(report)), attempt_id, created_at.isoformat(),
            ),
        )
        self._conn.commit()

    def save_decision(self, decision: ResearchDecision, created_at: datetime) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO research_decisions "
            "(decision_id, research_run_id, symbol, snapshot_id, rating, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                decision.decision_id, decision.research_run_id, decision.symbol, decision.snapshot_id,
                decision.rating, json.dumps(_decision_to_payload(decision)), created_at.isoformat(),
            ),
        )
        self._conn.commit()

    def mark_run_finished(self, research_run_id: str, status: str, completed_at: datetime) -> None:
        self._conn.execute(
            "UPDATE research_committee_runs SET status = ?, completed_at = ? WHERE research_run_id = ?",
            (status, completed_at.isoformat(), research_run_id),
        )
        self._conn.commit()


def save_overlay_decision(conn: sqlite3.Connection, overlay: ResearchOverlayDecision, created_at: datetime) -> bool:
    existing = conn.execute(
        "SELECT 1 FROM research_overlay_decisions WHERE overlay_id = ?", (overlay.overlay_id,)
    ).fetchone()
    if existing is not None:
        return False
    payload = {
        "overlay_id": overlay.overlay_id, "research_decision_id": overlay.research_decision_id,
        "baseline_score": str(overlay.baseline_score) if overlay.baseline_score is not None else None,
        "action": overlay.action, "reasons": list(overlay.reasons), "critical_risks": list(overlay.critical_risks),
        "policy_version": overlay.policy_version,
    }
    conn.execute(
        "INSERT INTO research_overlay_decisions "
        "(overlay_id, research_decision_id, baseline_score, action, policy_version, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            overlay.overlay_id, overlay.research_decision_id,
            str(overlay.baseline_score) if overlay.baseline_score is not None else None, overlay.action,
            overlay.policy_version, json.dumps(payload), created_at.isoformat(),
        ),
    )
    conn.commit()
    return True


def save_experiment_assignment(conn: sqlite3.Connection, assignment: ExperimentAssignment, created_at: datetime) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO research_experiment_assignments "
        "(experiment_id, candidate_run_id, symbol, as_of, arm, baseline_recommendation_id, "
        "enhanced_recommendation_id, assignment_policy_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            assignment.experiment_id, assignment.candidate_run_id, assignment.symbol, assignment.as_of.isoformat(),
            assignment.arm, assignment.baseline_recommendation_id, assignment.enhanced_recommendation_id,
            assignment.assignment_policy_version, created_at.isoformat(),
        ),
    )
    conn.commit()


def list_experiment_assignments(conn: sqlite3.Connection, experiment_id: str) -> tuple[ExperimentAssignment, ...]:
    rows = conn.execute(
        "SELECT * FROM research_experiment_assignments WHERE experiment_id = ? ORDER BY symbol, arm",
        (experiment_id,),
    ).fetchall()
    return tuple(
        ExperimentAssignment(
            experiment_id=r["experiment_id"], candidate_run_id=r["candidate_run_id"], symbol=r["symbol"],
            as_of=datetime.fromisoformat(r["as_of"]), arm=r["arm"],
            baseline_recommendation_id=r["baseline_recommendation_id"],
            enhanced_recommendation_id=r["enhanced_recommendation_id"],
            assignment_policy_version=r["assignment_policy_version"],
        )
        for r in rows
    )


def list_attempt_rows_for_metrics(conn: sqlite3.Connection) -> list[dict]:
    """Feeds `research/failure_metrics.py::compute_research_failure_metrics` — unlike
    `list_attempt_usage_rows`, this includes `research_run_id` so attempts can be grouped
    per (run, role) to compute retry-success/retry-exhaustion rates."""
    rows = conn.execute(
        "SELECT research_run_id, role, success, input_tokens, output_tokens, latency_ms FROM research_attempts"
    ).fetchall()
    return [dict(r) for r in rows]


def list_all_attempt_failures(conn: sqlite3.Connection) -> tuple[ResearchValidationFailure, ...]:
    """Every persisted failure across every run — feeds
    `research/failure_metrics.py::compute_research_failure_metrics`."""
    rows = conn.execute("SELECT * FROM research_attempt_failures ORDER BY occurred_at").fetchall()
    return tuple(_failure_from_row(r) for r in rows)


def list_attempt_usage_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT role, provider, model_name, input_tokens, output_tokens, latency_ms, retry_count, success, "
        "estimated_cost, cost_status FROM research_attempts"
    ).fetchall()
    return [dict(r) for r in rows]


def list_research_committee_runs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM research_committee_runs ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def _failure_from_row(row: sqlite3.Row) -> ResearchValidationFailure:
    return ResearchValidationFailure(
        failure_id=row["failure_id"], research_run_id=row["research_run_id"], attempt_id=row["attempt_id"],
        role=row["role"], attempt_number=row["attempt_number"], stage=row["stage"], code=row["code"],
        message=row["message"], field_path=row["field_path"], claim_id=row["claim_id"],
        evidence_ids=tuple(json.loads(row["evidence_ids_json"])), retryable=bool(row["retryable"]),
        model_name=row["model_name"], prompt_version=row["prompt_version"], schema_version=row["schema_version"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]), metadata=json.loads(row["metadata_json"]),
    )


def list_attempt_failures(conn: sqlite3.Connection, attempt_id: str) -> tuple[ResearchValidationFailure, ...]:
    rows = conn.execute(
        "SELECT * FROM research_attempt_failures WHERE attempt_id = ? ORDER BY occurred_at", (attempt_id,)
    ).fetchall()
    return tuple(_failure_from_row(r) for r in rows)


def list_role_failures(conn: sqlite3.Connection, research_run_id: str, role: str) -> tuple[ResearchValidationFailure, ...]:
    rows = conn.execute(
        "SELECT * FROM research_attempt_failures WHERE research_run_id = ? AND role = ? ORDER BY occurred_at",
        (research_run_id, role),
    ).fetchall()
    return tuple(_failure_from_row(r) for r in rows)


def list_run_failures(
    conn: sqlite3.Connection,
    research_run_id: str,
    *,
    role: str | None = None,
    attempt_number: int | None = None,
    stage: str | None = None,
    code: str | None = None,
    retryable: bool | None = None,
) -> tuple[ResearchValidationFailure, ...]:
    """Queryable by every dimension Step 6/15 require: run (mandatory), and optionally
    role, attempt_number, stage, code, retryability."""
    query = "SELECT * FROM research_attempt_failures WHERE research_run_id = ?"
    params: list = [research_run_id]
    if role is not None:
        query += " AND role = ?"
        params.append(role)
    if attempt_number is not None:
        query += " AND attempt_number = ?"
        params.append(attempt_number)
    if stage is not None:
        query += " AND stage = ?"
        params.append(stage)
    if code is not None:
        query += " AND code = ?"
        params.append(code)
    if retryable is not None:
        query += " AND retryable = ?"
        params.append(int(retryable))
    query += " ORDER BY occurred_at"
    rows = conn.execute(query, params).fetchall()
    return tuple(_failure_from_row(r) for r in rows)


def summarize_run_failures(conn: sqlite3.Connection, research_run_id: str) -> dict:
    """Counts by stage and by code for one run — used by the `research-failures` CLI
    command (Step 15) and by `research/failure_metrics.py` (Step 17)."""
    failures = list_run_failures(conn, research_run_id)
    by_stage: dict[str, int] = {}
    by_code: dict[str, int] = {}
    for f in failures:
        by_stage[f.stage] = by_stage.get(f.stage, 0) + 1
        by_code[f.code] = by_code.get(f.code, 0) + 1
    return {
        "research_run_id": research_run_id, "total_failures": len(failures),
        "counts_by_stage": by_stage, "counts_by_code": by_code,
    }


def compute_cycle_telemetry(conn: sqlite3.Connection, research_run_ids: tuple[str, ...]) -> ResearchCycleTelemetry:
    """Derives `ResearchCycleTelemetry` (docs/milestone-7.1.md Step 16) from
    real, already-persisted `research_attempts`/`research_attempt_failures`
    rows for exactly the supplied `research_run_id`s — the authoritative
    source, never a duplicated in-memory counter kept alongside the
    orchestrator's own bookkeeping.

    `status=UNAVAILABLE` when `research_run_ids` is empty (no research ran
    this cycle — nothing was inspected, not "zero and healthy").
    `status=PARTIAL` when any required-role-failure/retry-exhaustion/
    budget-skip occurred. `status=COMPLETE` otherwise. Token/latency/cost
    fields stay `None` when genuinely no attempt reported them (never a
    fabricated 0) — `missing_usage_record_count` names how many attempts
    that affects.
    """
    if not research_run_ids:
        return ResearchCycleTelemetry(
            status=STATUS_UNAVAILABLE, research_run_ids=(), attempt_count=0, successful_attempt_count=0,
            failed_attempt_count=0, retry_count=0, retry_exhaustion_count=0, required_role_failure_count=0,
            provider_failure_count=0, unsupported_claim_count=0, output_truncation_count=0,
            budget_skipped_attempt_count=0, input_tokens=None, output_tokens=None, latency_ms=None,
            priced_usage_cost_usd=None, pricing_status="NO_DATA", missing_usage_record_count=0,
        )

    placeholders = ",".join("?" for _ in research_run_ids)
    attempt_rows = conn.execute(
        f"SELECT * FROM research_attempts WHERE research_run_id IN ({placeholders})", research_run_ids,
    ).fetchall()
    failures = tuple(
        f
        for run_id in research_run_ids
        for f in list_run_failures(conn, run_id)
    )

    attempt_count = len(attempt_rows)
    successful_attempt_count = sum(1 for r in attempt_rows if r["success"])
    failed_attempt_count = attempt_count - successful_attempt_count
    retry_count = sum(1 for r in attempt_rows if r["attempt_number"] > 1)
    retry_exhaustion_count = sum(1 for f in failures if f.code == CODE_RETRY_EXHAUSTED)
    required_role_failure_count = sum(1 for f in failures if f.code == CODE_MISSING_REQUIRED_ROLE)
    provider_failure_count = sum(1 for f in failures if f.code in _PROVIDER_FAILURE_CODES)
    unsupported_claim_count = sum(1 for f in failures if f.code in _UNSUPPORTED_CLAIM_CODES)
    output_truncation_count = sum(1 for f in failures if f.code == CODE_OUTPUT_TRUNCATED)
    budget_skipped_attempt_count = sum(1 for f in failures if f.code == CODE_BUDGET_EXHAUSTED)

    input_token_values = [r["input_tokens"] for r in attempt_rows if r["input_tokens"] is not None]
    output_token_values = [r["output_tokens"] for r in attempt_rows if r["output_tokens"] is not None]
    latency_values = [r["latency_ms"] for r in attempt_rows if r["latency_ms"] is not None]
    missing_usage_record_count = sum(1 for r in attempt_rows if r["input_tokens"] is None or r["output_tokens"] is None)

    cost_statuses = {r["cost_status"] for r in attempt_rows}
    priced_costs = [Decimal(r["estimated_cost"]) for r in attempt_rows if r["cost_status"] == "CALCULATED" and r["estimated_cost"] is not None]
    if not attempt_rows:
        pricing_status = "NO_DATA"
        priced_usage_cost_usd = None
    elif cost_statuses == {"NOT_APPLICABLE"}:
        pricing_status = "NOT_APPLICABLE"
        priced_usage_cost_usd = None
    elif "PRICING_NOT_CONFIGURED" in cost_statuses:
        pricing_status = "PRICING_NOT_CONFIGURED"
        priced_usage_cost_usd = sum(priced_costs) if priced_costs else None
    elif "USAGE_NOT_RETURNED" in cost_statuses:
        pricing_status = "USAGE_NOT_RETURNED"
        priced_usage_cost_usd = sum(priced_costs) if priced_costs else None
    elif cost_statuses == {"CALCULATED"}:
        pricing_status = "CALCULATED"
        priced_usage_cost_usd = sum(priced_costs)
    else:
        pricing_status = "MIXED"
        priced_usage_cost_usd = sum(priced_costs) if priced_costs else None

    if required_role_failure_count > 0 or retry_exhaustion_count > 0 or budget_skipped_attempt_count > 0:
        status = STATUS_PARTIAL
    else:
        status = STATUS_COMPLETE

    return ResearchCycleTelemetry(
        status=status, research_run_ids=tuple(research_run_ids), attempt_count=attempt_count,
        successful_attempt_count=successful_attempt_count, failed_attempt_count=failed_attempt_count,
        retry_count=retry_count, retry_exhaustion_count=retry_exhaustion_count,
        required_role_failure_count=required_role_failure_count, provider_failure_count=provider_failure_count,
        unsupported_claim_count=unsupported_claim_count, output_truncation_count=output_truncation_count,
        budget_skipped_attempt_count=budget_skipped_attempt_count,
        input_tokens=sum(input_token_values) if input_token_values else None,
        output_tokens=sum(output_token_values) if output_token_values else None,
        latency_ms=sum(latency_values) if latency_values else None,
        priced_usage_cost_usd=priced_usage_cost_usd, pricing_status=pricing_status,
        missing_usage_record_count=missing_usage_record_count,
    )
