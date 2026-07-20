from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier

import pytest

from trading_research.storage.database import connect
from trading_research.storage.shadow_operations_repositories import (
    list_token_budget_reconciliations,
    load_budget_reservation,
)
from trading_research.research.models import (
    TOKEN_ACCOUNTING_NOT_APPLICABLE,
    TOKEN_ACCOUNTING_REASONING_INCLUDED_IN_OUTPUT,
    TOKEN_ACCOUNTING_REASONING_SEPARATE,
)
from trading_research.strategies.research_budget import (
    ResearchTokenReservation,
    TokenBudgetError,
    TokenBudgetRejected,
    mark_research_tokens_ambiguous,
    reconcile_ambiguous_research_tokens,
    release_research_tokens,
    reserve_research_tokens,
    settle_research_tokens,
)

UTC_DATE = date(2026, 7, 11)


def _clock():
    return datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    return connect(tmp_path / "budget.sqlite3")


def _reserve(conn, *, symbol="AAPL", research_run_id="run-1", estimated_input_tokens=1000,
             maximum_output_tokens=500, maximum_reasoning_tokens=0, daily_token_cap=2000,
             token_accounting_policy=TOKEN_ACCOUNTING_NOT_APPLICABLE,
             research_attempt_identity="attempt-1", utc_date=UTC_DATE):
    return reserve_research_tokens(
        conn, research_run_id=research_run_id, symbol=symbol, provider="deterministic", model_name=None,
        utc_date=utc_date, estimated_input_tokens=estimated_input_tokens,
        maximum_output_tokens=maximum_output_tokens, maximum_reasoning_tokens=maximum_reasoning_tokens,
        daily_token_cap=daily_token_cap, clock=_clock, token_accounting_policy=token_accounting_policy,
        research_attempt_identity=research_attempt_identity,
    )


def test_reservation_prevents_projected_token_overshoot(tmp_path):
    conn = _db(tmp_path)
    ok = _reserve(conn, symbol="AAPL", daily_token_cap=2000)
    assert isinstance(ok, ResearchTokenReservation)
    rejected = _reserve(conn, symbol="MSFT", daily_token_cap=2000)
    assert isinstance(rejected, TokenBudgetRejected)
    assert rejected.remaining_tokens < rejected.requested_tokens


def test_two_concurrent_reservations_cannot_exceed_the_cap(tmp_path):
    conn = _db(tmp_path)
    first = _reserve(conn, symbol="AAPL", estimated_input_tokens=900, maximum_output_tokens=100, daily_token_cap=2000)
    second = _reserve(conn, symbol="MSFT", estimated_input_tokens=900, maximum_output_tokens=100, daily_token_cap=2000)
    third = _reserve(conn, symbol="GOOG", estimated_input_tokens=900, maximum_output_tokens=100, daily_token_cap=2000)
    assert isinstance(first, ResearchTokenReservation)
    assert isinstance(second, ResearchTokenReservation)
    assert isinstance(third, TokenBudgetRejected)


def test_restart_preserves_spent_and_reserved_totals(tmp_path):
    db_path = tmp_path / "restart.sqlite3"
    conn = connect(db_path)
    _reserve(conn, symbol="AAPL", estimated_input_tokens=1800, maximum_output_tokens=100, daily_token_cap=2000)
    conn.close()

    reopened = connect(db_path)
    rejected = _reserve(reopened, symbol="MSFT", estimated_input_tokens=200, maximum_output_tokens=0, daily_token_cap=2000)
    assert isinstance(rejected, TokenBudgetRejected)


def test_research_reuse_consumes_zero_new_reservation(tmp_path):
    conn = _db(tmp_path)
    first = _reserve(conn, research_run_id="run-1", symbol="AAPL", daily_token_cap=3000)
    duplicate = _reserve(conn, research_run_id="run-1", symbol="AAPL", daily_token_cap=3000)
    assert duplicate.reservation_id == first.reservation_id
    # A third distinct symbol still has room — proves the duplicate call
    # above did not double-count against the cap.
    third = _reserve(conn, research_run_id="run-1", symbol="MSFT", daily_token_cap=3000)
    assert isinstance(third, ResearchTokenReservation)


def test_settlement_uses_actual_tokens(tmp_path):
    conn = _db(tmp_path)
    reservation = _reserve(conn, symbol="AAPL", estimated_input_tokens=1000, maximum_output_tokens=900, daily_token_cap=2000)
    settled = settle_research_tokens(
        conn, reservation.reservation_id, actual_input_tokens=100, actual_output_tokens=50, clock=_clock,
    )
    assert settled is True
    # The unused reserved portion (1900 - 150) is released, so a new
    # reservation that would have overshot the original reserved amount
    # now fits.
    second = _reserve(conn, symbol="MSFT", estimated_input_tokens=1000, maximum_output_tokens=800, daily_token_cap=2000)
    assert isinstance(second, ResearchTokenReservation)


def test_ambiguous_call_retains_reservation(tmp_path):
    conn = _db(tmp_path)
    reservation = _reserve(conn, symbol="AAPL", estimated_input_tokens=1900, maximum_output_tokens=0, daily_token_cap=2000)
    assert mark_research_tokens_ambiguous(conn, reservation.reservation_id) is True
    still_blocked = _reserve(conn, symbol="MSFT", estimated_input_tokens=200, maximum_output_tokens=0, daily_token_cap=2000)
    assert isinstance(still_blocked, TokenBudgetRejected)


def test_released_reservation_frees_its_tokens(tmp_path):
    conn = _db(tmp_path)
    reservation = _reserve(conn, symbol="AAPL", estimated_input_tokens=1900, maximum_output_tokens=0, daily_token_cap=2000)
    assert release_research_tokens(conn, reservation.reservation_id, clock=_clock) is True
    now_fits = _reserve(conn, symbol="MSFT", estimated_input_tokens=1900, maximum_output_tokens=0, daily_token_cap=2000)
    assert isinstance(now_fits, ResearchTokenReservation)


def test_settling_an_unknown_reservation_fails_closed(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(TokenBudgetError):
        settle_research_tokens(conn, "resv-tokens-does-not-exist", actual_input_tokens=1, actual_output_tokens=1, clock=_clock)


def test_double_settlement_is_a_no_op(tmp_path):
    conn = _db(tmp_path)
    reservation = _reserve(conn, symbol="AAPL", daily_token_cap=2000)
    assert settle_research_tokens(conn, reservation.reservation_id, actual_input_tokens=10, actual_output_tokens=10, clock=_clock) is True
    assert settle_research_tokens(conn, reservation.reservation_id, actual_input_tokens=10, actual_output_tokens=10, clock=_clock) is False
    with pytest.raises(TokenBudgetError, match="TOKEN_RESERVATION_STATE_CONFLICT"):
        settle_research_tokens(
            conn, reservation.reservation_id, actual_input_tokens=999, actual_output_tokens=999, clock=_clock,
        )


def test_utc_date_is_derived_from_clock_and_mismatch_fails_closed(tmp_path):
    conn = _db(tmp_path)
    local_clock = lambda: datetime(2026, 7, 10, 18, 0, tzinfo=timezone(timedelta(hours=-7)))
    reservation = reserve_research_tokens(
        conn, research_run_id="run-date", symbol="AAPL", provider="scripted", model_name="m",
        estimated_input_tokens=1, maximum_output_tokens=1, maximum_reasoning_tokens=0,
        daily_token_cap=10, clock=local_clock,
    )
    row = load_budget_reservation(conn, reservation.reservation_id)
    assert row["budget_date"] == "2026-07-11"
    assert reservation.idempotency_key.endswith("2026-07-11")

    with pytest.raises(TokenBudgetError, match="TOKEN_BUDGET_DATE_MISMATCH"):
        reserve_research_tokens(
            conn, research_run_id="run-bad-date", symbol="MSFT", provider="scripted", model_name="m",
            utc_date=date(2026, 7, 10), estimated_input_tokens=1, maximum_output_tokens=1,
            maximum_reasoning_tokens=0, daily_token_cap=10, clock=local_clock,
        )


def test_reasoning_included_in_output_is_not_double_counted(tmp_path):
    conn = _db(tmp_path)
    first = _reserve(
        conn, estimated_input_tokens=1000, maximum_output_tokens=500, maximum_reasoning_tokens=500,
        daily_token_cap=2000, token_accounting_policy=TOKEN_ACCOUNTING_REASONING_INCLUDED_IN_OUTPUT,
    )
    assert isinstance(first, ResearchTokenReservation)
    second = _reserve(
        conn, symbol="MSFT", estimated_input_tokens=400, maximum_output_tokens=100,
        maximum_reasoning_tokens=100, daily_token_cap=2000,
        token_accounting_policy=TOKEN_ACCOUNTING_REASONING_INCLUDED_IN_OUTPUT,
    )
    assert isinstance(second, ResearchTokenReservation)


def test_reasoning_separate_is_counted_independently(tmp_path):
    conn = _db(tmp_path)
    first = _reserve(
        conn, estimated_input_tokens=1000, maximum_output_tokens=500, maximum_reasoning_tokens=500,
        daily_token_cap=2000, token_accounting_policy=TOKEN_ACCOUNTING_REASONING_SEPARATE,
    )
    assert isinstance(first, ResearchTokenReservation)
    rejected = _reserve(
        conn, symbol="MSFT", estimated_input_tokens=1, maximum_output_tokens=0,
        maximum_reasoning_tokens=0, daily_token_cap=2000,
        token_accounting_policy=TOKEN_ACCOUNTING_REASONING_SEPARATE,
    )
    assert isinstance(rejected, TokenBudgetRejected)


def test_settlement_persists_separate_reasoning_usage(tmp_path):
    conn = _db(tmp_path)
    reservation = _reserve(
        conn, token_accounting_policy=TOKEN_ACCOUNTING_REASONING_SEPARATE,
        maximum_reasoning_tokens=250, daily_token_cap=3000,
    )
    settle_research_tokens(
        conn, reservation.reservation_id, actual_input_tokens=100, actual_output_tokens=50,
        actual_reasoning_tokens=25, token_accounting_policy=TOKEN_ACCOUNTING_REASONING_SEPARATE,
        provider_request_id="request-1", clock=_clock,
    )
    row = load_budget_reservation(conn, reservation.reservation_id)
    assert row["reserved_reasoning_tokens"] == 250
    assert row["consumed_reasoning_tokens"] == 25
    assert row["token_accounting_policy"] == TOKEN_ACCOUNTING_REASONING_SEPARATE
    assert row["provider_request_id"] == "request-1"


def test_usage_above_reservation_is_persisted_and_blocks_date(tmp_path):
    conn = _db(tmp_path)
    reservation = _reserve(
        conn, estimated_input_tokens=50, maximum_output_tokens=50, daily_token_cap=1000,
    )
    settle_research_tokens(
        conn, reservation.reservation_id, actual_input_tokens=100, actual_output_tokens=100, clock=_clock,
    )
    row = load_budget_reservation(conn, reservation.reservation_id)
    assert row["consumed_input_tokens"] == 100
    assert row["consumed_output_tokens"] == 100
    assert row["emergency_margin_breached"] == 1
    blocked = _reserve(conn, symbol="MSFT", estimated_input_tokens=1, maximum_output_tokens=0, daily_token_cap=1000)
    assert isinstance(blocked, TokenBudgetRejected)
    assert "breach" in blocked.reason


def test_operator_can_reconcile_ambiguous_to_settled(tmp_path):
    conn = _db(tmp_path)
    reservation = _reserve(conn, token_accounting_policy=TOKEN_ACCOUNTING_REASONING_SEPARATE)
    mark_research_tokens_ambiguous(conn, reservation.reservation_id)
    assert reconcile_ambiguous_research_tokens(
        conn, reservation.reservation_id, target_status="SETTLED", operator="operator@example.test",
        reason="matched provider usage export", reconciliation_source="provider-usage-export:2026-07-11",
        actual_input_tokens=10, actual_output_tokens=20, actual_reasoning_tokens=5,
        token_accounting_policy=TOKEN_ACCOUNTING_REASONING_SEPARATE,
        provider_request_id="provider-request-1", clock=_clock,
    ) is True
    row = load_budget_reservation(conn, reservation.reservation_id)
    assert row["status"] == "SETTLED"
    assert row["consumed_reasoning_tokens"] == 5
    audit = list_token_budget_reconciliations(conn, reservation_id=reservation.reservation_id)
    assert [(entry["from_status"], entry["to_status"]) for entry in audit] == [("AMBIGUOUS", "SETTLED")]


def test_operator_can_reconcile_ambiguous_to_released(tmp_path):
    conn = _db(tmp_path)
    reservation = _reserve(conn)
    mark_research_tokens_ambiguous(conn, reservation.reservation_id)
    assert reconcile_ambiguous_research_tokens(
        conn, reservation.reservation_id, target_status="RELEASED", operator="operator@example.test",
        reason="adapter log proves request was never transmitted",
        reconciliation_source="signed-adapter-log:request-absent", clock=_clock,
    ) is True
    assert load_budget_reservation(conn, reservation.reservation_id)["status"] == "RELEASED"


def test_ambiguous_attempt_blocks_a_new_automatic_attempt(tmp_path):
    conn = _db(tmp_path)
    reservation = _reserve(conn, research_attempt_identity="fundamental:1")
    mark_research_tokens_ambiguous(conn, reservation.reservation_id)
    blocked = _reserve(conn, research_attempt_identity="fundamental:2")
    assert isinstance(blocked, TokenBudgetRejected)
    assert blocked.code == "TOKEN_RESERVATION_RECONCILIATION_REQUIRED"


def _race(db_path, reservation_id, left, right):
    barrier = Barrier(2)

    def run(operation):
        conn = connect(db_path)
        barrier.wait()
        try:
            return ("ok", operation(conn, reservation_id))
        except TokenBudgetError as exc:
            return ("error", exc.code)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        return [future.result() for future in (pool.submit(run, left), pool.submit(run, right))]


def test_concurrent_settle_versus_release_has_one_winner(tmp_path):
    db_path = tmp_path / "transition-race.sqlite3"
    conn = connect(db_path)
    reservation = _reserve(conn)
    conn.close()
    outcomes = _race(
        db_path, reservation.reservation_id,
        lambda c, rid: settle_research_tokens(c, rid, actual_input_tokens=1, actual_output_tokens=1, clock=_clock),
        lambda c, rid: release_research_tokens(c, rid, _clock),
    )
    assert sum(outcome == ("ok", True) for outcome in outcomes) == 1
    assert ("error", "TOKEN_RESERVATION_STATE_CONFLICT") in outcomes


def test_concurrent_settle_versus_ambiguous_has_one_winner(tmp_path):
    db_path = tmp_path / "ambiguous-race.sqlite3"
    conn = connect(db_path)
    reservation = _reserve(conn)
    conn.close()
    outcomes = _race(
        db_path, reservation.reservation_id,
        lambda c, rid: settle_research_tokens(c, rid, actual_input_tokens=1, actual_output_tokens=1, clock=_clock),
        lambda c, rid: mark_research_tokens_ambiguous(c, rid),
    )
    assert sum(outcome == ("ok", True) for outcome in outcomes) == 1
    assert ("error", "TOKEN_RESERVATION_STATE_CONFLICT") in outcomes


def test_two_connections_cannot_reserve_past_daily_cap(tmp_path):
    db_path = tmp_path / "reservation-race.sqlite3"
    connect(db_path).close()
    barrier = Barrier(2)

    def reserve_symbol(symbol):
        conn = connect(db_path)
        barrier.wait()
        try:
            return _reserve(
                conn, symbol=symbol, estimated_input_tokens=700, maximum_output_tokens=0,
                daily_token_cap=1000,
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result() for future in (
            pool.submit(reserve_symbol, "AAPL"), pool.submit(reserve_symbol, "MSFT"),
        )]
    assert sum(isinstance(outcome, ResearchTokenReservation) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, TokenBudgetRejected) for outcome in outcomes) == 1
