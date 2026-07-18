"""Milestone 12.1 Item 5: dimension-specific health/hysteresis. An
insufficient evidence-provider sample must never suppress a genuinely
FAILing retry-exhaustion or unsupported-claim rate for the same cycle —
each rate-based dimension is evaluated (and its own persistent hysteresis
streak advanced) completely independently, reusing the existing per-`scope`
hysteresis engine in `health_hysteresis.py`."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_research.shadow import health as health_mod
from trading_research.shadow import health_hysteresis as hh
from trading_research.storage import shadow_alerts_repositories as repo
from trading_research.storage.database import connect

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


def _clock(offset_seconds=0):
    t = NOW + timedelta(seconds=offset_seconds)
    return lambda: t


def _check(check_name, status):
    return health_mod.HealthCheckResult(
        check_name=check_name, status=status, input_value=None, input_unit="fraction",
        threshold_value="0.500", threshold_unit="fraction", comparison=">", applicable=True,
        pause_flag_enabled=True, reason="test fixture",
    )


def _config(**overrides):
    kwargs = dict(
        warning_after_n_failures=1, pause_recommended_after_n_failures=2, pause_required_after_m_failures=3,
        recovery_streak=2,
    )
    kwargs.update(overrides)
    return hh.PersistentHealthPolicyConfig(**kwargs)


def _dimension_result(conn, *, scope, check, cycle_id, clock, config=None):
    return hh.evaluate_and_persist_hysteresis(
        conn, scope=scope, cycle_id=cycle_id,
        cycle_status=health_mod.dimension_cycle_status(check),
        qualified=health_mod.dimension_is_qualified(check),
        config=config or _config(), clock=clock,
    )


def test_insufficient_evidence_provider_sample_does_not_suppress_retry_exhaustion(conn):
    """Required tests #1-3: evidence-provider INSUFFICIENT_DATA plus a
    FAILing retry-exhaustion check in the same cycle — retry-exhaustion's
    hysteresis must advance while evidence-provider's does not."""
    evidence_check = _check(health_mod.CHECK_NAME_PROVIDER_FAILURE_RATE, health_mod.CHECK_STATUS_INSUFFICIENT_DATA)
    retry_check = _check(health_mod.CHECK_NAME_RETRY_EXHAUSTION_RATE, health_mod.CHECK_STATUS_FAIL)

    evidence_decision = _dimension_result(
        conn, scope=hh.DEFAULT_SCOPE, check=evidence_check, cycle_id="cycle-1", clock=_clock(),
    )
    retry_decision = _dimension_result(
        conn, scope=f"{hh.DEFAULT_SCOPE}:{health_mod.DIMENSION_RETRY_EXHAUSTION}", check=retry_check,
        cycle_id="cycle-1", clock=_clock(),
    )

    assert evidence_decision.consecutive_failures == 0  # unqualified — never counted
    assert evidence_decision.qualified_cycle_count == 0
    assert retry_decision.consecutive_failures == 1  # counted independently
    assert retry_decision.qualified_cycle_count == 1


def test_unsupported_claim_failures_advance_their_own_streak(conn):
    """Required test #4."""
    unsupported_check = _check(health_mod.CHECK_NAME_UNSUPPORTED_CLAIM_RATE, health_mod.CHECK_STATUS_FAIL)
    decision = _dimension_result(
        conn, scope=f"{hh.DEFAULT_SCOPE}:{health_mod.DIMENSION_UNSUPPORTED_CLAIMS}", check=unsupported_check,
        cycle_id="cycle-1", clock=_clock(),
    )
    assert decision.consecutive_failures == 1


def test_healthy_evidence_provider_does_not_clear_retry_exhaustion_streak(conn):
    """Required test #5 (adapted: this repository has no separately
    hysteresis-tracked MODEL_PROVIDER_FAILURE dimension, so this proves
    cross-dimension independence between the two dimensions that ARE
    tracked): a healthy evidence-provider cycle must not reset an
    independently-accumulating retry-exhaustion failure streak."""
    config = _config(pause_recommended_after_n_failures=5, pause_required_after_m_failures=9)
    retry_scope = f"{hh.DEFAULT_SCOPE}:{health_mod.DIMENSION_RETRY_EXHAUSTION}"
    fail_check = _check(health_mod.CHECK_NAME_RETRY_EXHAUSTION_RATE, health_mod.CHECK_STATUS_FAIL)
    _dimension_result(conn, scope=retry_scope, check=fail_check, cycle_id="cycle-1", clock=_clock(1), config=config)
    retry_after_two_failures = _dimension_result(
        conn, scope=retry_scope, check=fail_check, cycle_id="cycle-2", clock=_clock(2), config=config,
    )
    assert retry_after_two_failures.consecutive_failures == 2

    # A healthy evidence-provider cycle at the SAME cycle boundary — must not
    # touch the retry-exhaustion scope's already-accumulated streak at all.
    healthy_evidence_check = _check(health_mod.CHECK_NAME_PROVIDER_FAILURE_RATE, health_mod.CHECK_STATUS_PASS)
    _dimension_result(
        conn, scope=hh.DEFAULT_SCOPE, check=healthy_evidence_check, cycle_id="cycle-3", clock=_clock(3), config=config,
    )
    retry_state = repo.load_health_hysteresis_state(conn, retry_scope)
    assert retry_state["consecutive_failures"] == 2  # untouched by the evidence-provider scope's own call


def test_recovery_is_dimension_specific(conn):
    """Required test #6."""
    config = _config(pause_recommended_after_n_failures=2, pause_required_after_m_failures=3, recovery_streak=1)
    retry_scope = f"{hh.DEFAULT_SCOPE}:{health_mod.DIMENSION_RETRY_EXHAUSTION}"
    fail_check = _check(health_mod.CHECK_NAME_RETRY_EXHAUSTION_RATE, health_mod.CHECK_STATUS_FAIL)
    pass_check = _check(health_mod.CHECK_NAME_RETRY_EXHAUSTION_RATE, health_mod.CHECK_STATUS_PASS)
    _dimension_result(conn, scope=retry_scope, check=fail_check, cycle_id="c1", clock=_clock(1), config=config)
    _dimension_result(conn, scope=retry_scope, check=fail_check, cycle_id="c2", clock=_clock(2), config=config)
    recovered = _dimension_result(conn, scope=retry_scope, check=pass_check, cycle_id="c3", clock=_clock(3), config=config)
    assert recovered.decision == health_mod.STATUS_HEALTHY
    assert recovered.consecutive_recoveries == 1


def test_overall_status_reflects_worst_dimension():
    """Required test #7."""
    worst = health_mod.worst_health_status((
        health_mod.STATUS_HEALTHY, health_mod.STATUS_PAUSE_REQUIRED, health_mod.STATUS_DEGRADED,
    ))
    assert worst == health_mod.STATUS_PAUSE_REQUIRED


def test_repeated_cycle_evaluation_is_idempotent_per_dimension(conn):
    """Required test #9."""
    retry_scope = f"{hh.DEFAULT_SCOPE}:{health_mod.DIMENSION_RETRY_EXHAUSTION}"
    fail_check = _check(health_mod.CHECK_NAME_RETRY_EXHAUSTION_RATE, health_mod.CHECK_STATUS_FAIL)
    first = _dimension_result(conn, scope=retry_scope, check=fail_check, cycle_id="c1", clock=_clock(1))
    second = _dimension_result(conn, scope=retry_scope, check=fail_check, cycle_id="c1", clock=_clock(2))
    assert second.idempotent_replay is True
    assert second.consecutive_failures == first.consecutive_failures == 1


def test_history_can_explain_which_dimension_caused_the_pause(conn):
    """Required test #10: distinct evaluation history per scope."""
    evidence_check = _check(health_mod.CHECK_NAME_PROVIDER_FAILURE_RATE, health_mod.CHECK_STATUS_PASS)
    retry_check = _check(health_mod.CHECK_NAME_RETRY_EXHAUSTION_RATE, health_mod.CHECK_STATUS_FAIL)
    _dimension_result(conn, scope=hh.DEFAULT_SCOPE, check=evidence_check, cycle_id="c1", clock=_clock(1))
    retry_scope = f"{hh.DEFAULT_SCOPE}:{health_mod.DIMENSION_RETRY_EXHAUSTION}"
    _dimension_result(conn, scope=retry_scope, check=retry_check, cycle_id="c1", clock=_clock(1))

    evidence_history = repo.load_latest_health_hysteresis_evaluation(conn, scope=hh.DEFAULT_SCOPE)
    retry_history = repo.load_latest_health_hysteresis_evaluation(conn, scope=retry_scope)
    assert evidence_history["single_cycle_status"] == health_mod.STATUS_HEALTHY
    assert retry_history["single_cycle_status"] == health_mod.STATUS_PAUSE_REQUIRED


def test_pass_warning_fail_are_all_qualified():
    """Milestone 12.1.1 Item 3, required test #1."""
    for status in (health_mod.CHECK_STATUS_PASS, health_mod.CHECK_STATUS_WARNING, health_mod.CHECK_STATUS_FAIL):
        assert health_mod.dimension_is_qualified(_check(health_mod.CHECK_NAME_RETRY_EXHAUSTION_RATE, status)) is True


def test_insufficient_data_is_unqualified():
    """Milestone 12.1.1 Item 3, required test #2."""
    check = _check(health_mod.CHECK_NAME_RETRY_EXHAUSTION_RATE, health_mod.CHECK_STATUS_INSUFFICIENT_DATA)
    assert health_mod.dimension_is_qualified(check) is False


def test_not_applicable_is_unqualified():
    """Milestone 12.1.1 Item 3, required test #3: the bug this closes — a
    generic `!= INSUFFICIENT_DATA` qualification helper treated
    `NOT_APPLICABLE` (e.g. a fixture-only/deterministic cycle) as qualified."""
    check = _check(health_mod.CHECK_NAME_RETRY_EXHAUSTION_RATE, health_mod.CHECK_STATUS_NOT_APPLICABLE)
    assert health_mod.dimension_is_qualified(check) is False


def test_repeated_fixture_cycles_do_not_advance_recovery(conn):
    """Milestone 12.1.1 Item 3, required test #4: a real failure streak
    followed only by NOT_APPLICABLE fixture cycles must never recover."""
    scope = f"{hh.DEFAULT_SCOPE}:{health_mod.DIMENSION_RETRY_EXHAUSTION}"
    fail_check = _check(health_mod.CHECK_NAME_RETRY_EXHAUSTION_RATE, health_mod.CHECK_STATUS_FAIL)
    _dimension_result(conn, scope=scope, check=fail_check, cycle_id="cycle-1", clock=_clock(0))
    _dimension_result(conn, scope=scope, check=fail_check, cycle_id="cycle-2", clock=_clock(1))
    failing_decision = _dimension_result(conn, scope=scope, check=fail_check, cycle_id="cycle-3", clock=_clock(2))
    assert failing_decision.decision == hh.STATUS_PAUSE_REQUIRED

    na_check = _check(health_mod.CHECK_NAME_RETRY_EXHAUSTION_RATE, health_mod.CHECK_STATUS_NOT_APPLICABLE)
    for i, offset in enumerate((3, 4, 5)):
        na_decision = _dimension_result(conn, scope=scope, check=na_check, cycle_id=f"na-{i}", clock=_clock(offset))
        assert na_decision.decision == hh.STATUS_PAUSE_REQUIRED
        assert na_decision.consecutive_recoveries == 0


def test_fixture_cycles_do_not_advance_failure(conn):
    """Milestone 12.1.1 Item 3, required test #5."""
    scope = f"{hh.DEFAULT_SCOPE}:{health_mod.DIMENSION_RETRY_EXHAUSTION}"
    na_check = _check(health_mod.CHECK_NAME_RETRY_EXHAUSTION_RATE, health_mod.CHECK_STATUS_NOT_APPLICABLE)
    decision = None
    for i, offset in enumerate((0, 1, 2, 3)):
        decision = _dimension_result(conn, scope=scope, check=na_check, cycle_id=f"na-{i}", clock=_clock(offset))
    assert decision.decision == hh.STATUS_HEALTHY
    assert decision.consecutive_failures == 0


def test_not_applicable_idempotent_replay_is_unchanged(conn):
    """Milestone 12.1.1 Item 3, required test #6."""
    scope = f"{hh.DEFAULT_SCOPE}:{health_mod.DIMENSION_RETRY_EXHAUSTION}"
    na_check = _check(health_mod.CHECK_NAME_RETRY_EXHAUSTION_RATE, health_mod.CHECK_STATUS_NOT_APPLICABLE)
    first = _dimension_result(conn, scope=scope, check=na_check, cycle_id="na-replay", clock=_clock(0))
    second = _dimension_result(conn, scope=scope, check=na_check, cycle_id="na-replay", clock=_clock(0))
    assert second.idempotent_replay is True
    assert second.consecutive_failures == first.consecutive_failures
    assert second.consecutive_recoveries == first.consecutive_recoveries
