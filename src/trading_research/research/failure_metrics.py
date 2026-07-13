"""Deterministic research-failure metrics (docs/milestone-6.1.md Step 17).

Pure functions over already-persisted attempt rows and `ResearchValidationFailure`
records — no I/O here (the CLI/`research-usage` integration reads the rows and passes
them in, mirroring `research/usage.py`'s own "pricing is data, not code" separation).
Every metric reports an explicit status so a caller never mistakes an empty dataset for a
real zero:

* `OK` — the metric was computed from at least one relevant sample.
* `INSUFFICIENT_DATA` — no attempts/failures existed to compute this metric from.
* `UNDEFINED` — the metric's denominator is structurally zero even though data exists
  (e.g. a rate whose denominator category never occurred).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .failure_taxonomy import (
    CODE_MANAGER_NOT_INVOKED,
    CODE_NUMERIC_VALUE_MISMATCH,
    CODE_OUTPUT_TRUNCATED,
    CODE_UNKNOWN_EVIDENCE_ID,
    CODE_UNSUPPORTED_NUMERIC_CLAIM,
    STAGE_MANAGER_SKIPPED,
    STAGE_PROVIDER_REQUEST,
    STAGE_PROVIDER_RESPONSE,
    STAGE_REQUIRED_ROLE_FAILED,
    STAGE_RETRY_EXHAUSTED,
    STAGE_STRUCTURED_SCHEMA,
    ResearchValidationFailure,
)

METRIC_STATUS_OK = "OK"
METRIC_STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
METRIC_STATUS_UNDEFINED = "UNDEFINED"


@dataclass(frozen=True)
class MetricValue:
    status: str
    value: float | int | None = None

    def to_dict(self) -> dict:
        return {"status": self.status, "value": self.value}


def _rate(numerator: int, denominator: int) -> MetricValue:
    if denominator == 0:
        return MetricValue(METRIC_STATUS_UNDEFINED)
    return MetricValue(METRIC_STATUS_OK, numerator / denominator)


def _average(values: Sequence[float]) -> MetricValue:
    if not values:
        return MetricValue(METRIC_STATUS_INSUFFICIENT_DATA)
    return MetricValue(METRIC_STATUS_OK, sum(values) / len(values))


def _count_by(items: Sequence[Any], key: "Any") -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        k = key(item)
        counts[k] = counts.get(k, 0) + 1
    return counts


def compute_research_failure_metrics(
    *,
    attempt_rows: Sequence[Mapping[str, Any]],
    failures: Sequence[ResearchValidationFailure],
) -> dict:
    """`attempt_rows` — one dict per persisted `research_attempts` row with at least
    `research_run_id`, `role`, `success`, `input_tokens`, `output_tokens`, `latency_ms`.
    `failures` — every persisted `ResearchValidationFailure` in scope (typically all of
    them, or a bounded recent window supplied by the caller)."""
    total_failures = len(failures)
    total_attempts = len(attempt_rows)

    failures_by_role = _count_by(failures, lambda f: f.role)
    failures_by_stage = _count_by(failures, lambda f: f.stage)
    failures_by_code = _count_by(failures, lambda f: f.code)

    role_groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in attempt_rows:
        role_groups.setdefault((row["research_run_id"], row["role"]), []).append(row)

    completed_group_attempt_counts = [
        len(rows) for rows in role_groups.values() if any(bool(r["success"]) for r in rows)
    ]
    retryable_groups = [rows for rows in role_groups.values() if len(rows) > 1]
    retry_succeeded = sum(1 for rows in retryable_groups if any(bool(r["success"]) for r in rows))

    retry_exhausted_failures = [f for f in failures if f.stage == STAGE_RETRY_EXHAUSTED]
    required_role_failed_failures = [f for f in failures if f.stage == STAGE_REQUIRED_ROLE_FAILED]
    manager_skipped_failures = [f for f in failures if f.stage == STAGE_MANAGER_SKIPPED]
    distinct_runs = {row["research_run_id"] for row in attempt_rows} | {f.research_run_id for f in failures}

    failed_attempts = [row for row in attempt_rows if not bool(row["success"])]
    failed_input_tokens = [row["input_tokens"] for row in failed_attempts if row.get("input_tokens") is not None]
    failed_output_tokens = [row["output_tokens"] for row in failed_attempts if row.get("output_tokens") is not None]
    failed_latency = [row["latency_ms"] for row in failed_attempts if row.get("latency_ms") is not None]

    exhausted_role_keys = {(f.research_run_id, f.role) for f in retry_exhausted_failures}
    exhausted_retry_tokens = sum(
        (row.get("input_tokens") or 0) + (row.get("output_tokens") or 0)
        for row in attempt_rows
        if (row["research_run_id"], row["role"]) in exhausted_role_keys
    )

    return {
        "total_attempts": total_attempts,
        "total_failures": total_failures,
        "failures_by_role": failures_by_role,
        "failures_by_stage": failures_by_stage,
        "failures_by_code": failures_by_code,
        "attempts_per_completed_role": _average(completed_group_attempt_counts).to_dict(),
        "retry_success_rate": (
            _rate(retry_succeeded, len(retryable_groups)).to_dict() if retryable_groups
            else MetricValue(METRIC_STATUS_INSUFFICIENT_DATA).to_dict()
        ),
        # Denominator is the population of (research_run_id, role) attempt groups that
        # actually happened — well-defined only when attempt rows were supplied alongside
        # the failures being rated against them.
        "retry_exhaustion_rate": (
            _rate(len(retry_exhausted_failures), len(role_groups)).to_dict() if role_groups
            else MetricValue(METRIC_STATUS_INSUFFICIENT_DATA).to_dict()
        ),
        "required_role_failure_rate": (
            _rate(len(required_role_failed_failures), len(role_groups)).to_dict() if role_groups
            else MetricValue(METRIC_STATUS_INSUFFICIENT_DATA).to_dict()
        ),
        "manager_skip_rate": (
            _rate(len(manager_skipped_failures), len(distinct_runs)).to_dict() if distinct_runs
            else MetricValue(METRIC_STATUS_INSUFFICIENT_DATA).to_dict()
        ),
        "unknown_evidence_id_rate": _rate(failures_by_code.get(CODE_UNKNOWN_EVIDENCE_ID, 0), total_failures).to_dict()
        if total_failures else MetricValue(METRIC_STATUS_INSUFFICIENT_DATA).to_dict(),
        "unsupported_numeric_claim_rate": _rate(
            failures_by_code.get(CODE_UNSUPPORTED_NUMERIC_CLAIM, 0) + failures_by_code.get(CODE_NUMERIC_VALUE_MISMATCH, 0),
            total_failures,
        ).to_dict() if total_failures else MetricValue(METRIC_STATUS_INSUFFICIENT_DATA).to_dict(),
        "schema_failure_rate": _rate(failures_by_stage.get(STAGE_STRUCTURED_SCHEMA, 0), total_failures).to_dict()
        if total_failures else MetricValue(METRIC_STATUS_INSUFFICIENT_DATA).to_dict(),
        "output_truncation_rate": _rate(failures_by_code.get(CODE_OUTPUT_TRUNCATED, 0), total_failures).to_dict()
        if total_failures else MetricValue(METRIC_STATUS_INSUFFICIENT_DATA).to_dict(),
        "provider_error_rate": _rate(
            failures_by_stage.get(STAGE_PROVIDER_REQUEST, 0) + failures_by_stage.get(STAGE_PROVIDER_RESPONSE, 0),
            total_failures,
        ).to_dict() if total_failures else MetricValue(METRIC_STATUS_INSUFFICIENT_DATA).to_dict(),
        "average_failed_attempt_input_tokens": _average(failed_input_tokens).to_dict(),
        "average_failed_attempt_output_tokens": _average(failed_output_tokens).to_dict(),
        "average_failed_attempt_latency_ms": _average(failed_latency).to_dict(),
        "tokens_spent_on_exhausted_retries": (
            {"status": METRIC_STATUS_OK, "value": exhausted_retry_tokens} if exhausted_role_keys
            else {"status": METRIC_STATUS_INSUFFICIENT_DATA, "value": None}
        ),
    }
