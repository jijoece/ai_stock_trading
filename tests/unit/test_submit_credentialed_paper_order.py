from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_research.execution.config import load_execution_config
from trading_research.execution.eligibility import PaperExecutionEligibilityPolicy
from trading_research.runtime.client.process_client import RuntimeClient
from trading_research.services.submit_credentialed_paper_order import (
    STATUS_REJECTED_INELIGIBLE,
    STATUS_RESUMED,
    STATUS_RUNTIME_UNAVAILABLE,
    STATUS_SUBMISSION_UNKNOWN,
    STATUS_SUBMITTED,
    submit_credentialed_paper_order,
)
from trading_research.storage import execution_repositories as exec_repo
from trading_research.storage.database import connect
from trading_research.universe.tickers import default_universe

from tests.support.execution_fixtures import buy_candidate_payload, insert_recommendation_row
from tests.support.runtime_client_fixtures import FakeTransport, fake_transport_factory, start_ready_client

NOW = datetime(2026, 7, 11, 15, 0, tzinfo=timezone.utc)
CONFIG = load_execution_config()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "credentialed.sqlite3")
    yield c
    c.close()


@pytest.fixture
def policy():
    return PaperExecutionEligibilityPolicy(universe=default_universe(), config=CONFIG)


def _started_client():
    fake = FakeTransport()
    client = RuntimeClient(
        command=["python3", "-m", "trading_paper_runtime"],
        transport_factory=fake_transport_factory(fake),
        startup_timeout_seconds=1.0, request_timeout_seconds=1.0,
    )
    start_ready_client(client, fake)
    return client, fake


def _order_snapshot(intent_id, *, status="ACCEPTED", filled_quantity=0, average_fill_price=None, broker_order_id="alp-1"):
    return {
        "intent_id": intent_id, "client_order_id": intent_id, "broker_order_id": broker_order_id,
        "status": status, "raw_broker_status": status.lower(), "quantity": 70,
        "filled_quantity": filled_quantity, "average_fill_price": average_fill_price,
        "submitted_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
    }


def test_ineligible_recommendation_never_contacts_runtime(conn, policy):
    payload = buy_candidate_payload(rec_id="rec-1", symbol="SOFI", status="expired", now=NOW)
    insert_recommendation_row(conn, payload)
    client, fake = _started_client()
    writes_before = len(fake.written_lines)

    outcome = submit_credentialed_paper_order(
        "rec-1", conn=conn, execution_config=CONFIG, eligibility_policy=policy, client=client,
        git_sha="abc1234", clock=lambda: NOW,
    )

    assert outcome.status == STATUS_REJECTED_INELIGIBLE
    assert len(fake.written_lines) == writes_before  # no runtime call at all


def test_fresh_submission_full_flow(conn, policy):
    payload = buy_candidate_payload(rec_id="rec-3", symbol="SOFI", now=NOW)
    insert_recommendation_row(conn, payload)
    client, fake = _started_client()

    fake.queue_failure("UNKNOWN_ORDER", "not found")  # initial get_order lookup

    from trading_research.execution.intent_builder import build_paper_order_intent

    intent = build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")
    fake.queue_success(_order_snapshot(intent.intent_id, status="ACCEPTED"))  # submit_order response

    outcome = submit_credentialed_paper_order(
        "rec-3", conn=conn, execution_config=CONFIG, eligibility_policy=policy, client=client,
        git_sha="abc1234", clock=lambda: NOW,
    )

    assert outcome.status == STATUS_SUBMITTED
    assert outcome.submission.submission_status == "ACCEPTED"
    assert outcome.submission.broker_order_id == "alp-1"
    assert outcome.submission.attempt_count == 1


def test_repeated_call_after_terminal_result_is_resumed_without_runtime_call(conn, policy):
    payload = buy_candidate_payload(rec_id="rec-4", symbol="SOFI", now=NOW)
    insert_recommendation_row(conn, payload)
    client, fake = _started_client()

    from trading_research.execution.intent_builder import build_paper_order_intent

    intent = build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")
    fake.queue_failure("UNKNOWN_ORDER", "not found")
    fake.queue_success(_order_snapshot(intent.intent_id, status="FILLED", filled_quantity=70, average_fill_price="14.92"))

    first = submit_credentialed_paper_order(
        "rec-4", conn=conn, execution_config=CONFIG, eligibility_policy=policy, client=client,
        git_sha="abc1234", clock=lambda: NOW,
    )
    assert first.status == STATUS_SUBMITTED
    assert first.submission.submission_status == "FILLED"

    writes_before = len(fake.written_lines)
    second = submit_credentialed_paper_order(
        "rec-4", conn=conn, execution_config=CONFIG, eligibility_policy=policy, client=client,
        git_sha="abc1234", clock=lambda: NOW + timedelta(seconds=5),
    )
    assert second.status == STATUS_RESUMED
    assert len(fake.written_lines) == writes_before  # no new runtime call


def test_restart_recovery_finds_existing_broker_order_without_resubmitting(conn, policy):
    """Simulates a process crash after PENDING_SUBMISSION was persisted but
    before the broker's acknowledgement was recorded — the broker already
    has the order under this client_order_id (docs/milestone-4.md Step 8:
    'support process restart without duplicating orders')."""
    payload = buy_candidate_payload(rec_id="rec-5", symbol="SOFI", now=NOW)
    insert_recommendation_row(conn, payload)

    from trading_research.execution.intent_builder import build_paper_order_intent

    intent = build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")
    exec_repo.save_intent(conn, intent, now=NOW)
    exec_repo.create_pending_submission(conn, intent_id=intent.intent_id, client_order_id=intent.intent_id, now=NOW)

    client, fake = _started_client()
    writes_before = len(fake.written_lines)
    # The broker already knows about this order from a prior (interrupted) attempt.
    fake.queue_success(_order_snapshot(intent.intent_id, status="ACCEPTED"))

    outcome = submit_credentialed_paper_order(
        "rec-5", conn=conn, execution_config=CONFIG, eligibility_policy=policy, client=client,
        git_sha="abc1234", clock=lambda: NOW,
    )

    assert outcome.status == STATUS_SUBMITTED
    # Only one runtime call (the lookup) — submit_order was never called.
    assert len(fake.written_lines) == writes_before + 1


def test_ambiguous_submission_recovers_via_lookup_not_blind_retry(conn, policy):
    payload = buy_candidate_payload(rec_id="rec-6", symbol="SOFI", now=NOW)
    insert_recommendation_row(conn, payload)

    from trading_research.execution.intent_builder import build_paper_order_intent

    intent = build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")
    client, fake = _started_client()

    fake.queue_failure("UNKNOWN_ORDER", "not found")  # initial lookup: nothing yet
    fake.queue_timeout()  # submit_order times out — ambiguous
    fake.queue_success(_order_snapshot(intent.intent_id, status="ACCEPTED"))  # recovery lookup finds it

    outcome = submit_credentialed_paper_order(
        "rec-6", conn=conn, execution_config=CONFIG, eligibility_policy=policy, client=client,
        git_sha="abc1234", clock=lambda: NOW,
    )

    assert outcome.status == STATUS_SUBMITTED
    assert outcome.submission.submission_status == "ACCEPTED"


def test_submission_unknown_when_recovery_lookup_also_fails(conn, policy):
    payload = buy_candidate_payload(rec_id="rec-7", symbol="SOFI", now=NOW)
    insert_recommendation_row(conn, payload)
    client, fake = _started_client()

    fake.queue_failure("UNKNOWN_ORDER", "not found")  # initial lookup
    fake.queue_timeout()  # submit_order times out
    fake.queue_timeout()  # recovery lookup also fails

    outcome = submit_credentialed_paper_order(
        "rec-7", conn=conn, execution_config=CONFIG, eligibility_policy=policy, client=client,
        git_sha="abc1234", clock=lambda: NOW,
    )

    assert outcome.status == STATUS_SUBMISSION_UNKNOWN
    assert outcome.submission.submission_status == "SUBMISSION_UNKNOWN"

    # Milestone 11.2 Part 22: the recovery lookup's own failure must be
    # persisted as bounded evidence, not silently discarded.
    rows = conn.execute(
        "SELECT stage FROM paper_execution_failures WHERE intent_id = ? ORDER BY occurred_at", (outcome.intent.intent_id,)
    ).fetchall()
    stages = [row["stage"] for row in rows]
    assert "credentialed_recovery_lookup" in stages
    assert "credentialed_submit" in stages


def test_runtime_unavailable_during_initial_lookup(conn, policy):
    payload = buy_candidate_payload(rec_id="rec-8", symbol="SOFI", now=NOW)
    insert_recommendation_row(conn, payload)
    client, fake = _started_client()

    fake.queue_eof()  # runtime process died mid-request

    outcome = submit_credentialed_paper_order(
        "rec-8", conn=conn, execution_config=CONFIG, eligibility_policy=policy, client=client,
        git_sha="abc1234", clock=lambda: NOW,
    )

    assert outcome.status == STATUS_RUNTIME_UNAVAILABLE
    assert outcome.submission.submission_status == "SUBMISSION_UNKNOWN"
