"""Experiment execution policy (docs/milestone-6.md Step 15) — decides which
experiment arm(s), if any, a scheduled cycle may submit to the existing
paper-execution pipeline. This module never calls
`services/execute_paper_recommendation.py` itself; it only answers "may the
baseline/enhanced recommendation for this cycle be submitted" so the
scheduled-cycle service can decide, keeping all actual execution in the
Milestone 3/4 pipeline unchanged.
"""
from __future__ import annotations


class UnknownExperimentPolicyError(RuntimeError):
    """An experiment policy name outside the known set — fails closed."""


class UnsupportedExperimentPolicyError(RuntimeError):
    """A recognized policy that requires infrastructure this repository does
    not implement yet (separate paper-portfolio namespaces for concurrently
    executing arms) — fails closed rather than submitting two competing
    orders into the same paper account."""


OBSERVE_ONLY = "OBSERVE_ONLY"
BASELINE_ONLY = "BASELINE_ONLY"
ENHANCED_ONLY = "ENHANCED_ONLY"
BOTH_SEPARATE_PAPER_BOOKS = "BOTH_SEPARATE_PAPER_BOOKS"
SHADOW_ENHANCED = "SHADOW_ENHANCED"

KNOWN_POLICIES = (OBSERVE_ONLY, BASELINE_ONLY, ENHANCED_ONLY, BOTH_SEPARATE_PAPER_BOOKS, SHADOW_ENHANCED)

# Policies fully supported without a separate paper-portfolio namespace.
# `ENHANCED_ONLY` and `BOTH_SEPARATE_PAPER_BOOKS` are recognized names (so
# config validation can name them) but are not selectable yet — see
# docs/milestone-6.md Step 15: "If both arms execute later, implement
# separate logical paper portfolios or allocation namespaces first."
_SUPPORTED_POLICIES = (OBSERVE_ONLY, BASELINE_ONLY, SHADOW_ENHANCED)

DEFAULT_EXPERIMENT_POLICY = SHADOW_ENHANCED


def validate_experiment_policy(policy: str) -> None:
    if policy not in KNOWN_POLICIES:
        raise UnknownExperimentPolicyError(f"experiment policy {policy!r} is not one of {KNOWN_POLICIES} — fails closed")
    if policy not in _SUPPORTED_POLICIES:
        raise UnsupportedExperimentPolicyError(
            f"experiment policy {policy!r} requires separate paper-portfolio namespaces, "
            "which this repository does not implement — fails closed"
        )


def may_submit_baseline(policy: str) -> bool:
    validate_experiment_policy(policy)
    return policy == BASELINE_ONLY or policy == SHADOW_ENHANCED


def may_submit_enhanced(policy: str) -> bool:
    """Always False for every currently supported policy — the enhanced arm
    is generated and evaluated but never executes
    (docs/milestone-6.md: "No execution from a shadow experiment arm").

    This function governs only the legacy global `paper/ledger.py` submission
    path and is unchanged by Milestone 8 — it must remain unconditionally
    False regardless of any paper-book configuration. Isolated paper-book
    submission is governed by the separate
    `may_submit_enhanced_to_paper_book`/`may_submit_baseline_to_paper_book`
    functions below, which never affect this function's behavior."""
    validate_experiment_policy(policy)
    return False


# -- Milestone 8: isolated paper-book policy (docs/milestone-8.md Step 13) --
#
# These functions are purely additive — every function above this comment is
# unchanged in signature and behavior. `ENHANCED_ONLY`/`BOTH_SEPARATE_PAPER_BOOKS`
# become selectable *only* through this separate policy surface, and only
# when the matching isolated paper book is explicitly enabled in
# `config/paper_books.yaml`. Enhanced paper-book submission can never fall
# back to the baseline book, and baseline paper-book submission can never
# fall back to the enhanced book — each policy value maps to an exact,
# fixed set of books, never inferred.


def validate_paper_book_experiment_policy(
    policy: str, *, baseline_book_enabled: bool, enhanced_book_enabled: bool
) -> None:
    """Fails closed unless the policy is a known name and every isolated
    book it requires is explicitly enabled. `OBSERVE_ONLY` never requires
    any book (it submits nothing to either arm)."""
    if policy not in KNOWN_POLICIES:
        raise UnknownExperimentPolicyError(f"experiment policy {policy!r} is not one of {KNOWN_POLICIES} — fails closed")
    if policy == OBSERVE_ONLY:
        return
    requires_baseline = policy in (BASELINE_ONLY, SHADOW_ENHANCED, BOTH_SEPARATE_PAPER_BOOKS)
    requires_enhanced = policy in (ENHANCED_ONLY, BOTH_SEPARATE_PAPER_BOOKS)
    if requires_baseline and not baseline_book_enabled:
        raise UnsupportedExperimentPolicyError(
            f"experiment policy {policy!r} requires the isolated BASELINE paper book to be "
            "enabled in config/paper_books.yaml — fails closed"
        )
    if requires_enhanced and not enhanced_book_enabled:
        raise UnsupportedExperimentPolicyError(
            f"experiment policy {policy!r} requires the isolated ENHANCED paper book to be "
            "enabled in config/paper_books.yaml — fails closed"
        )


def may_submit_baseline_to_paper_book(
    policy: str, *, baseline_book_enabled: bool, enhanced_book_enabled: bool
) -> bool:
    validate_paper_book_experiment_policy(
        policy, baseline_book_enabled=baseline_book_enabled, enhanced_book_enabled=enhanced_book_enabled
    )
    return policy in (BASELINE_ONLY, SHADOW_ENHANCED, BOTH_SEPARATE_PAPER_BOOKS)


def may_submit_enhanced_to_paper_book(
    policy: str, *, baseline_book_enabled: bool, enhanced_book_enabled: bool
) -> bool:
    """True only for `ENHANCED_ONLY`/`BOTH_SEPARATE_PAPER_BOOKS`, and only
    when the isolated enhanced book is enabled. This is the *only* function
    in the entire repository that can ever return True for enhanced
    submission — and even then, only to the isolated enhanced paper book,
    never to any live destination or to the baseline book."""
    validate_paper_book_experiment_policy(
        policy, baseline_book_enabled=baseline_book_enabled, enhanced_book_enabled=enhanced_book_enabled
    )
    return policy in (ENHANCED_ONLY, BOTH_SEPARATE_PAPER_BOOKS)
