"""Paper-book promotion evidence (docs/milestone-8.md Step 21) — evidence
only, never an authorization. Extends (does not replace)
`research/promotion.py`'s existing arm-agnostic promotion gate; no result
value here means "promoted" — only "eligible for human review." No
automatic promotion exists anywhere in this module.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from ..storage import paper_books_repositories as repo
from .comparison import PaperExperimentComparison

PROMOTION_EVIDENCE_POLICY_VERSION = "paper-books-promotion-evidence-v1"

RESULT_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
RESULT_NOT_COMPARABLE = "NOT_COMPARABLE"
RESULT_BASELINE_OUTPERFORMS = "BASELINE_OUTPERFORMS"
RESULT_ENHANCED_OUTPERFORMS_OBSERVED = "ENHANCED_OUTPERFORMS_OBSERVED"
RESULT_ENHANCED_OUTPERFORMS_NOT_PROMOTABLE = "ENHANCED_OUTPERFORMS_NOT_PROMOTABLE"
RESULT_PROMOTION_REVIEW_ELIGIBLE = "PROMOTION_REVIEW_ELIGIBLE"


def evaluate_promotion_evidence(
    comparison: PaperExperimentComparison, enhanced_metrics: dict, *, cycle_count: int,
    min_comparable_cycles: int, min_trading_days: int, min_closed_trades: int,
    operational_health_ok: bool, reconciliation_ok: bool,
) -> tuple[str, tuple[str, ...]]:
    if not comparison.comparable:
        return RESULT_NOT_COMPARABLE, comparison.comparability_reasons

    return_delta = comparison.metric_deltas.get("cumulative_return")
    if return_delta is None:
        return RESULT_INSUFFICIENT_DATA, ("cumulative_return delta is unavailable for this window",)

    trading_days = len(enhanced_metrics.get("daily_returns") or [])
    closed_trades = enhanced_metrics.get("trade_count") or 0
    sample_floor_reasons = []
    if cycle_count < min_comparable_cycles:
        sample_floor_reasons.append(f"comparable cycles {cycle_count} < required {min_comparable_cycles}")
    if trading_days < min_trading_days:
        sample_floor_reasons.append(f"trading days {trading_days} < required {min_trading_days}")
    if closed_trades < min_closed_trades:
        sample_floor_reasons.append(f"closed trades {closed_trades} < required {min_closed_trades}")
    sample_floors_met = not sample_floor_reasons

    if return_delta <= 0:
        if sample_floors_met:
            return RESULT_BASELINE_OUTPERFORMS, (f"enhanced cumulative_return delta {return_delta} <= 0",)
        return RESULT_INSUFFICIENT_DATA, tuple(
            [f"cumulative_return delta {return_delta} <= 0 but sample floors not met"] + sample_floor_reasons
        )

    # return_delta > 0 from here on — a positive observed signal.
    if not sample_floors_met:
        return RESULT_ENHANCED_OUTPERFORMS_OBSERVED, tuple(
            [f"positive cumulative_return delta {return_delta} observed, but sample floors not met — not yet promotable"]
            + sample_floor_reasons
        )
    if not operational_health_ok or not reconciliation_ok:
        blocking = []
        if not operational_health_ok:
            blocking.append("operational health check failed")
        if not reconciliation_ok:
            blocking.append("reconciliation status blocks promotion review")
        return RESULT_ENHANCED_OUTPERFORMS_NOT_PROMOTABLE, tuple(
            [f"positive cumulative_return delta {return_delta} at sufficient sample size"] + blocking
        )

    return RESULT_PROMOTION_REVIEW_ELIGIBLE, (
        f"positive cumulative_return delta {return_delta} at sufficient sample size and healthy operations "
        "— eligible for human review, not an automatic promotion",
    )


def save_promotion_evidence(conn, comparison: PaperExperimentComparison, result: str, reasons: tuple[str, ...], *, clock) -> str:
    promotion_evidence_id = f"pb-promo-{uuid.uuid5(uuid.NAMESPACE_URL, comparison.comparison_id)}"
    repo.save_promotion_evidence(conn, {
        "promotion_evidence_id": promotion_evidence_id, "experiment_id": comparison.experiment_id,
        "comparison_id": comparison.comparison_id, "result": result, "reasons": reasons,
        "policy_version": PROMOTION_EVIDENCE_POLICY_VERSION, "created_at": clock(),
    })
    return promotion_evidence_id
