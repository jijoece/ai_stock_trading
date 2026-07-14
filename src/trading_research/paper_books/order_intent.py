"""Book-aware, immutable paper order intent construction (docs/milestone-8.md
Step 12). A rejected `PaperRiskDecision` never produces a submit-ready
intent — `build_order_intent` returns `None` in that case, after the risk
decision itself has still been persisted (queryable audit trail either way).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from ..storage import paper_books_repositories as repo
from .models import (
    APPROVED_RISK_DECISIONS,
    ORDER_SIDE_BUY,
    ORDER_TYPE_LIMIT,
    PaperBookOrderIntent,
    derive_paper_order_intent_id,
)

EXECUTION_VERSION = "paper-books-execution-v1"


def persist_risk_decision(
    conn, book_id: str, cycle_id: str, recommendation_id: str, symbol: str, decision,
    portfolio_snapshot_id: str | None, clock,
) -> str:
    """Every risk decision (approved or rejected) gets a stable, deterministic
    ID and is persisted unconditionally — a rejected recommendation still has
    a queryable audit trail (Step 11: "decisions persisted")."""
    import hashlib

    digest = hashlib.sha256(f"{book_id}:{cycle_id}:{recommendation_id}:{symbol}".encode()).hexdigest()[:32]
    risk_decision_id = f"pb-risk-{digest}"
    repo.save_risk_decision(
        conn, risk_decision_id, book_id, cycle_id, recommendation_id, symbol, decision,
        portfolio_snapshot_id, clock(),
    )
    return risk_decision_id


def build_order_intent(
    *, book_id: str, experiment_arm: str, cycle_id: str, recommendation_id: str, symbol: str,
    risk_decision, risk_decision_id: str, portfolio_snapshot_id: str, config_hash: str,
    as_of: datetime, time_in_force: str = "DAY", clock,
) -> PaperBookOrderIntent | None:
    if risk_decision.decision not in APPROVED_RISK_DECISIONS:
        return None
    intent_id = derive_paper_order_intent_id(recommendation_id, book_id, EXECUTION_VERSION)
    limit_price = risk_decision.approved_notional_usd / risk_decision.approved_quantity
    return PaperBookOrderIntent(
        paper_order_intent_id=intent_id, book_id=book_id, experiment_arm=experiment_arm, cycle_id=cycle_id,
        recommendation_id=recommendation_id, symbol=symbol, side=ORDER_SIDE_BUY, order_type=ORDER_TYPE_LIMIT,
        quantity=risk_decision.approved_quantity, limit_price=limit_price,
        notional_usd=risk_decision.approved_notional_usd, time_in_force=time_in_force, as_of=as_of,
        risk_decision_id=risk_decision_id, portfolio_snapshot_id=portfolio_snapshot_id, config_hash=config_hash,
        created_at=clock(),
    )


def persist_order_intent(conn, intent: PaperBookOrderIntent) -> bool:
    """Idempotent: a retried service invocation resolves to the same
    (book_id, paper_order_intent_id) and is a no-op (never a second row)."""
    return repo.save_order_intent(conn, intent)
