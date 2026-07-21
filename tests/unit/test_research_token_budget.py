from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier

import pytest

from trading_research.storage.database import connect
from trading_research.storage.shadow_operations_repositories import (
    list_budget_reservations,
    list_token_budget_reconciliations,
    load_budget_reservation,
)
from trading_research.research.models import (
    TOKEN_ACCOUNTING_NOT_APPLICABLE,
    TOKEN_ACCOUNTING_REASONING_INCLUDED_IN_OUTPUT,
    TOKEN_ACCOUNTING_REASONING_SEPARATE,
)
from trading_research.strategies.research_budget import (
    RESEARCH_TOKEN_RESERVATION_KIND,
    TOKEN_RESERVATION_AMBIGUOUS,
    TOKEN_RESERVATION_IN_FLIGHT,
    ResearchTokenReservation,
    TokenBudgetError,
    TokenBudgetRejected,
    claim_research_token_attempt,
    mark_research_tokens_ambiguous,
    migrate_legacy_token_reservations,
    reconcile_ambiguous_research_tokens,
    recover_expired_token_claims,
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


# --- A2: ambiguous retries must be blocked across UTC dates, not just the current date ---


def test_ambiguous_reservation_blocks_retry_on_a_later_utc_date(tmp_path):
    conn = _db(tmp_path)
    july_19 = reserve_research_tokens(
        conn, research_run_id="run-cross-date", symbol="AAPL", provider="deterministic", model_name=None,
        utc_date=date(2026, 7, 19), estimated_input_tokens=1000, maximum_output_tokens=500,
        maximum_reasoning_tokens=0, daily_token_cap=10_000,
        clock=lambda: datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
        token_accounting_policy=TOKEN_ACCOUNTING_NOT_APPLICABLE, research_attempt_identity="fundamental:1",
    )
    assert isinstance(july_19, ResearchTokenReservation)
    assert mark_research_tokens_ambiguous(conn, july_19.reservation_id) is True

    july_20 = reserve_research_tokens(
        conn, research_run_id="run-cross-date", symbol="AAPL", provider="deterministic", model_name=None,
        utc_date=date(2026, 7, 20), estimated_input_tokens=1000, maximum_output_tokens=500,
        maximum_reasoning_tokens=0, daily_token_cap=10_000,
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        token_accounting_policy=TOKEN_ACCOUNTING_NOT_APPLICABLE, research_attempt_identity="fundamental:1",
    )
    assert isinstance(july_20, TokenBudgetRejected)
    assert july_20.code == "TOKEN_RESERVATION_RECONCILIATION_REQUIRED"


def test_reconciled_to_released_permits_retry_on_a_later_date(tmp_path):
    conn = _db(tmp_path)
    july_19 = reserve_research_tokens(
        conn, research_run_id="run-cross-date-2", symbol="AAPL", provider="deterministic", model_name=None,
        utc_date=date(2026, 7, 19), estimated_input_tokens=1000, maximum_output_tokens=500,
        maximum_reasoning_tokens=0, daily_token_cap=10_000,
        clock=lambda: datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
        token_accounting_policy=TOKEN_ACCOUNTING_NOT_APPLICABLE, research_attempt_identity="fundamental:1",
    )
    mark_research_tokens_ambiguous(conn, july_19.reservation_id)
    reconcile_ambiguous_research_tokens(
        conn, july_19.reservation_id, target_status="RELEASED", operator="operator@example.test",
        reason="adapter log proves request was never transmitted",
        reconciliation_source="signed-adapter-log:request-absent", clock=_clock,
    )
    july_20 = reserve_research_tokens(
        conn, research_run_id="run-cross-date-2", symbol="AAPL", provider="deterministic", model_name=None,
        utc_date=date(2026, 7, 20), estimated_input_tokens=1000, maximum_output_tokens=500,
        maximum_reasoning_tokens=0, daily_token_cap=10_000,
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        token_accounting_policy=TOKEN_ACCOUNTING_NOT_APPLICABLE, research_attempt_identity="fundamental:1",
    )
    assert isinstance(july_20, ResearchTokenReservation)


# --- A5: idempotent reuse must validate the immutable reservation payload ---


def test_same_key_larger_output_allowance_fails_closed(tmp_path):
    conn = _db(tmp_path)
    _reserve(conn, research_run_id="run-payload", estimated_input_tokens=100, maximum_output_tokens=50)
    with pytest.raises(TokenBudgetError, match="TOKEN_RESERVATION_PAYLOAD_MISMATCH"):
        _reserve(conn, research_run_id="run-payload", estimated_input_tokens=100, maximum_output_tokens=999)


def test_same_key_changed_reasoning_allowance_fails_closed(tmp_path):
    conn = _db(tmp_path)
    _reserve(
        conn, research_run_id="run-payload-2", maximum_reasoning_tokens=10,
        token_accounting_policy=TOKEN_ACCOUNTING_REASONING_SEPARATE,
    )
    with pytest.raises(TokenBudgetError, match="TOKEN_RESERVATION_PAYLOAD_MISMATCH"):
        _reserve(
            conn, research_run_id="run-payload-2", maximum_reasoning_tokens=999,
            token_accounting_policy=TOKEN_ACCOUNTING_REASONING_SEPARATE,
        )


def test_same_key_identical_payload_reuses_reservation(tmp_path):
    conn = _db(tmp_path)
    first = _reserve(conn, research_run_id="run-payload-3", estimated_input_tokens=100, maximum_output_tokens=50)
    second = _reserve(conn, research_run_id="run-payload-3", estimated_input_tokens=100, maximum_output_tokens=50)
    assert isinstance(first, ResearchTokenReservation) and isinstance(second, ResearchTokenReservation)
    assert first.reservation_id == second.reservation_id


# --- A6: repeated reconciliation to the same target must validate evidence ---


def test_repeated_reconciliation_with_conflicting_evidence_fails_closed(tmp_path):
    conn = _db(tmp_path)
    reservation = _reserve(conn, token_accounting_policy=TOKEN_ACCOUNTING_REASONING_SEPARATE)
    mark_research_tokens_ambiguous(conn, reservation.reservation_id)
    reconcile_ambiguous_research_tokens(
        conn, reservation.reservation_id, target_status="SETTLED", operator="operator@example.test",
        reason="first pass", reconciliation_source="provider-usage-export:2026-07-11",
        actual_input_tokens=10, actual_output_tokens=20, actual_reasoning_tokens=5,
        token_accounting_policy=TOKEN_ACCOUNTING_REASONING_SEPARATE, provider_request_id="request-1", clock=_clock,
    )
    with pytest.raises(TokenBudgetError, match="TOKEN_RECONCILIATION_EVIDENCE_CONFLICT"):
        reconcile_ambiguous_research_tokens(
            conn, reservation.reservation_id, target_status="SETTLED", operator="operator@example.test",
            reason="second pass with different numbers", reconciliation_source="provider-usage-export:2026-07-11",
            actual_input_tokens=999, actual_output_tokens=20, actual_reasoning_tokens=5,
            token_accounting_policy=TOKEN_ACCOUNTING_REASONING_SEPARATE, provider_request_id="request-1",
            clock=_clock,
        )


def test_repeated_reconciliation_with_identical_evidence_is_idempotent(tmp_path):
    conn = _db(tmp_path)
    reservation = _reserve(conn, token_accounting_policy=TOKEN_ACCOUNTING_REASONING_SEPARATE)
    mark_research_tokens_ambiguous(conn, reservation.reservation_id)
    kwargs = dict(
        target_status="SETTLED", operator="operator@example.test", reason="matched export",
        reconciliation_source="provider-usage-export:2026-07-11", actual_input_tokens=10, actual_output_tokens=20,
        actual_reasoning_tokens=5, token_accounting_policy=TOKEN_ACCOUNTING_REASONING_SEPARATE,
        provider_request_id="request-1", clock=_clock,
    )
    assert reconcile_ambiguous_research_tokens(conn, reservation.reservation_id, **kwargs) is True
    assert reconcile_ambiguous_research_tokens(conn, reservation.reservation_id, **kwargs) is False


# --- A4: legacy pre-PR #35 token reservation migration ---


def test_legacy_token_reservation_migrates_to_research_tokens(tmp_path):
    conn = _db(tmp_path)
    legacy_key = "research_token_budget:run-legacy:AAPL:deterministic:none:attempt-1:2026-07-01"
    conn.execute(
        "INSERT INTO shadow_budget_reservations "
        "(reservation_id, idempotency_key, cycle_intent, reserved_estimated_cost_usd, reserved_input_tokens, "
        "reserved_output_tokens, reserved_latency_seconds, status, consumed_cost_usd, consumed_input_tokens, "
        "consumed_output_tokens, consumed_latency_seconds, emergency_margin_breached, reservation_kind, "
        "created_at, settled_at) "
        "VALUES ('resv-legacy-1', ?, 'deterministic', '0', 500, 300, 0, 'SETTLED', '0', 100, 90, 0, 0, "
        "'RESEARCH_COST', ?, ?)",
        (legacy_key, "2026-07-01T12:00:00+00:00", "2026-07-01T12:05:00+00:00"),
    )
    conn.commit()

    result = migrate_legacy_token_reservations(conn, clock=_clock)
    assert result.migrated_reservation_ids == ("resv-legacy-1",)
    assert result.conflicts == ()

    row = load_budget_reservation(conn, "resv-legacy-1")
    assert row["reservation_kind"] == RESEARCH_TOKEN_RESERVATION_KIND
    assert row["budget_date"] == "2026-07-01"
    assert row["token_accounting_policy"] == TOKEN_ACCOUNTING_REASONING_INCLUDED_IN_OUTPUT
    assert row["reserved_output_tokens"] == 300  # combined allowance preserved exactly
    assert row["reserved_reasoning_tokens"] == 0


def test_migration_is_idempotent(tmp_path):
    conn = _db(tmp_path)
    legacy_key = "research_token_budget:run-legacy-2:AAPL:deterministic:none:attempt-1:2026-07-01"
    conn.execute(
        "INSERT INTO shadow_budget_reservations "
        "(reservation_id, idempotency_key, cycle_intent, reserved_estimated_cost_usd, reserved_input_tokens, "
        "reserved_output_tokens, reserved_latency_seconds, status, consumed_cost_usd, consumed_input_tokens, "
        "consumed_output_tokens, consumed_latency_seconds, emergency_margin_breached, reservation_kind, "
        "created_at, settled_at) "
        "VALUES ('resv-legacy-2', ?, 'deterministic', '0', 500, 300, 0, 'RESERVED', '0', 0, 0, 0, 0, "
        "'RESEARCH_COST', ?, NULL)",
        (legacy_key, "2026-07-01T12:00:00+00:00"),
    )
    conn.commit()

    first = migrate_legacy_token_reservations(conn, clock=_clock)
    assert first.migrated_reservation_ids == ("resv-legacy-2",)
    second = migrate_legacy_token_reservations(conn, clock=_clock)
    assert second.migrated_reservation_ids == ()
    assert second.already_migrated_reservation_ids == ("resv-legacy-2",)
    assert second.conflicts == ()


def test_migration_leaves_cost_reservations_untouched(tmp_path):
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO shadow_budget_reservations "
        "(reservation_id, idempotency_key, cycle_intent, reserved_estimated_cost_usd, reserved_input_tokens, "
        "reserved_output_tokens, reserved_latency_seconds, status, consumed_cost_usd, consumed_input_tokens, "
        "consumed_output_tokens, consumed_latency_seconds, emergency_margin_breached, reservation_kind, "
        "created_at, settled_at) "
        "VALUES ('resv-cost-1', 'cost:run-x:AAPL', 'deterministic', '1.50', 0, 0, 0, 'SETTLED', '1.50', 0, 0, 0, "
        "0, 'RESEARCH_COST', ?, ?)",
        ("2026-07-01T12:00:00+00:00", "2026-07-01T12:05:00+00:00"),
    )
    conn.commit()
    result = migrate_legacy_token_reservations(conn, clock=_clock)
    assert result.migrated_reservation_ids == ()
    row = load_budget_reservation(conn, "resv-cost-1")
    assert row["reservation_kind"] == "RESEARCH_COST"
    assert row["budget_date"] is None


# --- Milestone 27 A1: fenced provider-attempt claim ------------------------------


def test_claim_transitions_reserved_to_in_flight(tmp_path):
    conn = _db(tmp_path)
    reservation = _reserve(conn)
    claim = claim_research_token_attempt(conn, reservation.reservation_id, "owner-1", _clock(), _clock())
    assert claim.status == "CLAIMED"
    assert claim.claim_generation == 1
    row = load_budget_reservation(conn, reservation.reservation_id)
    assert row["status"] == TOKEN_RESERVATION_IN_FLIGHT
    assert row["claim_owner"] == "owner-1"


def test_second_claim_on_in_flight_reservation_is_rejected(tmp_path):
    conn = _db(tmp_path)
    reservation = _reserve(conn)
    first = claim_research_token_attempt(conn, reservation.reservation_id, "owner-1", _clock(), _clock())
    assert first.status == "CLAIMED"
    second = claim_research_token_attempt(conn, reservation.reservation_id, "owner-2", _clock(), _clock())
    assert second.status == "ALREADY_IN_FLIGHT"


def test_claim_on_settled_reservation_is_a_state_conflict(tmp_path):
    conn = _db(tmp_path)
    reservation = _reserve(conn)
    claim = claim_research_token_attempt(conn, reservation.reservation_id, "owner-1", _clock(), _clock())
    settle_research_tokens(
        conn, reservation.reservation_id, actual_input_tokens=1, actual_output_tokens=1, clock=_clock,
        claim_owner="owner-1", claim_generation=claim.claim_generation,
    )
    stale = claim_research_token_attempt(conn, reservation.reservation_id, "owner-2", _clock(), _clock())
    assert stale.status == "STATE_CONFLICT"


def test_two_concurrent_claims_on_the_same_reservation_have_exactly_one_winner(tmp_path):
    """One reservation must authorize exactly one provider call
    (docs/milestones/27.md A1/A3): both workers reuse the same idempotency
    key/reservation, but only one may win the fenced RESERVED -> IN_FLIGHT
    claim."""
    db_path = tmp_path / "claim-race.sqlite3"
    setup_conn = connect(db_path)
    reservation = _reserve(setup_conn)
    setup_conn.close()

    def claim_as(owner):
        conn = connect(db_path)
        try:
            return claim_research_token_attempt(conn, reservation.reservation_id, owner, _clock(), _clock())
        finally:
            conn.close()

    barrier = Barrier(2)

    def run(owner):
        conn = connect(db_path)
        barrier.wait()
        try:
            return claim_research_token_attempt(conn, reservation.reservation_id, owner, _clock(), _clock())
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result() for future in (
            pool.submit(run, "owner-a"), pool.submit(run, "owner-b"),
        )]
    assert sum(outcome.status == "CLAIMED" for outcome in outcomes) == 1
    assert sum(outcome.status == "ALREADY_IN_FLIGHT" for outcome in outcomes) == 1

    final_conn = connect(db_path)
    assert len(list_budget_reservations(final_conn)) == 1


def test_expired_claim_recovers_to_ambiguous_not_auto_retried(tmp_path):
    conn = _db(tmp_path)
    reservation = _reserve(conn)
    claimed_at = _clock()
    expires_at = claimed_at + timedelta(seconds=1)
    claim_research_token_attempt(conn, reservation.reservation_id, "owner-1", claimed_at, expires_at)

    after_expiry = expires_at + timedelta(seconds=1)
    recovered = recover_expired_token_claims(conn, now=after_expiry)
    assert recovered == (reservation.reservation_id,)
    row = load_budget_reservation(conn, reservation.reservation_id)
    assert row["status"] == TOKEN_RESERVATION_AMBIGUOUS

    # Idempotent: nothing left to recover on a second pass.
    assert recover_expired_token_claims(conn, now=after_expiry) == ()


def test_stale_claim_owner_cannot_settle_after_expiry_recovery(tmp_path):
    """docs/milestones/27.md A3 "Stale worker fencing": worker A claims
    generation 1; the claim expires; recovery moves the reservation to
    AMBIGUOUS; worker A's later settlement attempt is rejected and the
    reservation remains AMBIGUOUS."""
    conn = _db(tmp_path)
    reservation = _reserve(conn)
    claimed_at = _clock()
    expires_at = claimed_at + timedelta(seconds=1)
    claim = claim_research_token_attempt(conn, reservation.reservation_id, "owner-a", claimed_at, expires_at)

    recover_expired_token_claims(conn, now=expires_at + timedelta(seconds=1))
    row = load_budget_reservation(conn, reservation.reservation_id)
    assert row["status"] == TOKEN_RESERVATION_AMBIGUOUS

    with pytest.raises(TokenBudgetError) as excinfo:
        settle_research_tokens(
            conn, reservation.reservation_id, actual_input_tokens=1, actual_output_tokens=1, clock=_clock,
            claim_owner="owner-a", claim_generation=claim.claim_generation,
        )
    assert excinfo.value.code == "TOKEN_RESERVATION_STATE_CONFLICT"
    row_after = load_budget_reservation(conn, reservation.reservation_id)
    assert row_after["status"] == TOKEN_RESERVATION_AMBIGUOUS


# --- Milestone 27 A2: automatic legacy token migration at startup ----------------


def test_startup_migration_runs_automatically_without_manual_call(tmp_path):
    db_path = tmp_path / "startup-legacy.sqlite3"
    conn = connect(db_path)
    legacy_key = "research_token_budget:run-startup:AAPL:deterministic:none:attempt-1:2026-07-01"
    conn.execute(
        "INSERT INTO shadow_budget_reservations "
        "(reservation_id, idempotency_key, cycle_intent, reserved_estimated_cost_usd, reserved_input_tokens, "
        "reserved_output_tokens, reserved_latency_seconds, status, consumed_cost_usd, consumed_input_tokens, "
        "consumed_output_tokens, consumed_latency_seconds, emergency_margin_breached, reservation_kind, "
        "created_at, settled_at) "
        "VALUES ('resv-startup-legacy', ?, 'deterministic', '0', 500, 300, 0, 'SETTLED', '0', 100, 90, 0, 0, "
        "'RESEARCH_COST', ?, ?)",
        (legacy_key, "2026-07-01T12:00:00+00:00", "2026-07-01T12:05:00+00:00"),
    )
    conn.commit()
    # Simulate a database that predates Milestone 27's migration 12 (the
    # legacy row already existed; migration 12 has never run against it).
    conn.execute("DELETE FROM schema_version WHERE version = 12")
    conn.commit()
    conn.close()

    reopened = connect(db_path)
    row = load_budget_reservation(reopened, "resv-startup-legacy")
    assert row["reservation_kind"] == RESEARCH_TOKEN_RESERVATION_KIND
    assert row["budget_date"] == "2026-07-01"
    live_reserved = _live_tokens_reserved_for_date_via_reserve(reopened)
    assert live_reserved > 0  # counts against the daily cap without a manual migration call
    reopened.close()

    # Reopening again must not rewrite or double-count the already-migrated row.
    reopened_again = connect(db_path)
    row_again = load_budget_reservation(reopened_again, "resv-startup-legacy")
    assert row_again["reservation_kind"] == RESEARCH_TOKEN_RESERVATION_KIND
    assert row_again["budget_date"] == "2026-07-01"
    assert len(list_budget_reservations(reopened_again)) == 1


def _live_tokens_reserved_for_date_via_reserve(conn) -> int:
    """A settled RESEARCH_TOKENS row still counts against the daily cap --
    prove it the same way `reserve_research_tokens` would, by attempting a
    reservation on the migrated row's own budget date (2026-07-01) that
    would overshoot unless the migrated row is counted."""
    probe_clock = lambda: datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    rejected = reserve_research_tokens(
        conn, research_run_id="run-cap-probe", symbol="MSFT", provider="deterministic", model_name=None,
        estimated_input_tokens=700, maximum_output_tokens=0, maximum_reasoning_tokens=0,
        daily_token_cap=800, clock=probe_clock, research_attempt_identity="attempt-1",
    )
    assert isinstance(rejected, TokenBudgetRejected)
    return rejected.remaining_tokens


def test_startup_migration_conflict_blocks_connect_with_no_partial_rows(tmp_path):
    db_path = tmp_path / "startup-legacy-conflict.sqlite3"
    conn = connect(db_path)
    good_key = "research_token_budget:run-good:AAPL:deterministic:none:attempt-1:2026-07-01"
    bad_key = "research_token_budget:run-bad:AAPL:deterministic:none:attempt-1:not-a-date"
    conn.execute(
        "INSERT INTO shadow_budget_reservations "
        "(reservation_id, idempotency_key, cycle_intent, reserved_estimated_cost_usd, reserved_input_tokens, "
        "reserved_output_tokens, reserved_latency_seconds, status, consumed_cost_usd, consumed_input_tokens, "
        "consumed_output_tokens, consumed_latency_seconds, emergency_margin_breached, reservation_kind, "
        "created_at, settled_at) "
        "VALUES ('resv-good', ?, 'deterministic', '0', 500, 300, 0, 'SETTLED', '0', 100, 90, 0, 0, "
        "'RESEARCH_COST', ?, ?)",
        (good_key, "2026-07-01T12:00:00+00:00", "2026-07-01T12:05:00+00:00"),
    )
    conn.execute(
        "INSERT INTO shadow_budget_reservations "
        "(reservation_id, idempotency_key, cycle_intent, reserved_estimated_cost_usd, reserved_input_tokens, "
        "reserved_output_tokens, reserved_latency_seconds, status, consumed_cost_usd, consumed_input_tokens, "
        "consumed_output_tokens, consumed_latency_seconds, emergency_margin_breached, reservation_kind, "
        "created_at, settled_at) "
        "VALUES ('resv-bad', ?, 'deterministic', '0', 500, 300, 0, 'SETTLED', '0', 100, 90, 0, 0, "
        "'RESEARCH_COST', ?, NULL)",
        # Naive (non-tz-aware) created_at: `_legacy_budget_date` cannot derive a
        # budget date from it, and the key's own date suffix ("not-a-date") is
        # unparseable too, so this row is an unresolvable migration conflict.
        (bad_key, "2026-07-01T12:00:00"),
    )
    conn.commit()
    conn.execute("DELETE FROM schema_version WHERE version = 12")
    conn.commit()
    conn.close()

    with pytest.raises(TokenBudgetError) as excinfo:
        connect(db_path)
    assert excinfo.value.code == "TOKEN_MIGRATION_CONFLICT"

    import sqlite3
    raw = sqlite3.connect(db_path)
    raw.row_factory = sqlite3.Row
    good_row = raw.execute("SELECT * FROM shadow_budget_reservations WHERE reservation_id = 'resv-good'").fetchone()
    assert good_row["reservation_kind"] == "RESEARCH_COST"  # rolled back, not partially migrated
    raw.close()
