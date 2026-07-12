"""Orchestration service: frozen recommendation → paper execution → ledger →
reconciliation (docs/milestone-3.md Step 8).

A deterministic **application service**, matching the existing
`services/analyze_candidate.py` convention: it wires together already-built
collaborators (repositories, ledger, adapter, eligibility policy, clock) and
runs one fixed sequence. It never decides eligibility itself (that's
`execution/eligibility.py`), never computes prices (those come from the
already-frozen recommendation), and never talks to LumiBot directly (that's
behind the injected `PaperExecutionAdapter`).

Idempotency / resumability: the very first thing this service does after
loading the recommendation is check for an already-persisted intent for
`(recommendation_id, execution_version)`. If a result already exists for
that intent, this call is a pure read — no adapter call, no ledger mutation,
no new intent. If an intent exists but no result yet (a prior invocation was
interrupted after persisting the intent but before completing), execution
resumes from that persisted intent rather than re-running eligibility or
building a second intent. Only when no intent exists at all does eligibility
run and a new intent get built — this is what makes "repeated service
invocation must not submit a second paper order" hold (docs/milestone-3.md
Step 7/12).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable

from ..execution.adapter_protocol import PaperExecutionAdapter
from ..execution.config import ExecutionConfig
from ..execution.eligibility import PaperExecutionEligibilityPolicy
from ..execution.intent_builder import build_paper_order_intent
from ..execution.ledger_events import apply_all_new_events
from ..execution.models import PaperExecutionEligibility, PaperExecutionResult, PaperOrderIntent, ReconciliationResult
from ..execution.reconciliation import reconcile_intent
from ..paper.ledger import LedgerError, PaperLedger
from ..storage import execution_repositories as exec_repo
from ..storage.trading_repositories import load_recommendation

STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_REJECTED_INELIGIBLE = "REJECTED_INELIGIBLE"
STATUS_EXECUTED = "EXECUTED"
STATUS_RESUMED = "RESUMED"
STATUS_ADAPTER_ERROR = "ADAPTER_ERROR"


class RecommendationNotFoundError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaperExecutionOutcome:
    recommendation_id: str
    status: str
    intent: PaperOrderIntent | None = None
    eligibility: PaperExecutionEligibility | None = None
    result: PaperExecutionResult | None = None
    reconciliation: ReconciliationResult | None = None


def execute_paper_recommendation(
    recommendation_id: str,
    *,
    conn: sqlite3.Connection,
    execution_config: ExecutionConfig,
    ledger: PaperLedger,
    adapter: PaperExecutionAdapter,
    eligibility_policy: PaperExecutionEligibilityPolicy,
    git_sha: str,
    clock: Callable[[], datetime],
) -> PaperExecutionOutcome:
    now = clock()
    recommendation = load_recommendation(conn, recommendation_id)
    if recommendation is None:
        raise RecommendationNotFoundError(
            f"recommendation {recommendation_id!r} does not exist or has no persisted payload"
        )

    existing_intent = exec_repo.get_intent_by_recommendation(
        conn, recommendation_id, execution_config.execution_version
    )
    if existing_intent is not None:
        existing_result = exec_repo.get_result(conn, existing_intent.intent_id)
        if existing_result is not None:
            # Already fully executed — pure read, no adapter call, no ledger mutation.
            return PaperExecutionOutcome(
                recommendation_id=recommendation_id,
                status=STATUS_RESUMED,
                intent=existing_intent,
                result=existing_result,
                reconciliation=exec_repo.get_reconciliation(conn, existing_intent.intent_id),
            )
        # Intent persisted but no result yet — a prior invocation was interrupted.
        return _submit_and_settle(
            recommendation, existing_intent, conn=conn, ledger=ledger, adapter=adapter, now=now,
        )

    eligibility = eligibility_policy.evaluate(recommendation, conn=conn, now=now)
    if not eligibility.eligible:
        exec_repo.record_failure(
            conn, recommendation_id=recommendation_id, intent_id=None, stage="eligibility",
            reason="; ".join(eligibility.reasons), now=now,
        )
        return PaperExecutionOutcome(
            recommendation_id=recommendation_id, status=STATUS_REJECTED_INELIGIBLE, eligibility=eligibility,
        )

    intent = build_paper_order_intent(recommendation, config=execution_config, git_sha=git_sha)
    exec_repo.save_intent(conn, intent, now=now)

    return _submit_and_settle(
        recommendation, intent, conn=conn, ledger=ledger, adapter=adapter, now=now, eligibility=eligibility,
    )


def _submit_and_settle(
    recommendation: dict,
    intent: PaperOrderIntent,
    *,
    conn: sqlite3.Connection,
    ledger: PaperLedger,
    adapter: PaperExecutionAdapter,
    now: datetime,
    eligibility: PaperExecutionEligibility | None = None,
) -> PaperExecutionOutcome:
    try:
        events, result = adapter.submit(intent)
    except Exception as exc:  # adapter implementations are arbitrary/pluggable at this boundary
        exec_repo.record_failure(
            conn, recommendation_id=intent.recommendation_id, intent_id=intent.intent_id,
            stage="adapter_submit", reason=str(exc), now=now,
        )
        return PaperExecutionOutcome(
            recommendation_id=intent.recommendation_id, status=STATUS_ADAPTER_ERROR,
            intent=intent, eligibility=eligibility,
        )

    for event in events:
        # `apply_all_new_events` persists each event (idempotently) before
        # deciding whether it changes the ledger — "normalize and persist"
        # and "apply to the ledger" happen together per event, see
        # execution/ledger_events.py.
        try:
            apply_all_new_events(conn, ledger, (event,), now=now)
        except LedgerError as exc:
            # Retain the persisted event; do not mark it ledger_applied. A
            # retry (or reconciliation) will surface the drift rather than
            # silently dropping a broker-reported fill.
            exec_repo.record_failure(
                conn, recommendation_id=intent.recommendation_id, intent_id=intent.intent_id,
                stage="ledger_apply", reason=f"{event.event_id}: {exc}", now=now,
            )

    exec_repo.save_result(conn, result)

    reconciliation = _reconcile(conn, ledger, adapter, intent, now)

    return PaperExecutionOutcome(
        recommendation_id=intent.recommendation_id, status=STATUS_EXECUTED,
        intent=intent, eligibility=eligibility, result=result, reconciliation=reconciliation,
    )


def _reconcile(
    conn: sqlite3.Connection, ledger: PaperLedger, adapter: PaperExecutionAdapter, intent: PaperOrderIntent,
    now: datetime,
) -> ReconciliationResult | None:
    try:
        broker_snapshot = adapter.reconcile(intent.intent_id)
    except Exception as exc:
        exec_repo.record_failure(
            conn, recommendation_id=intent.recommendation_id, intent_id=intent.intent_id,
            stage="adapter_reconcile", reason=str(exc), now=now,
        )
        return None

    ledger_quantity, ledger_notional = _ledger_position_for(ledger, intent)
    reconciliation = reconcile_intent(
        intent, broker_snapshot, ledger_quantity=ledger_quantity, ledger_notional=ledger_notional, now=now,
    )
    exec_repo.save_reconciliation(conn, reconciliation)
    return reconciliation


def _ledger_position_for(ledger: PaperLedger, intent: PaperOrderIntent) -> tuple[int, Decimal]:
    for pos in ledger.positions():
        if pos["symbol"] == intent.symbol:
            qty = int(pos["qty"])
            notional = Decimal(str(pos["avg_cost"])) * qty
            return qty, notional
    return 0, Decimal("0")
