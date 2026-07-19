from datetime import date, datetime, timezone

import pytest

from trading_research.storage.database import connect
from trading_research.strategies.research_budget import (
    ResearchTokenReservation,
    TokenBudgetError,
    TokenBudgetRejected,
    mark_research_tokens_ambiguous,
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
             maximum_output_tokens=500, maximum_reasoning_tokens=0, daily_token_cap=2000):
    return reserve_research_tokens(
        conn, research_run_id=research_run_id, symbol=symbol, provider="deterministic", model_name=None,
        utc_date=UTC_DATE, estimated_input_tokens=estimated_input_tokens,
        maximum_output_tokens=maximum_output_tokens, maximum_reasoning_tokens=maximum_reasoning_tokens,
        daily_token_cap=daily_token_cap, clock=_clock,
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
    assert settle_research_tokens(conn, reservation.reservation_id, actual_input_tokens=999, actual_output_tokens=999, clock=_clock) is False
