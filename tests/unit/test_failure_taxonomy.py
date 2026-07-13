"""Milestone 6.1 Step 19: failure-model tests for `research/failure_taxonomy.py`."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_research.research.failure_taxonomy import (
    FailureValidationError,
    MAX_FIELD_PATH_CHARS,
    MAX_MESSAGE_CHARS,
    new_failure,
    sanitize_message,
)

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _base_kwargs(**overrides) -> dict:
    kwargs = dict(
        research_run_id="run-1", attempt_id="run-1-bear-1", role="bear", attempt_number=1,
        stage="CLAIM_EVIDENCE_VALIDATION", code="UNKNOWN_EVIDENCE_ID", message="claim cites unknown evidence",
        model_name="claude-sonnet-5", prompt_version="v1", schema_version="role-report.v1", occurred_at=NOW,
    )
    kwargs.update(overrides)
    return kwargs


def test_valid_failure_constructs():
    failure = new_failure(**_base_kwargs())
    assert failure.role == "bear"
    assert failure.stage == "CLAIM_EVIDENCE_VALIDATION"
    assert failure.code == "UNKNOWN_EVIDENCE_ID"
    assert failure.failure_id.startswith("failure-")


def test_invalid_stage_rejected():
    with pytest.raises(FailureValidationError):
        new_failure(**_base_kwargs(stage="NOT_A_REAL_STAGE"))


def test_invalid_code_rejected():
    with pytest.raises(FailureValidationError):
        new_failure(**_base_kwargs(code="NOT_A_REAL_CODE"))


def test_invalid_role_rejected():
    with pytest.raises(FailureValidationError):
        new_failure(**_base_kwargs(role="not-a-real-role"))


def test_naive_timestamp_rejected():
    """`new_failure` backfills a naive timestamp to UTC (guards the common
    `datetime.now()` mistake at the call site), but the dataclass itself must still
    reject one when constructed directly — this is what the validation requirement
    actually protects against."""
    from trading_research.research.failure_taxonomy import ResearchValidationFailure

    with pytest.raises(FailureValidationError):
        ResearchValidationFailure(
            failure_id="failure-x", research_run_id="run-1", attempt_id="run-1-bear-1", role="bear",
            attempt_number=1, stage="CLAIM_EVIDENCE_VALIDATION", code="UNKNOWN_EVIDENCE_ID", message="m",
            field_path=None, claim_id=None, evidence_ids=(), retryable=True, model_name="m",
            prompt_version="v1", schema_version="s1", occurred_at=datetime(2026, 7, 1), metadata={},
        )


def test_non_positive_attempt_number_rejected():
    with pytest.raises(FailureValidationError):
        new_failure(**_base_kwargs(attempt_number=0))


def test_bounded_message_rejected_when_too_long():
    from trading_research.research.failure_taxonomy import ResearchValidationFailure

    with pytest.raises(FailureValidationError):
        ResearchValidationFailure(
            failure_id="failure-x", research_run_id="run-1", attempt_id="run-1-bear-1", role="bear",
            attempt_number=1, stage="CLAIM_EVIDENCE_VALIDATION", code="UNKNOWN_EVIDENCE_ID",
            message="x" * (MAX_MESSAGE_CHARS + 1), field_path=None, claim_id=None, evidence_ids=(),
            retryable=True, model_name="m", prompt_version="v1", schema_version="s1", occurred_at=NOW, metadata={},
        )


def test_sanitize_message_truncates_oversized_text():
    text = "x" * (MAX_MESSAGE_CHARS + 500)
    cleaned = sanitize_message(text)
    assert len(cleaned) == MAX_MESSAGE_CHARS
    assert cleaned.endswith("...")


def test_bounded_field_path_rejected_when_too_long():
    with pytest.raises(FailureValidationError):
        new_failure(**_base_kwargs(field_path="x" * (MAX_FIELD_PATH_CHARS + 1)))


def test_secret_like_metadata_value_rejected():
    with pytest.raises(FailureValidationError):
        new_failure(**_base_kwargs(metadata={"provider_status_code": "sk-ant-abcdef123456"}))


def test_unknown_metadata_key_rejected():
    with pytest.raises(FailureValidationError):
        new_failure(**_base_kwargs(metadata={"authorization_header": "irrelevant"}))


def test_allowed_metadata_key_accepted():
    failure = new_failure(**_base_kwargs(metadata={"stop_reason": "max_tokens", "output_tokens": 500}))
    assert failure.metadata["stop_reason"] == "max_tokens"
    assert failure.metadata["output_tokens"] == 500


def test_metadata_non_scalar_value_rejected():
    with pytest.raises(FailureValidationError):
        new_failure(**_base_kwargs(metadata={"stop_reason": {"nested": "dict"}}))


def test_deterministic_failure_id_same_content_same_id():
    a = new_failure(**_base_kwargs())
    b = new_failure(**_base_kwargs())
    assert a.failure_id == b.failure_id


def test_deterministic_failure_id_different_content_different_id():
    a = new_failure(**_base_kwargs())
    b = new_failure(**_base_kwargs(code="NUMERIC_VALUE_MISMATCH", message="numeric claim mismatch"))
    assert a.failure_id != b.failure_id


def test_evidence_ids_bounded():
    with pytest.raises(FailureValidationError):
        new_failure(**_base_kwargs(evidence_ids=tuple(f"ev-{i}" for i in range(51))))


def test_empty_evidence_id_rejected():
    with pytest.raises(FailureValidationError):
        new_failure(**_base_kwargs(evidence_ids=("",)))
