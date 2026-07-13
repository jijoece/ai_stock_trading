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
    (docs/milestone-6.md: "No execution from a shadow experiment arm")."""
    validate_experiment_policy(policy)
    return False
