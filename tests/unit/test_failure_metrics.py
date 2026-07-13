"""Milestone 6.1 Step 17/19: unit tests for
`research/failure_metrics.py::compute_research_failure_metrics`."""
from __future__ import annotations

from datetime import datetime, timezone

from trading_research.research.failure_metrics import METRIC_STATUS_INSUFFICIENT_DATA, METRIC_STATUS_OK, compute_research_failure_metrics
from trading_research.research.failure_taxonomy import new_failure

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _failure(**overrides):
    kwargs = dict(
        research_run_id="run-1", attempt_id="run-1-bear-1", role="bear", attempt_number=1,
        stage="CLAIM_EVIDENCE_VALIDATION", code="NUMERIC_VALUE_MISMATCH", message="numeric mismatch",
        model_name="m", prompt_version="v1", schema_version="role-report.v1", occurred_at=NOW,
    )
    kwargs.update(overrides)
    return new_failure(**kwargs)


def test_empty_dataset_returns_insufficient_data_everywhere():
    metrics = compute_research_failure_metrics(attempt_rows=[], failures=())
    assert metrics["total_attempts"] == 0
    assert metrics["total_failures"] == 0
    for key in (
        "attempts_per_completed_role", "retry_success_rate", "retry_exhaustion_rate",
        "required_role_failure_rate", "manager_skip_rate", "unknown_evidence_id_rate",
        "unsupported_numeric_claim_rate", "schema_failure_rate", "output_truncation_rate",
        "provider_error_rate", "average_failed_attempt_input_tokens",
        "average_failed_attempt_output_tokens", "average_failed_attempt_latency_ms",
        "tokens_spent_on_exhausted_retries",
    ):
        assert metrics[key]["status"] == METRIC_STATUS_INSUFFICIENT_DATA, key


def test_failures_by_role_and_code_counted():
    failures = (
        _failure(role="bear", code="NUMERIC_VALUE_MISMATCH"),
        _failure(role="bear", code="NUMERIC_VALUE_MISMATCH", attempt_id="run-1-bear-2", attempt_number=2),
        _failure(role="manager", code="MANAGER_NOT_INVOKED", stage="MANAGER_SKIPPED", attempt_id="run-1-manager-skipped"),
    )
    metrics = compute_research_failure_metrics(attempt_rows=[], failures=failures)
    assert metrics["failures_by_role"]["bear"] == 2
    assert metrics["failures_by_role"]["manager"] == 1
    assert metrics["failures_by_code"]["NUMERIC_VALUE_MISMATCH"] == 2
    assert metrics["failures_by_stage"]["MANAGER_SKIPPED"] == 1


def test_retry_exhaustion_rate_and_manager_skip_rate():
    attempt_rows = [
        {"research_run_id": "run-1", "role": "bear", "success": False, "input_tokens": 100, "output_tokens": 50, "latency_ms": 10},
        {"research_run_id": "run-1", "role": "bear", "success": False, "input_tokens": 110, "output_tokens": 55, "latency_ms": 12},
        {"research_run_id": "run-1", "role": "fundamental", "success": True, "input_tokens": 90, "output_tokens": 40, "latency_ms": 8},
    ]
    failures = (
        _failure(stage="RETRY_EXHAUSTED", code="RETRY_EXHAUSTED", attempt_id="run-1-bear-2", attempt_number=2),
        _failure(stage="MANAGER_SKIPPED", code="MANAGER_NOT_INVOKED", role="manager", attempt_id="run-1-manager-skipped"),
    )
    metrics = compute_research_failure_metrics(attempt_rows=attempt_rows, failures=failures)
    # 2 (run,role) groups: (run-1,bear) and (run-1,fundamental); 1 of them has RETRY_EXHAUSTED.
    assert metrics["retry_exhaustion_rate"] == {"status": METRIC_STATUS_OK, "value": 0.5}
    assert metrics["manager_skip_rate"] == {"status": METRIC_STATUS_OK, "value": 1.0}


def test_unsupported_numeric_claim_rate_combines_both_codes():
    failures = (
        _failure(code="UNSUPPORTED_NUMERIC_CLAIM"),
        _failure(code="NUMERIC_VALUE_MISMATCH", attempt_id="run-1-bear-2"),
        _failure(code="UNKNOWN_EVIDENCE_ID", attempt_id="run-1-bear-3"),
    )
    metrics = compute_research_failure_metrics(attempt_rows=[], failures=failures)
    assert metrics["unsupported_numeric_claim_rate"] == {"status": METRIC_STATUS_OK, "value": 2 / 3}


def test_average_failed_attempt_tokens_and_latency():
    attempt_rows = [
        {"research_run_id": "run-1", "role": "bear", "success": False, "input_tokens": 100, "output_tokens": 50, "latency_ms": 10},
        {"research_run_id": "run-1", "role": "bear", "success": False, "input_tokens": 200, "output_tokens": 100, "latency_ms": 20},
        {"research_run_id": "run-1", "role": "bear", "success": True, "input_tokens": 300, "output_tokens": 150, "latency_ms": 30},
    ]
    metrics = compute_research_failure_metrics(attempt_rows=attempt_rows, failures=())
    assert metrics["average_failed_attempt_input_tokens"] == {"status": METRIC_STATUS_OK, "value": 150.0}
    assert metrics["average_failed_attempt_output_tokens"] == {"status": METRIC_STATUS_OK, "value": 75.0}
    assert metrics["average_failed_attempt_latency_ms"] == {"status": METRIC_STATUS_OK, "value": 15.0}


def test_tokens_spent_on_exhausted_retries():
    attempt_rows = [
        {"research_run_id": "run-1", "role": "bear", "success": False, "input_tokens": 100, "output_tokens": 50, "latency_ms": 10},
        {"research_run_id": "run-1", "role": "bear", "success": False, "input_tokens": 110, "output_tokens": 55, "latency_ms": 12},
        {"research_run_id": "run-1", "role": "fundamental", "success": True, "input_tokens": 90, "output_tokens": 40, "latency_ms": 8},
    ]
    failures = (_failure(stage="RETRY_EXHAUSTED", code="RETRY_EXHAUSTED", attempt_id="run-1-bear-2", attempt_number=2),)
    metrics = compute_research_failure_metrics(attempt_rows=attempt_rows, failures=failures)
    assert metrics["tokens_spent_on_exhausted_retries"] == {"status": METRIC_STATUS_OK, "value": 100 + 50 + 110 + 55}


def test_attempts_per_completed_role_only_counts_groups_that_eventually_succeeded():
    attempt_rows = [
        {"research_run_id": "run-1", "role": "bear", "success": False, "input_tokens": 1, "output_tokens": 1, "latency_ms": 1},
        {"research_run_id": "run-1", "role": "bear", "success": False, "input_tokens": 1, "output_tokens": 1, "latency_ms": 1},
        {"research_run_id": "run-1", "role": "fundamental", "success": True, "input_tokens": 1, "output_tokens": 1, "latency_ms": 1},
    ]
    metrics = compute_research_failure_metrics(attempt_rows=attempt_rows, failures=())
    # only the (run-1, fundamental) group succeeded — 1 attempt.
    assert metrics["attempts_per_completed_role"] == {"status": METRIC_STATUS_OK, "value": 1.0}
