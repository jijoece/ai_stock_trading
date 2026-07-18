"""Milestone 11.2 Part 11/36 regression: `scope_sequence`, not `created_at`,
must be the authority for which external-order event is "current" — a
clock regression must never cause an earlier event to be selected as
current."""
from __future__ import annotations

from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect


def _conn():
    c = connect(":memory:")
    c.execute(
        "INSERT INTO paper_books (book_id, experiment_arm, starting_cash_usd, created_at, config_hash) "
        "VALUES ('BASELINE', 'BASELINE', '100000', '2026-01-01T00:00:00Z', 'cfg')"
    )
    c.commit()
    return c


def _event(book_id, intent_id, client_order_id, *, sequence, created_at, new_state):
    return {
        "external_order_event_id": f"evt-{sequence}", "external_order_scope_id": "scope-1",
        "book_id": book_id, "paper_order_intent_id": intent_id, "client_order_id": client_order_id,
        "broker_order_id": None, "account_fingerprint": "fp-1", "previous_state": "NOT_SUBMITTED",
        "new_state": new_state, "payload_hash": "hash-1", "quantity": "10", "limit_price": "100",
        "operator": "alice", "reason": "test", "runtime_request_id": None, "error_code": None,
        "created_at": created_at, "policy_version": "v1", "config_hash": "cfg", "attempt_number": 0,
        "scope_sequence": sequence,
    }


def test_backward_clock_does_not_select_the_earlier_event_as_current():
    conn = _conn()
    # Event 0 is inserted with a *later* created_at than event 1 (simulating
    # a clock that regressed between the two writes) — scope_sequence still
    # correctly identifies event 1 as the true successor.
    repo.save_external_order_event(conn, _event(
        "BASELINE", "intent-1", "client-1", sequence=0,
        created_at="2026-07-15T20:05:00Z", new_state="PREVIEWED",
    ))
    repo.save_external_order_event(conn, _event(
        "BASELINE", "intent-1", "client-1", sequence=1,
        created_at="2026-07-15T20:00:00Z", new_state="SUBMISSION_REQUESTED",
    ))
    current = repo.load_latest_external_order_event(conn, "BASELINE", "client-1")
    assert current["scope_sequence"] == 1
    assert current["new_state"] == "SUBMISSION_REQUESTED"

    by_intent = repo.load_latest_external_order_event_for_intent(conn, "BASELINE", "intent-1")
    assert by_intent["scope_sequence"] == 1


def test_mixed_offset_timestamps_do_not_affect_sequence_ordering():
    conn = _conn()
    # created_at values use different (but both valid) UTC-offset spellings
    # for what is otherwise a normal forward sequence.
    repo.save_external_order_event(conn, _event(
        "BASELINE", "intent-2", "client-2", sequence=0,
        created_at="2026-07-15T20:00:00+00:00", new_state="PREVIEWED",
    ))
    repo.save_external_order_event(conn, _event(
        "BASELINE", "intent-2", "client-2", sequence=1,
        created_at="2026-07-15T20:00:01Z", new_state="SUBMISSION_REQUESTED",
    ))
    repo.save_external_order_event(conn, _event(
        "BASELINE", "intent-2", "client-2", sequence=2,
        created_at="2026-07-15T20:00:02+00:00", new_state="SUBMITTED",
    ))
    current = repo.load_latest_external_order_event(conn, "BASELINE", "client-2")
    assert current["scope_sequence"] == 2
    assert current["new_state"] == "SUBMITTED"


def test_legacy_null_sequence_row_never_shadows_a_sequenced_row():
    """A pre-Milestone-11.2 event row with NULL scope_sequence must never be
    treated as current once any sequenced row exists for the same chain."""
    conn = _conn()
    legacy = _event(
        "BASELINE", "intent-3", "client-3", sequence=99,
        created_at="2026-07-15T23:59:59Z", new_state="PREVIEWED",
    )
    legacy["scope_sequence"] = None
    repo.save_external_order_event(conn, legacy)
    repo.save_external_order_event(conn, _event(
        "BASELINE", "intent-3", "client-3", sequence=0,
        created_at="2026-07-15T20:00:00Z", new_state="SUBMISSION_REQUESTED",
    ))
    current = repo.load_latest_external_order_event(conn, "BASELINE", "client-3")
    assert current["new_state"] == "SUBMISSION_REQUESTED"
