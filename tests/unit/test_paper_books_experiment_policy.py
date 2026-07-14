"""Tests for the additive paper-book policy functions in
research/experiment_policy.py (docs/milestone-8.md Step 13)."""
from __future__ import annotations

import pytest

from trading_research.research import experiment_policy as ep


def test_both_separate_paper_books_fails_closed_when_enhanced_disabled():
    with pytest.raises(ep.UnsupportedExperimentPolicyError):
        ep.validate_paper_book_experiment_policy(
            ep.BOTH_SEPARATE_PAPER_BOOKS, baseline_book_enabled=True, enhanced_book_enabled=False,
        )


def test_both_separate_paper_books_fails_closed_when_baseline_disabled():
    with pytest.raises(ep.UnsupportedExperimentPolicyError):
        ep.validate_paper_book_experiment_policy(
            ep.BOTH_SEPARATE_PAPER_BOOKS, baseline_book_enabled=False, enhanced_book_enabled=True,
        )


def test_both_separate_paper_books_succeeds_when_both_enabled():
    ep.validate_paper_book_experiment_policy(
        ep.BOTH_SEPARATE_PAPER_BOOKS, baseline_book_enabled=True, enhanced_book_enabled=True,
    )
    assert ep.may_submit_baseline_to_paper_book(
        ep.BOTH_SEPARATE_PAPER_BOOKS, baseline_book_enabled=True, enhanced_book_enabled=True,
    ) is True
    assert ep.may_submit_enhanced_to_paper_book(
        ep.BOTH_SEPARATE_PAPER_BOOKS, baseline_book_enabled=True, enhanced_book_enabled=True,
    ) is True


def test_enhanced_only_fails_closed_without_enhanced_book():
    with pytest.raises(ep.UnsupportedExperimentPolicyError):
        ep.may_submit_enhanced_to_paper_book(
            ep.ENHANCED_ONLY, baseline_book_enabled=True, enhanced_book_enabled=False,
        )


def test_enhanced_only_succeeds_with_enhanced_book_enabled():
    assert ep.may_submit_enhanced_to_paper_book(
        ep.ENHANCED_ONLY, baseline_book_enabled=False, enhanced_book_enabled=True,
    ) is True
    assert ep.may_submit_baseline_to_paper_book(
        ep.ENHANCED_ONLY, baseline_book_enabled=False, enhanced_book_enabled=True,
    ) is False


def test_observe_only_never_requires_any_book():
    ep.validate_paper_book_experiment_policy(ep.OBSERVE_ONLY, baseline_book_enabled=False, enhanced_book_enabled=False)
    assert ep.may_submit_baseline_to_paper_book(ep.OBSERVE_ONLY, baseline_book_enabled=False, enhanced_book_enabled=False) is False
    assert ep.may_submit_enhanced_to_paper_book(ep.OBSERVE_ONLY, baseline_book_enabled=False, enhanced_book_enabled=False) is False


def test_unknown_policy_fails_closed():
    with pytest.raises(ep.UnknownExperimentPolicyError):
        ep.validate_paper_book_experiment_policy("NOT_A_REAL_POLICY", baseline_book_enabled=True, enhanced_book_enabled=True)


def test_legacy_may_submit_enhanced_is_unaffected_by_paper_book_state():
    """The pre-existing, hardcoded-False legacy function must never be
    influenced by paper-book enablement — the two policy surfaces are
    completely independent."""
    assert ep.may_submit_enhanced(ep.SHADOW_ENHANCED) is False
    # Even after confirming ENHANCED_ONLY is fully permitted for paper books,
    # the legacy global-ledger function is still unconditionally False.
    ep.may_submit_enhanced_to_paper_book(ep.ENHANCED_ONLY, baseline_book_enabled=False, enhanced_book_enabled=True)
    assert ep.may_submit_enhanced(ep.SHADOW_ENHANCED) is False


def test_baseline_only_never_grants_enhanced_paper_submission():
    assert ep.may_submit_enhanced_to_paper_book(
        ep.BASELINE_ONLY, baseline_book_enabled=True, enhanced_book_enabled=True,
    ) is False


def test_disabling_enhanced_book_prevents_enhanced_intent_creation_end_to_end():
    """Simulates the CLI-level guard: an operator requests
    BOTH_SEPARATE_PAPER_BOOKS but the enhanced book is disabled — no
    enhanced order intent may ever be built."""
    baseline_enabled, enhanced_enabled = True, False
    with pytest.raises(ep.UnsupportedExperimentPolicyError):
        ep.may_submit_enhanced_to_paper_book(
            ep.BOTH_SEPARATE_PAPER_BOOKS, baseline_book_enabled=baseline_enabled, enhanced_book_enabled=enhanced_enabled,
        )
