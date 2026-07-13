"""Milestone 6.1 Step 11/19: retry-feedback tests for
`research/failure_taxonomy.py::build_retry_feedback`."""
from __future__ import annotations

from datetime import datetime, timezone

from trading_research.research.failure_taxonomy import MAX_RETRY_FEEDBACK_ITEMS, build_retry_feedback, new_failure

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _failure(**overrides):
    kwargs = dict(
        research_run_id="run-1", attempt_id="run-1-bear-1", role="bear", attempt_number=1,
        stage="CLAIM_EVIDENCE_VALIDATION", code="UNKNOWN_EVIDENCE_ID", message="claim cites unknown evidence_id",
        model_name="m", prompt_version="v1", schema_version="role-report.v1", occurred_at=NOW,
    )
    kwargs.update(overrides)
    return new_failure(**kwargs)


def test_empty_failures_produce_no_feedback():
    assert build_retry_feedback(()) == ()


def test_failure_code_and_message_included():
    feedback = build_retry_feedback((_failure(),))
    text = " ".join(feedback)
    assert "UNKNOWN_EVIDENCE_ID" in text
    assert "claim cites unknown evidence_id" in text


def test_field_path_and_claim_id_included_when_present():
    feedback = build_retry_feedback((_failure(field_path="bear_case", claim_id="claim-9"),))
    text = " ".join(feedback)
    assert "field=bear_case" in text
    assert "claim_id=claim-9" in text


def test_duplicate_codes_grouped_with_occurrence_count():
    failures = (
        _failure(claim_id="c1"),
        _failure(claim_id="c2", message="another unknown evidence_id"),
    )
    feedback = build_retry_feedback(failures)
    text = " ".join(feedback)
    assert "2 occurrences" in text
    # grouped means only one line for this shared code, not two full lines
    assert text.count("code=UNKNOWN_EVIDENCE_ID") == 1


def test_bounded_by_max_items():
    failures = tuple(
        _failure(code=code, message=f"failure for {code}")
        for code in [
            "UNKNOWN_EVIDENCE_ID", "STALE_EVIDENCE_REFERENCE", "POINT_IN_TIME_UNSAFE_EVIDENCE",
            "UNSUPPORTED_NUMERIC_CLAIM", "NUMERIC_VALUE_MISMATCH", "UNSUPPORTED_MATERIAL_CLAIM",
            "SCHEMA_TYPE_MISMATCH",
        ]
    )
    feedback = build_retry_feedback(failures, max_items=MAX_RETRY_FEEDBACK_ITEMS)
    code_lines = [line for line in feedback if line.startswith("code=")]
    assert len(code_lines) == MAX_RETRY_FEEDBACK_ITEMS


def test_allowed_evidence_ids_included_when_supplied():
    feedback = build_retry_feedback((_failure(),), allowed_evidence_ids=("ev-1", "ev-2"))
    text = " ".join(feedback)
    assert "ev-1" in text and "ev-2" in text
    assert "Allowed evidence_id values" in text


def test_allowed_evidence_ids_omitted_when_not_supplied():
    feedback = build_retry_feedback((_failure(),))
    text = " ".join(feedback)
    assert "Allowed evidence_id values" not in text


def test_full_replacement_report_requested():
    feedback = build_retry_feedback((_failure(),))
    text = " ".join(feedback)
    assert "complete replacement report" in text
    assert "order instructions" in text or "allocation" in text


def test_raw_previous_response_never_included():
    """Feedback is built entirely from sanitized ResearchValidationFailure fields — it can
    never contain a raw provider response, since it never receives one."""
    feedback = build_retry_feedback((_failure(message="claim cites unknown evidence_id"),))
    text = " ".join(feedback)
    assert '"parsed_json"' not in text
    assert "tool_use" not in text


def test_no_secret_in_feedback():
    feedback = build_retry_feedback((_failure(metadata={"stop_reason": "max_tokens"}),))
    text = " ".join(feedback).lower()
    assert "api_key" not in text
    assert "sk-ant-" not in text
    assert "authorization" not in text
