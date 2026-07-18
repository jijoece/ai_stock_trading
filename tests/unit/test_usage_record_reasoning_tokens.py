"""Unit tests for `UsageRecord`'s reasoning-token invariant (Milestone 12.1
Item 4, required test #4: invalid negative values fail)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from trading_research.research.errors import EvidenceValidationError
from trading_research.research.models import (
    COST_BASIS_NOT_APPLICABLE,
    COST_NOT_APPLICABLE,
    TOKEN_ACCOUNTING_NOT_APPLICABLE,
    TOKEN_ACCOUNTING_REASONING_INCLUDED_IN_OUTPUT,
    UsageRecord,
)


def _record(**overrides) -> UsageRecord:
    defaults = dict(
        provider="codex", model_name="gpt-5.1-codex", role="fundamental", input_tokens=10, output_tokens=7,
        cache_read_tokens=None, cache_write_tokens=None, latency_ms=100, provider_request_id=None, retry_count=0,
        success=True, pricing_version=None, estimated_cost=None, cost_status=COST_NOT_APPLICABLE,
        cost_estimate_basis=COST_BASIS_NOT_APPLICABLE,
    )
    defaults.update(overrides)
    return UsageRecord(**defaults)


def test_negative_reasoning_tokens_rejected():
    with pytest.raises(EvidenceValidationError):
        _record(reasoning_output_tokens=-1)


def test_reasoning_exceeding_output_under_inclusion_policy_rejected():
    with pytest.raises(EvidenceValidationError):
        _record(
            output_tokens=7, reasoning_output_tokens=8,
            token_accounting_policy=TOKEN_ACCOUNTING_REASONING_INCLUDED_IN_OUTPUT,
        )


def test_reasoning_equal_to_output_under_inclusion_policy_accepted():
    record = _record(
        output_tokens=7, reasoning_output_tokens=7,
        token_accounting_policy=TOKEN_ACCOUNTING_REASONING_INCLUDED_IN_OUTPUT,
    )
    assert record.reasoning_output_tokens == 7


def test_default_token_accounting_policy_is_not_applicable():
    record = _record()
    assert record.token_accounting_policy == TOKEN_ACCOUNTING_NOT_APPLICABLE
    assert record.reasoning_output_tokens is None


def test_unknown_token_accounting_policy_rejected():
    with pytest.raises(EvidenceValidationError):
        _record(token_accounting_policy="MADE_UP_POLICY")
