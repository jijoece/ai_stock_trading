"""Milestone 11.3.1 Item 8 Part C: persistent, multi-cycle health hysteresis."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_research.shadow import health_hysteresis as hh
from trading_research.shadow import pause as pause_mod
from trading_research.shadow.health import (
    STATUS_DEGRADED, STATUS_HEALTHY, STATUS_PAUSE_RECOMMENDED, STATUS_PAUSE_REQUIRED,
)
from trading_research.storage.database import connect

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


def _clock(offset_seconds=0):
    t = NOW + timedelta(seconds=offset_seconds)
    return lambda: t


def _config(**overrides):
    kwargs = dict(
        warning_after_n_failures=1, pause_recommended_after_n_failures=2, pause_required_after_m_failures=3,
        recovery_streak=2,
    )
    kwargs.update(overrides)
    return hh.PersistentHealthPolicyConfig(**kwargs)


# --- 1. one failing qualified cycle does not immediately pause when hysteresis requires more


def test_single_failing_cycle_does_not_immediately_require_pause(conn):
    config = _config(pause_recommended_after_n_failures=3, pause_required_after_m_failures=5)
    decision = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c1", cycle_status=STATUS_PAUSE_REQUIRED, qualified=True, config=config, clock=_clock(),
    )
    assert decision.decision == STATUS_DEGRADED
    assert decision.consecutive_failures == 1


# --- 2. consecutive failures cross warning and pause thresholds --------------


def test_consecutive_failures_cross_all_thresholds(conn):
    config = _config()
    d1 = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c1", cycle_status=STATUS_PAUSE_RECOMMENDED, qualified=True, config=config, clock=_clock(1),
    )
    assert d1.decision == STATUS_DEGRADED
    d2 = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c2", cycle_status=STATUS_PAUSE_RECOMMENDED, qualified=True, config=config, clock=_clock(2),
    )
    assert d2.decision == STATUS_PAUSE_RECOMMENDED
    d3 = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c3", cycle_status=STATUS_PAUSE_RECOMMENDED, qualified=True, config=config, clock=_clock(3),
    )
    assert d3.decision == STATUS_PAUSE_REQUIRED
    assert d3.consecutive_failures == 3


# --- 3. intermittent success resets the failure streak -----------------------


def test_intermittent_success_resets_failure_streak(conn):
    config = _config()
    hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c1", cycle_status=STATUS_PAUSE_RECOMMENDED, qualified=True, config=config, clock=_clock(1),
    )
    hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c2", cycle_status=STATUS_PAUSE_RECOMMENDED, qualified=True, config=config, clock=_clock(2),
    )
    healthy = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c3", cycle_status=STATUS_HEALTHY, qualified=True, config=config, clock=_clock(3),
    )
    assert healthy.consecutive_failures == 0
    assert healthy.consecutive_recoveries == 1


# --- 4. recovery requires the configured healthy streak -----------------------


def test_recovery_requires_configured_healthy_streak(conn):
    config = _config(recovery_streak=2)
    hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c1", cycle_status=STATUS_PAUSE_REQUIRED, qualified=True, config=config, clock=_clock(1),
    )
    hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c2", cycle_status=STATUS_PAUSE_REQUIRED, qualified=True, config=config, clock=_clock(2),
    )
    still_bad = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c3", cycle_status=STATUS_HEALTHY, qualified=True, config=config, clock=_clock(3),
    )
    assert still_bad.decision != STATUS_HEALTHY  # only 1 of 2 required healthy cycles so far
    recovered = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c4", cycle_status=STATUS_HEALTHY, qualified=True, config=config, clock=_clock(4),
    )
    assert recovered.decision == STATUS_HEALTHY
    assert recovered.consecutive_recoveries == 2


# --- 5. insufficient-data cycles do not fabricate recovery --------------------


def test_insufficient_data_cycles_do_not_fabricate_recovery(conn):
    config = _config()
    hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c1", cycle_status=STATUS_PAUSE_REQUIRED, qualified=True, config=config, clock=_clock(1),
    )
    unqualified = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c2", cycle_status=STATUS_HEALTHY, qualified=False, config=config, clock=_clock(2),
    )
    assert unqualified.decision == STATUS_DEGRADED  # unchanged from after c1 (1 failure -> warning tier)
    assert unqualified.consecutive_recoveries == 0
    assert unqualified.consecutive_failures == 1


# --- 6. process restart preserves hysteresis state ----------------------------


def test_process_restart_preserves_hysteresis_state(tmp_path):
    db_path = tmp_path / "hysteresis.sqlite3"
    config = _config()
    conn1 = connect(db_path)
    hh.evaluate_and_persist_hysteresis(
        conn1, cycle_id="c1", cycle_status=STATUS_PAUSE_RECOMMENDED, qualified=True, config=config, clock=_clock(1),
    )
    conn1.close()

    conn2 = connect(db_path)  # fresh connection -- simulated restart
    resumed = hh.evaluate_and_persist_hysteresis(
        conn2, cycle_id="c2", cycle_status=STATUS_PAUSE_RECOMMENDED, qualified=True, config=config, clock=_clock(2),
    )
    assert resumed.consecutive_failures == 2  # continues from the persisted count, not from zero
    conn2.close()


# --- 7. repeated evaluation of the same cycle is idempotent -------------------


def test_repeated_evaluation_of_same_cycle_is_idempotent(conn):
    config = _config()
    first = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c1", cycle_status=STATUS_PAUSE_RECOMMENDED, qualified=True, config=config, clock=_clock(1),
    )
    second = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c1", cycle_status=STATUS_PAUSE_RECOMMENDED, qualified=True, config=config, clock=_clock(2),
    )
    assert second.idempotent_replay is True
    assert second.consecutive_failures == first.consecutive_failures == 1


# --- 8. manual pause and kill states are never automatically cleared ----------


def test_manual_pause_state_is_never_automatically_cleared(conn):
    config = _config(recovery_streak=1)
    pause_mod.request_pause(conn, "operator paused for maintenance", pause_mod.SOURCE_OPERATOR, clock=_clock())
    healthy = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c1", cycle_status=STATUS_HEALTHY, qualified=True, config=config, clock=_clock(1),
    )
    assert healthy.decision == STATUS_PAUSE_REQUIRED


def test_killed_state_is_never_automatically_cleared(conn):
    config = _config(recovery_streak=1)
    pause_mod.kill(conn, "manual kill", "operator-alice", clock=_clock())
    healthy = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c1", cycle_status=STATUS_HEALTHY, qualified=True, config=config, clock=_clock(1),
    )
    assert healthy.decision == STATUS_PAUSE_REQUIRED


# --- 9. configuration changes produce a new policy/hash boundary --------------


def test_configuration_change_resets_streak_as_a_policy_boundary(conn):
    config_a = _config(pause_recommended_after_n_failures=2)
    hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c1", cycle_status=STATUS_PAUSE_RECOMMENDED, qualified=True, config=config_a, clock=_clock(1),
    )
    hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c2", cycle_status=STATUS_PAUSE_RECOMMENDED, qualified=True, config=config_a, clock=_clock(2),
    )
    config_b = _config(pause_recommended_after_n_failures=5, pause_required_after_m_failures=6)  # different policy -> different hash
    reset = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c3", cycle_status=STATUS_PAUSE_RECOMMENDED, qualified=True, config=config_b, clock=_clock(3),
    )
    assert reset.policy_reset is True
    assert reset.consecutive_failures == 1  # streak restarted under the new policy


# --- severe error bypasses ordinary counting and immediately requires pause ---


def test_severe_error_bypasses_ordinary_counting(conn):
    config = _config(pause_recommended_after_n_failures=5, pause_required_after_m_failures=10)
    decision = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="c1", cycle_status=STATUS_DEGRADED, qualified=True, severe_error=True,
        config=config, clock=_clock(),
    )
    assert decision.decision == STATUS_PAUSE_REQUIRED


# --- policy validation ---------------------------------------------------------


def test_policy_rejects_out_of_order_thresholds():
    with pytest.raises(hh.HysteresisPolicyError):
        hh.PersistentHealthPolicyConfig(
            warning_after_n_failures=5, pause_recommended_after_n_failures=2, pause_required_after_m_failures=10,
            recovery_streak=1,
        )


def test_policy_rejects_non_positive_threshold():
    with pytest.raises(hh.HysteresisPolicyError):
        hh.PersistentHealthPolicyConfig(
            warning_after_n_failures=0, pause_recommended_after_n_failures=2, pause_required_after_m_failures=3,
            recovery_streak=1,
        )


# --- Milestone 12.1.1 Item 4: scheduler-run identity, not research_cycle_id, is the
# --- hysteresis evaluation/idempotency key.


def test_two_scheduler_runs_for_the_same_research_cycle_each_advance_the_streak(conn):
    """Required tests #1-2: run A and run B share the same deterministic
    `research_cycle_id` but are distinct `scheduler_run_id`s — each must
    independently advance the failure streak, not collide as one replay."""
    config = _config()
    decision_a = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="sched-run-A", research_cycle_id="research-cycle-1",
        cycle_status=STATUS_PAUSE_RECOMMENDED, qualified=True, config=config, clock=_clock(1),
    )
    decision_b = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="sched-run-B", research_cycle_id="research-cycle-1",
        cycle_status=STATUS_PAUSE_RECOMMENDED, qualified=True, config=config, clock=_clock(2),
    )
    assert decision_a.idempotent_replay is False
    assert decision_b.idempotent_replay is False
    assert decision_a.consecutive_failures == 1
    assert decision_b.consecutive_failures == 2


def test_replaying_the_same_scheduler_run_does_not_double_count(conn):
    """Required test #3."""
    config = _config()
    hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="sched-run-A", research_cycle_id="research-cycle-1",
        cycle_status=STATUS_PAUSE_RECOMMENDED, qualified=True, config=config, clock=_clock(1),
    )
    replay = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="sched-run-A", research_cycle_id="research-cycle-1",
        cycle_status=STATUS_PAUSE_RECOMMENDED, qualified=True, config=config, clock=_clock(2),
    )
    assert replay.idempotent_replay is True
    assert replay.consecutive_failures == 1


def test_later_successful_scheduler_run_advances_recovery(conn):
    """Required test #4."""
    config = _config()
    hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="sched-run-A", research_cycle_id="research-cycle-1",
        cycle_status=STATUS_PAUSE_REQUIRED, qualified=True, config=config, clock=_clock(1),
    )
    recovery = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="sched-run-B", research_cycle_id="research-cycle-2",
        cycle_status=STATUS_HEALTHY, qualified=True, config=config, clock=_clock(2),
    )
    assert recovery.consecutive_recoveries == 1
    assert recovery.consecutive_failures == 0


def test_manual_and_scheduled_evaluations_cannot_collide(conn):
    """Required test #5: a manual re-evaluation of the same research cycle
    (its own distinct scheduler_run_id/evaluation identity) does not collide
    with the scheduled run's own evaluation."""
    config = _config()
    scheduled = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="sched-run-A", research_cycle_id="research-cycle-1",
        cycle_status=STATUS_PAUSE_RECOMMENDED, qualified=True, config=config, clock=_clock(1),
    )
    manual = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="manual-eval-1", research_cycle_id="research-cycle-1",
        cycle_status=STATUS_PAUSE_RECOMMENDED, qualified=True, config=config, clock=_clock(2),
    )
    assert scheduled.idempotent_replay is False
    assert manual.idempotent_replay is False
    assert manual.consecutive_failures == 2


def test_research_cycle_id_persisted_for_reporting_only(conn):
    from trading_research.storage import shadow_alerts_repositories as repo

    config = _config()
    hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="sched-run-A", research_cycle_id="research-cycle-1",
        cycle_status=STATUS_HEALTHY, qualified=True, config=config, clock=_clock(1),
    )
    row = repo.load_health_hysteresis_evaluation(
        conn, scope=hh.DEFAULT_SCOPE, cycle_id="sched-run-A",
        policy_hash=hh.evaluate_and_persist_hysteresis(
            conn, cycle_id="sched-run-A", research_cycle_id="research-cycle-1",
            cycle_status=STATUS_HEALTHY, qualified=True, config=config, clock=_clock(1),
        ).policy_hash,
    )
    assert row["cycle_id"] == "sched-run-A"
    assert row["research_cycle_id"] == "research-cycle-1"


def test_migration_preserves_pr19_schema_history(tmp_path):
    """Required test #6: a database created under the pre-Milestone-12.1.1
    (PR #19) schema — no `research_cycle_id` column — must migrate additively,
    keep its historical evaluation rows readable, and accept new evaluations
    afterward."""
    import sqlite3

    from trading_research.storage.database import connect as connect_full
    from trading_research.storage.shadow_alerts_schema import SHADOW_ALERTS_DDL, apply_shadow_alerts_schema

    db_path = tmp_path / "pr19.db"
    legacy_ddl = SHADOW_ALERTS_DDL.replace(
        "    research_cycle_id TEXT,\n    UNIQUE(scope, cycle_id, policy_hash)",
        "    UNIQUE(scope, cycle_id, policy_hash)",
    )
    assert "research_cycle_id" not in legacy_ddl
    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.executescript(legacy_ddl)
    legacy_conn.execute(
        "INSERT INTO shadow_health_hysteresis_evaluations "
        "(evaluation_id, scope, cycle_id, policy_version, policy_hash, single_cycle_status, "
        "previous_hysteresis_status, new_hysteresis_status, effective_status, qualified, sample_size, "
        "minimum_sample_size, aggregate_success_rate, required_categories_json, required_providers_json, "
        "observed_providers_json, missing_required_providers_json, missing_required_categories_json, "
        "per_provider_metrics_json, severe_error_categories_json, consecutive_failures_before, "
        "consecutive_failures_after, consecutive_recoveries_before, consecutive_recoveries_after, "
        "reasons_json, evaluated_at) VALUES "
        "('eval-legacy', 'default', 'research-cycle-legacy', 'persistent_health/v1', 'hash-legacy', 'PASS', "
        "'HEALTHY', 'HEALTHY', 'HEALTHY', 1, 5, 1, 1.0, '[]', '[]', '[]', '[]', '[]', '{}', '[]', 0, 0, 0, 0, "
        "'[]', '2026-01-01T00:00:00+00:00')",
    )
    legacy_conn.commit()
    legacy_conn.close()

    conn = connect_full(db_path)
    row = conn.execute(
        "SELECT cycle_id, research_cycle_id FROM shadow_health_hysteresis_evaluations WHERE evaluation_id = 'eval-legacy'"
    ).fetchone()
    assert row["cycle_id"] == "research-cycle-legacy"
    assert row["research_cycle_id"] is None  # additive column, absent on historical rows

    config = _config()
    decision = hh.evaluate_and_persist_hysteresis(
        conn, cycle_id="sched-run-new", research_cycle_id="research-cycle-new",
        cycle_status=STATUS_HEALTHY, qualified=True, config=config, clock=_clock(1),
    )
    assert decision.idempotent_replay is False
    conn.close()
