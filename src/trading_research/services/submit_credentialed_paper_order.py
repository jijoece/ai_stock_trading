"""Credentialed paper-broker submission (docs/milestone-4.md Step 8/11).

A sibling orchestrator to `services/execute_paper_recommendation.py`, not a
replacement — that service's synchronous `PaperExecutionAdapter.submit()`
contract assumes a fill is known by the time `submit()` returns (true for
the Milestone 3 deterministic/LumiBot-in-process adapters). A real
credentialed broker submission is asynchronous: submission is acknowledged
now, fills arrive later (docs/milestone-4.md Step 9's polling loop, see
`services/sync_paper_orders.py`). This module owns exactly the
acknowledgement half of that split.

Idempotency/crash-safety (docs/milestone-4.md Step 8): a `paper_broker_
submissions` row is created in `PENDING_SUBMISSION` *before* the runtime is
ever contacted. Every call — first attempt, retried call after a crash, or
a duplicate service invocation — starts by asking the runtime whether an
order already exists for this intent's derived client-order id
(`get_order`) before ever calling `submit_order`. This single check
correctly handles both "never submitted yet" (broker says unknown -> safe
to submit) and "a prior attempt may have reached the broker before this
process died" (broker says it exists -> never resubmit) with one code
path, and requires no separate "was this a restart" flag.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from ..execution.broker_snapshots import BrokerOrderSubmission
from ..execution.config import ExecutionConfig
from ..execution.eligibility import PaperExecutionEligibilityPolicy
from ..execution.intent_builder import build_paper_order_intent
from ..execution.models import PaperExecutionEligibility, PaperOrderIntent
from ..runtime.client.errors import RuntimeOperationError, RuntimeRequestTimeoutError, RuntimeUnavailableError
from ..runtime.client.models import derive_client_order_id, intent_to_submit_payload
from ..runtime.client.process_client import RuntimeClient
from ..storage import execution_repositories as exec_repo
from ..storage.trading_repositories import load_recommendation
from .execute_paper_recommendation import RecommendationNotFoundError

STATUS_REJECTED_INELIGIBLE = "REJECTED_INELIGIBLE"
STATUS_SUBMITTED = "SUBMITTED"  # broker has acknowledged this order (new or already existing)
STATUS_RESUMED = "RESUMED"  # submission was already terminal
STATUS_SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"  # ambiguous outcome; do not resubmit, poll later
STATUS_RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"


@dataclass(frozen=True)
class CredentialedSubmissionOutcome:
    recommendation_id: str
    status: str
    intent: PaperOrderIntent | None = None
    eligibility: PaperExecutionEligibility | None = None
    submission: BrokerOrderSubmission | None = None


def submit_credentialed_paper_order(
    recommendation_id: str,
    *,
    conn: sqlite3.Connection,
    execution_config: ExecutionConfig,
    eligibility_policy: PaperExecutionEligibilityPolicy,
    client: RuntimeClient,
    git_sha: str,
    clock: Callable[[], datetime],
) -> CredentialedSubmissionOutcome:
    now = clock()
    recommendation = load_recommendation(conn, recommendation_id)
    if recommendation is None:
        raise RecommendationNotFoundError(
            f"recommendation {recommendation_id!r} does not exist or has no persisted payload"
        )

    intent = exec_repo.get_intent_by_recommendation(conn, recommendation_id, execution_config.execution_version)
    eligibility: PaperExecutionEligibility | None = None
    if intent is None:
        eligibility = eligibility_policy.evaluate(recommendation, conn=conn, now=now)
        if not eligibility.eligible:
            exec_repo.record_failure(
                conn, recommendation_id=recommendation_id, intent_id=None, stage="eligibility",
                reason="; ".join(eligibility.reasons), now=now,
            )
            return CredentialedSubmissionOutcome(
                recommendation_id=recommendation_id, status=STATUS_REJECTED_INELIGIBLE, eligibility=eligibility,
            )
        intent = build_paper_order_intent(recommendation, config=execution_config, git_sha=git_sha)
        exec_repo.save_intent(conn, intent, now=now)

    client_order_id = derive_client_order_id(intent)
    submission = exec_repo.get_submission(conn, intent.intent_id)
    if submission is not None and submission.is_terminal():
        return CredentialedSubmissionOutcome(
            recommendation_id=recommendation_id, status=STATUS_RESUMED, intent=intent,
            eligibility=eligibility, submission=submission,
        )
    if submission is None:
        exec_repo.create_pending_submission(conn, intent_id=intent.intent_id, client_order_id=client_order_id, now=now)

    # Always look the order up before ever calling submit_order — this one
    # check covers both "never submitted" (not found) and "a previous
    # attempt may already have reached the broker" (found) without a
    # separate restart flag.
    try:
        current = client.get_order(client_order_id)
    except (RuntimeUnavailableError, RuntimeRequestTimeoutError) as exc:
        exec_repo.update_submission_status(
            conn, intent_id=intent.intent_id, submission_status="SUBMISSION_UNKNOWN",
            broker_order_id=None, now=now, increment_attempt=True,
        )
        exec_repo.record_failure(
            conn, recommendation_id=recommendation_id, intent_id=intent.intent_id,
            stage="credentialed_lookup", reason=str(exc), now=now,
        )
        return CredentialedSubmissionOutcome(
            recommendation_id=recommendation_id, status=STATUS_RUNTIME_UNAVAILABLE, intent=intent,
            eligibility=eligibility, submission=exec_repo.get_submission(conn, intent.intent_id),
        )

    if current is not None:
        exec_repo.update_submission_status(
            conn, intent_id=intent.intent_id, submission_status=current["status"],
            broker_order_id=current.get("broker_order_id"), now=now,
        )
        return CredentialedSubmissionOutcome(
            recommendation_id=recommendation_id, status=STATUS_SUBMITTED, intent=intent,
            eligibility=eligibility, submission=exec_repo.get_submission(conn, intent.intent_id),
        )

    payload = intent_to_submit_payload(intent)
    try:
        response = client.submit_order(payload)
    except (RuntimeUnavailableError, RuntimeRequestTimeoutError, RuntimeOperationError) as exc:
        # Ambiguous: the broker may have received the order despite the
        # error. One recovery lookup, never a blind resubmit. Milestone
        # 11.2 Part 22: a failure of this recovery lookup itself must not
        # be silently discarded — it is bounded, sanitized evidence in its
        # own right (distinct from the original submit failure recorded
        # below), since it could indicate a different, more urgent problem
        # (e.g. credentials expired between the submit and the lookup).
        recovered = None
        try:
            recovered = client.get_order(client_order_id)
        except Exception as lookup_exc:
            recovered = None
            exec_repo.record_failure(
                conn, recommendation_id=recommendation_id, intent_id=intent.intent_id,
                stage="credentialed_recovery_lookup", reason=str(lookup_exc), now=now,
            )
        if recovered is not None:
            exec_repo.update_submission_status(
                conn, intent_id=intent.intent_id, submission_status=recovered["status"],
                broker_order_id=recovered.get("broker_order_id"), now=now, increment_attempt=True,
            )
            return CredentialedSubmissionOutcome(
                recommendation_id=recommendation_id, status=STATUS_SUBMITTED, intent=intent,
                eligibility=eligibility, submission=exec_repo.get_submission(conn, intent.intent_id),
            )
        exec_repo.update_submission_status(
            conn, intent_id=intent.intent_id, submission_status="SUBMISSION_UNKNOWN",
            broker_order_id=None, now=now, increment_attempt=True,
        )
        exec_repo.record_failure(
            conn, recommendation_id=recommendation_id, intent_id=intent.intent_id,
            stage="credentialed_submit", reason=str(exc), now=now,
        )
        return CredentialedSubmissionOutcome(
            recommendation_id=recommendation_id, status=STATUS_SUBMISSION_UNKNOWN, intent=intent,
            eligibility=eligibility, submission=exec_repo.get_submission(conn, intent.intent_id),
        )

    exec_repo.update_submission_status(
        conn, intent_id=intent.intent_id, submission_status=response["status"],
        broker_order_id=response.get("broker_order_id"), now=now, increment_attempt=True,
    )
    return CredentialedSubmissionOutcome(
        recommendation_id=recommendation_id, status=STATUS_SUBMITTED, intent=intent,
        eligibility=eligibility, submission=exec_repo.get_submission(conn, intent.intent_id),
    )
