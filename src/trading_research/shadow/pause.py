"""Global pause and kill switch (docs/milestone-7.md Step 15, ADR 0005
Decision 4). A single current-state row plus an append-only history in
`shadow_pause_state` (`storage/shadow_operations_repositories.py::insert_pause_state`
demotes the previous current row and inserts the new one in one
transaction, so exactly one row ever has `is_current = 1`).

This module has NO import of anything under `evidence_providers` or
`research` (Claude/provider code) — pause/kill state can only be changed by
the plain deterministic functions below, called either by an operator CLI
command or by the (separately implemented, later-milestone) automatic
health-rule evaluator via the `source=SOURCE_AUTOMATIC_HEALTH_RULE` path.
No model output can reach these functions structurally, because nothing in
this module accepts or evaluates Claude/provider output — the caller has
already reduced any such input to a plain reason string before it gets
here.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from ..storage.shadow_operations_repositories import (
    insert_operator_action,
    insert_pause_state,
    list_pause_state_history,
    load_current_pause_state,
)

STATE_ACTIVE = "ACTIVE"
STATE_PAUSED_MANUAL = "PAUSED_MANUAL"
STATE_PAUSED_BUDGET = "PAUSED_BUDGET"
STATE_PAUSED_PROVIDER_HEALTH = "PAUSED_PROVIDER_HEALTH"
STATE_PAUSED_RESEARCH_QUALITY = "PAUSED_RESEARCH_QUALITY"
STATE_PAUSED_RECONCILIATION = "PAUSED_RECONCILIATION"
STATE_KILLED = "KILLED"

ALL_STATES = (
    STATE_ACTIVE, STATE_PAUSED_MANUAL, STATE_PAUSED_BUDGET, STATE_PAUSED_PROVIDER_HEALTH,
    STATE_PAUSED_RESEARCH_QUALITY, STATE_PAUSED_RECONCILIATION, STATE_KILLED,
)
PAUSED_STATES = (
    STATE_PAUSED_MANUAL, STATE_PAUSED_BUDGET, STATE_PAUSED_PROVIDER_HEALTH,
    STATE_PAUSED_RESEARCH_QUALITY, STATE_PAUSED_RECONCILIATION,
)

SOURCE_OPERATOR = "OPERATOR"
SOURCE_AUTOMATIC_HEALTH_RULE = "AUTOMATIC_HEALTH_RULE"
ALL_SOURCES = (SOURCE_OPERATOR, SOURCE_AUTOMATIC_HEALTH_RULE)

Clock = Callable[[], datetime]


class PauseStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class PauseState:
    state: str
    previous_state: str | None
    reason: str
    source: str
    operator: str | None
    expires_at: datetime | None
    created_at: datetime

    def __post_init__(self) -> None:
        if self.state not in ALL_STATES:
            raise PauseStateError(f"unrecognized pause state {self.state!r} — fails closed")
        if self.source not in ALL_SOURCES:
            raise PauseStateError(f"unrecognized pause source {self.source!r} — fails closed")

    @property
    def is_blocking(self) -> bool:
        """True whenever new scheduled work must not proceed — every state
        except ACTIVE."""
        return self.state != STATE_ACTIVE

    @property
    def is_killed(self) -> bool:
        return self.state == STATE_KILLED


def _row_to_state(row: dict) -> PauseState:
    return PauseState(
        state=row["state"], previous_state=row["previous_state"], reason=row["reason"], source=row["source"],
        operator=row["operator"], expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def current_state(conn) -> PauseState:
    row = load_current_pause_state(conn)
    if row is None:
        # No row ever written -> the system starts ACTIVE by definition.
        return PauseState(
            state=STATE_ACTIVE, previous_state=None, reason="no pause-state history — defaults to ACTIVE",
            source=SOURCE_OPERATOR, operator=None, expires_at=None, created_at=datetime.now(),
        )
    return _row_to_state(row)


def history(conn) -> list[PauseState]:
    return [_row_to_state(row) for row in list_pause_state_history(conn)]


def _validate_source(source: str) -> None:
    if source not in ALL_SOURCES:
        raise PauseStateError(f"unrecognized pause source {source!r} — fails closed")


def _write_transition(
    conn, *, new_state: str, reason: str, source: str, operator: str | None, clock: Clock,
    expires_at: datetime | None = None,
) -> PauseState:
    _validate_source(source)
    if not reason or not reason.strip():
        raise PauseStateError(f"transition to {new_state!r} requires a non-empty reason")
    previous = current_state(conn)
    now = clock()
    insert_pause_state(
        conn,
        {
            "state": new_state, "previous_state": previous.state, "reason": reason, "source": source,
            "operator": operator, "expires_at": expires_at.isoformat() if expires_at else None,
            "created_at": now.isoformat(),
        },
    )
    return PauseState(
        state=new_state, previous_state=previous.state, reason=reason, source=source, operator=operator,
        expires_at=expires_at, created_at=now,
    )


def request_pause(
    conn, reason: str, source: str, *, target_state: str = STATE_PAUSED_MANUAL, operator: str | None = None,
    clock: Clock,
) -> PauseState:
    """Requests a pause. `target_state` defaults to `PAUSED_MANUAL` but any
    of the `PAUSED_*` states may be requested (e.g. automatic health rules
    request `PAUSED_BUDGET`/`PAUSED_PROVIDER_HEALTH`/etc). Refuses to
    downgrade a `KILLED` system — pausing a killed system is a no-op that
    must go through `force_clear_kill` first."""
    if target_state not in PAUSED_STATES:
        raise PauseStateError(f"target_state {target_state!r} is not a valid PAUSED_* state — fails closed")
    current = current_state(conn)
    if current.is_killed:
        raise PauseStateError("cannot pause a KILLED system — use force_clear_kill first")
    return _write_transition(conn, new_state=target_state, reason=reason, source=source, operator=operator, clock=clock)


def resume(conn, reason: str, operator: str, *, clock: Clock) -> PauseState:
    """Resumes from any PAUSED_* state back to ACTIVE. Raises if the current
    state is KILLED — killed cannot silently resume (docs/milestone-7.md
    Step 15 / this task's hard boundary)."""
    if not operator or not operator.strip():
        raise PauseStateError("resume requires a non-empty operator identity")
    current = current_state(conn)
    if current.is_killed:
        raise PauseStateError("cannot resume from KILLED — use force_clear_kill (requires operator + reason)")
    if current.state == STATE_ACTIVE:
        raise PauseStateError("cannot resume: system is already ACTIVE")
    result = _write_transition(
        conn, new_state=STATE_ACTIVE, reason=reason, source=SOURCE_OPERATOR, operator=operator, clock=clock,
    )
    insert_operator_action(
        conn,
        {
            "action_id": f"resume-{uuid.uuid4().hex}", "action_type": "PAUSE_RESUMED", "target": "shadow_pause_state",
            "reason": reason, "operator": operator, "details": f"previous_state={current.state}",
            "created_at": result.created_at.isoformat(),
        },
    )
    return result


def kill(conn, reason: str, operator: str, *, clock: Clock) -> PauseState:
    if not operator or not operator.strip():
        raise PauseStateError("kill requires a non-empty operator identity")
    result = _write_transition(
        conn, new_state=STATE_KILLED, reason=reason, source=SOURCE_OPERATOR, operator=operator, clock=clock,
    )
    insert_operator_action(
        conn,
        {
            "action_id": f"kill-{uuid.uuid4().hex}", "action_type": "KILL_ACTIVATED", "target": "shadow_pause_state",
            "reason": reason, "operator": operator, "details": None, "created_at": result.created_at.isoformat(),
        },
    )
    return result


def force_clear_kill(conn, reason: str, operator: str, *, clock: Clock) -> PauseState:
    """The only way to leave KILLED. Always records the operator + reason as
    a `shadow_operator_actions` row (docs/milestone-7.md Step 15: "override
    must be recorded"; "no override can enable live trading" — this function
    only ever transitions pause state, never anything execution-related)."""
    if not operator or not operator.strip():
        raise PauseStateError("force_clear_kill requires a non-empty operator identity")
    if not reason or not reason.strip():
        raise PauseStateError("force_clear_kill requires a non-empty reason")
    current = current_state(conn)
    if not current.is_killed:
        raise PauseStateError("force_clear_kill: system is not currently KILLED")
    result = _write_transition(
        conn, new_state=STATE_ACTIVE, reason=reason, source=SOURCE_OPERATOR, operator=operator, clock=clock,
    )
    insert_operator_action(
        conn,
        {
            "action_id": f"force-clear-kill-{uuid.uuid4().hex}", "action_type": "KILL_FORCE_CLEARED",
            "target": "shadow_pause_state", "reason": reason, "operator": operator, "details": None,
            "created_at": result.created_at.isoformat(),
        },
    )
    return result
