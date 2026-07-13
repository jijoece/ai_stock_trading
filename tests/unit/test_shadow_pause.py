"""Tests for shadow/pause.py (docs/milestone-7.md Step 15, Step 27 section H)."""
from __future__ import annotations

import ast
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_research.shadow import pause
from trading_research.shadow.pause import (
    PauseStateError,
    current_state,
    force_clear_kill,
    history,
    kill,
    request_pause,
    resume,
)
from trading_research.storage.database import connect
from trading_research.storage.shadow_operations_repositories import list_operator_actions

BASE_TIME = datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc)


def _clock_at(t: datetime):
    return lambda: t


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "shadow_pause_test.db")
        yield c
        c.close()


# --- active (default) ---------------------------------------------------------


def test_default_state_is_active(conn):
    state = current_state(conn)
    assert state.state == pause.STATE_ACTIVE
    assert state.is_blocking is False


# --- manual pause --------------------------------------------------------------


def test_request_manual_pause(conn):
    result = request_pause(conn, "operator maintenance", pause.SOURCE_OPERATOR, clock=_clock_at(BASE_TIME))
    assert result.state == pause.STATE_PAUSED_MANUAL
    assert result.source == pause.SOURCE_OPERATOR
    assert current_state(conn).state == pause.STATE_PAUSED_MANUAL
    assert current_state(conn).is_blocking is True


def test_pause_requires_non_empty_reason(conn):
    with pytest.raises(PauseStateError):
        request_pause(conn, "", pause.SOURCE_OPERATOR, clock=_clock_at(BASE_TIME))


# --- automatic pause (source field) --------------------------------------------


def test_automatic_pause_records_source(conn):
    result = request_pause(
        conn, "provider failure rate exceeded threshold", pause.SOURCE_AUTOMATIC_HEALTH_RULE,
        target_state=pause.STATE_PAUSED_PROVIDER_HEALTH, clock=_clock_at(BASE_TIME),
    )
    assert result.state == pause.STATE_PAUSED_PROVIDER_HEALTH
    assert result.source == pause.SOURCE_AUTOMATIC_HEALTH_RULE


def test_unknown_source_fails_closed(conn):
    with pytest.raises(PauseStateError):
        request_pause(conn, "reason", "SOME_OTHER_SOURCE", clock=_clock_at(BASE_TIME))


def test_unknown_target_state_fails_closed(conn):
    with pytest.raises(PauseStateError):
        request_pause(conn, "reason", pause.SOURCE_OPERATOR, target_state="NOT_A_STATE", clock=_clock_at(BASE_TIME))


def test_cannot_request_active_as_a_pause_target(conn):
    with pytest.raises(PauseStateError):
        request_pause(conn, "reason", pause.SOURCE_OPERATOR, target_state=pause.STATE_ACTIVE, clock=_clock_at(BASE_TIME))


# --- killed ---------------------------------------------------------------------


def test_kill_sets_killed_state(conn):
    result = kill(conn, "critical safety issue", "jijo", clock=_clock_at(BASE_TIME))
    assert result.state == pause.STATE_KILLED
    assert current_state(conn).is_killed is True


def test_kill_requires_operator(conn):
    with pytest.raises(PauseStateError):
        kill(conn, "reason", "", clock=_clock_at(BASE_TIME))


def test_kill_records_operator_action(conn):
    kill(conn, "critical safety issue", "jijo", clock=_clock_at(BASE_TIME))
    actions = list_operator_actions(conn, action_type="KILL_ACTIVATED")
    assert len(actions) == 1
    assert actions[0]["operator"] == "jijo"
    assert actions[0]["reason"] == "critical safety issue"


# --- resume ----------------------------------------------------------------------


def test_resume_from_manual_pause(conn):
    request_pause(conn, "maintenance", pause.SOURCE_OPERATOR, clock=_clock_at(BASE_TIME))
    result = resume(conn, "maintenance complete", "jijo", clock=_clock_at(BASE_TIME + timedelta(minutes=30)))
    assert result.state == pause.STATE_ACTIVE
    assert current_state(conn).state == pause.STATE_ACTIVE


def test_resume_requires_operator(conn):
    request_pause(conn, "maintenance", pause.SOURCE_OPERATOR, clock=_clock_at(BASE_TIME))
    with pytest.raises(PauseStateError):
        resume(conn, "reason", "", clock=_clock_at(BASE_TIME))


def test_resume_when_already_active_raises(conn):
    with pytest.raises(PauseStateError):
        resume(conn, "reason", "jijo", clock=_clock_at(BASE_TIME))


# --- killed cannot silently resume (hard boundary) ------------------------------


def test_resume_fails_while_killed(conn):
    kill(conn, "critical safety issue", "jijo", clock=_clock_at(BASE_TIME))
    with pytest.raises(PauseStateError):
        resume(conn, "trying to sneak back in", "jijo", clock=_clock_at(BASE_TIME + timedelta(minutes=1)))
    # State must remain KILLED after the failed resume attempt.
    assert current_state(conn).state == pause.STATE_KILLED


def test_request_pause_fails_while_killed(conn):
    kill(conn, "critical safety issue", "jijo", clock=_clock_at(BASE_TIME))
    with pytest.raises(PauseStateError):
        request_pause(conn, "reason", pause.SOURCE_OPERATOR, clock=_clock_at(BASE_TIME + timedelta(minutes=1)))


# --- override / force_clear_kill audit ------------------------------------------


def test_force_clear_kill_requires_reason_and_operator(conn):
    kill(conn, "critical safety issue", "jijo", clock=_clock_at(BASE_TIME))
    with pytest.raises(PauseStateError):
        force_clear_kill(conn, "", "jijo", clock=_clock_at(BASE_TIME + timedelta(minutes=1)))
    with pytest.raises(PauseStateError):
        force_clear_kill(conn, "reason", "", clock=_clock_at(BASE_TIME + timedelta(minutes=1)))


def test_force_clear_kill_only_valid_when_killed(conn):
    with pytest.raises(PauseStateError):
        force_clear_kill(conn, "reason", "jijo", clock=_clock_at(BASE_TIME))


def test_force_clear_kill_clears_and_records_audit(conn):
    kill(conn, "critical safety issue", "jijo", clock=_clock_at(BASE_TIME))
    result = force_clear_kill(
        conn, "root cause fixed, verified safe", "jijo", clock=_clock_at(BASE_TIME + timedelta(hours=1)),
    )
    assert result.state == pause.STATE_ACTIVE
    actions = list_operator_actions(conn, action_type="KILL_FORCE_CLEARED")
    assert len(actions) == 1
    assert actions[0]["reason"] == "root cause fixed, verified safe"
    assert actions[0]["operator"] == "jijo"


def test_resume_records_operator_action(conn):
    request_pause(conn, "maintenance", pause.SOURCE_OPERATOR, clock=_clock_at(BASE_TIME))
    resume(conn, "maintenance complete", "jijo", clock=_clock_at(BASE_TIME + timedelta(minutes=30)))
    actions = list_operator_actions(conn, action_type="PAUSE_RESUMED")
    assert len(actions) == 1
    assert actions[0]["operator"] == "jijo"


def test_history_preserves_full_transition_sequence(conn):
    request_pause(conn, "maintenance", pause.SOURCE_OPERATOR, clock=_clock_at(BASE_TIME))
    resume(conn, "done", "jijo", clock=_clock_at(BASE_TIME + timedelta(minutes=10)))
    kill(conn, "critical", "jijo", clock=_clock_at(BASE_TIME + timedelta(minutes=20)))
    force_clear_kill(conn, "fixed", "jijo", clock=_clock_at(BASE_TIME + timedelta(minutes=30)))
    states = [h.state for h in history(conn)]
    assert states == [
        pause.STATE_PAUSED_MANUAL, pause.STATE_ACTIVE, pause.STATE_KILLED, pause.STATE_ACTIVE,
    ]


def test_only_one_current_row_at_a_time(conn):
    request_pause(conn, "maintenance", pause.SOURCE_OPERATOR, clock=_clock_at(BASE_TIME))
    resume(conn, "done", "jijo", clock=_clock_at(BASE_TIME + timedelta(minutes=10)))
    current_rows = conn.execute("SELECT COUNT(*) AS n FROM shadow_pause_state WHERE is_current = 1").fetchone()
    assert current_rows["n"] == 1


# --- provider/Claude structurally never called from this module -------------


def test_pause_module_has_no_provider_or_research_imports():
    """Structural check: `shadow/pause.py` must not import anything from
    `evidence_providers` or `research` (Claude/provider code) — no model or
    provider output can reach pause/kill decisions because the module
    literally cannot see it."""
    import trading_research.shadow.pause as pause_module

    source = Path(pause_module.__file__).read_text()
    tree = ast.parse(source)
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden_substrings = ("evidence_providers", "research.orchestration", "research.scheduled_cycle", "anthropic")
    for module_name in imported_modules:
        for forbidden in forbidden_substrings:
            assert forbidden not in module_name, f"pause.py must not import {module_name!r} (contains {forbidden!r})"
