"""Unit tests for research/experiment_policy.py — Milestone 6
docs/milestone-6.md Step 22 category I."""
from __future__ import annotations

import pytest

from trading_research.research import experiment_policy as ep


def test_observe_only_never_submits_either_arm():
    assert ep.may_submit_baseline(ep.OBSERVE_ONLY) is False
    assert ep.may_submit_enhanced(ep.OBSERVE_ONLY) is False


def test_baseline_only_submits_baseline_never_enhanced():
    assert ep.may_submit_baseline(ep.BASELINE_ONLY) is True
    assert ep.may_submit_enhanced(ep.BASELINE_ONLY) is False


def test_shadow_enhanced_submits_baseline_never_enhanced():
    assert ep.may_submit_baseline(ep.SHADOW_ENHANCED) is True
    assert ep.may_submit_enhanced(ep.SHADOW_ENHANCED) is False


def test_enhanced_cannot_execute_under_any_supported_policy():
    for policy in (ep.OBSERVE_ONLY, ep.BASELINE_ONLY, ep.SHADOW_ENHANCED):
        assert ep.may_submit_enhanced(policy) is False


def test_unsupported_policies_fail_closed_not_execute():
    for policy in (ep.ENHANCED_ONLY, ep.BOTH_SEPARATE_PAPER_BOOKS):
        with pytest.raises(ep.UnsupportedExperimentPolicyError):
            ep.may_submit_baseline(policy)


def test_unknown_policy_fails_closed():
    with pytest.raises(ep.UnknownExperimentPolicyError):
        ep.validate_experiment_policy("NOT_A_REAL_POLICY")


def test_default_policy_is_shadow_enhanced():
    assert ep.DEFAULT_EXPERIMENT_POLICY == ep.SHADOW_ENHANCED
