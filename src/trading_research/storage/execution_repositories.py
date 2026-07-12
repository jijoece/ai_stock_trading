"""Persistence for Milestone 3 paper-execution intents, events, results, and
reconciliations (`storage/execution_schema.py`'s tables).

Mirrors the idempotency posture of `trading_repositories.py`:
`save_frozen_recommendation` there is a no-op on a duplicate `rec_id`; the
`save_*` functions here are no-ops on a duplicate `intent_id`/`event_id`
rather than raising, so a retried service invocation or a duplicate
broker callback can never create a second row. Decimal values are stored as
TEXT (not REAL) so they round-trip exactly — no float precision loss at the
persistence boundary.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from ..execution.models import (
    PaperExecutionEvent,
    PaperExecutionResult,
    PaperOrderIntent,
    ReconciliationResult,
)


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# -- intents ------------------------------------------------------------


def intent_exists(conn: sqlite3.Connection, intent_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM paper_execution_intents WHERE intent_id = ?", (intent_id,)
    ).fetchone()
    return row is not None


def get_intent_by_recommendation(
    conn: sqlite3.Connection, recommendation_id: str, execution_version: str
) -> PaperOrderIntent | None:
    row = conn.execute(
        "SELECT * FROM paper_execution_intents WHERE recommendation_id = ? AND execution_version = ?",
        (recommendation_id, execution_version),
    ).fetchone()
    return _row_to_intent(row) if row else None


def get_intent(conn: sqlite3.Connection, intent_id: str) -> PaperOrderIntent | None:
    row = conn.execute(
        "SELECT * FROM paper_execution_intents WHERE intent_id = ?", (intent_id,)
    ).fetchone()
    return _row_to_intent(row) if row else None


def _row_to_intent(row: sqlite3.Row) -> PaperOrderIntent:
    return PaperOrderIntent(
        intent_id=row["intent_id"],
        recommendation_id=row["recommendation_id"],
        symbol=row["symbol"],
        side=row["side"],
        quantity=row["quantity"],
        order_type=row["order_type"],
        limit_price=_dec(row["limit_price"]),
        reference_price=_dec(row["reference_price"]),
        expected_notional=_dec(row["expected_notional"]),
        recommendation_created_at=_iso(row["recommendation_created_at"]),
        recommendation_frozen_at=_iso(row["recommendation_frozen_at"]),
        expires_at=_iso(row["expires_at"]),
        config_hash=row["config_hash"],
        git_sha=row["git_sha"],
        policy_version=row["policy_version"],
        execution_version=row["execution_version"],
    )


def save_intent(conn: sqlite3.Connection, intent: PaperOrderIntent, *, now: datetime) -> bool:
    """Persist an intent. Returns True if newly inserted, False if this
    exact intent_id already existed (idempotent no-op — never a conflict).

    Relies on the `UNIQUE (recommendation_id, execution_version)` database
    constraint as the ultimate backstop against a second *different* intent
    for the same recommendation — that raises `sqlite3.IntegrityError`
    rather than silently succeeding, since `intent_id` is itself derived
    from `(recommendation_id, execution_version)` and so should never
    legitimately differ for the same pair.
    """
    if intent_exists(conn, intent.intent_id):
        return False
    conn.execute(
        "INSERT INTO paper_execution_intents "
        "(intent_id, recommendation_id, execution_version, symbol, side, quantity, order_type, "
        "limit_price, reference_price, expected_notional, recommendation_created_at, "
        "recommendation_frozen_at, expires_at, config_hash, git_sha, policy_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            intent.intent_id, intent.recommendation_id, intent.execution_version, intent.symbol,
            intent.side, intent.quantity, intent.order_type,
            str(intent.limit_price) if intent.limit_price is not None else None,
            str(intent.reference_price), str(intent.expected_notional),
            intent.recommendation_created_at.isoformat(), intent.recommendation_frozen_at.isoformat(),
            intent.expires_at.isoformat(), intent.config_hash, intent.git_sha, intent.policy_version,
            now.isoformat(),
        ),
    )
    conn.commit()
    return True


# -- events ---------------------------------------------------------------


def event_exists(conn: sqlite3.Connection, event_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM paper_execution_events WHERE event_id = ?", (event_id,)).fetchone()
    return row is not None


def save_event(conn: sqlite3.Connection, event: PaperExecutionEvent, *, now: datetime) -> bool:
    """Persist a normalized execution event. Returns True if newly inserted,
    False if `event_id` already existed — the idempotency guard against a
    duplicate broker callback applying twice."""
    if event_exists(conn, event.event_id):
        return False
    conn.execute(
        "INSERT INTO paper_execution_events "
        "(event_id, intent_id, recommendation_id, symbol, event_type, broker_order_id, quantity, "
        "filled_quantity, fill_price, occurred_at, raw_status, source, ledger_applied, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (
            event.event_id, event.intent_id, event.recommendation_id, event.symbol, event.event_type,
            event.broker_order_id, event.quantity, event.filled_quantity,
            str(event.fill_price) if event.fill_price is not None else None,
            event.occurred_at.isoformat(), event.raw_status, event.source, now.isoformat(),
        ),
    )
    conn.commit()
    return True


def mark_event_ledger_applied(conn: sqlite3.Connection, event_id: str) -> None:
    conn.execute(
        "UPDATE paper_execution_events SET ledger_applied = 1 WHERE event_id = ?", (event_id,)
    )
    conn.commit()


def is_event_ledger_applied(conn: sqlite3.Connection, event_id: str) -> bool:
    row = conn.execute(
        "SELECT ledger_applied FROM paper_execution_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    return bool(row and row["ledger_applied"])


def list_events(conn: sqlite3.Connection, intent_id: str) -> list[PaperExecutionEvent]:
    rows = conn.execute(
        "SELECT * FROM paper_execution_events WHERE intent_id = ? ORDER BY occurred_at, event_id",
        (intent_id,),
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def _row_to_event(row: sqlite3.Row) -> PaperExecutionEvent:
    return PaperExecutionEvent(
        event_id=row["event_id"],
        intent_id=row["intent_id"],
        recommendation_id=row["recommendation_id"],
        symbol=row["symbol"],
        event_type=row["event_type"],
        broker_order_id=row["broker_order_id"],
        quantity=row["quantity"],
        filled_quantity=row["filled_quantity"],
        fill_price=_dec(row["fill_price"]),
        occurred_at=_iso(row["occurred_at"]),
        raw_status=row["raw_status"],
        source=row["source"],
    )


# -- results ----------------------------------------------------------------


def save_result(conn: sqlite3.Connection, result: PaperExecutionResult) -> None:
    """Idempotent upsert — re-deriving the same final result for an intent
    (e.g. on a resumed/retried service invocation) must not fail; the row is
    keyed by `intent_id` (one result per intent, by construction)."""
    conn.execute(
        "INSERT OR REPLACE INTO paper_execution_results "
        "(intent_id, recommendation_id, final_status, requested_quantity, filled_quantity, "
        "average_fill_price, fees, event_ids_json, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            result.intent_id, result.recommendation_id, result.final_status, result.requested_quantity,
            result.filled_quantity,
            str(result.average_fill_price) if result.average_fill_price is not None else None,
            str(result.fees), json.dumps(list(result.event_ids)), result.completed_at.isoformat(),
        ),
    )
    conn.commit()


def get_result(conn: sqlite3.Connection, intent_id: str) -> PaperExecutionResult | None:
    row = conn.execute(
        "SELECT * FROM paper_execution_results WHERE intent_id = ?", (intent_id,)
    ).fetchone()
    if row is None:
        return None
    return PaperExecutionResult(
        intent_id=row["intent_id"],
        recommendation_id=row["recommendation_id"],
        final_status=row["final_status"],
        requested_quantity=row["requested_quantity"],
        filled_quantity=row["filled_quantity"],
        average_fill_price=_dec(row["average_fill_price"]),
        fees=Decimal(row["fees"]),
        event_ids=tuple(json.loads(row["event_ids_json"])),
        completed_at=_iso(row["completed_at"]),
    )


# -- reconciliation -----------------------------------------------------------


def save_reconciliation(conn: sqlite3.Connection, recon: ReconciliationResult) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO paper_execution_reconciliations "
        "(intent_id, status, broker_quantity, ledger_quantity, broker_notional, ledger_notional, "
        "reasons_json, reconciled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            recon.intent_id, recon.status, recon.broker_quantity, recon.ledger_quantity,
            str(recon.broker_notional), str(recon.ledger_notional), json.dumps(list(recon.reasons)),
            recon.reconciled_at.isoformat(),
        ),
    )
    conn.commit()


def get_reconciliation(conn: sqlite3.Connection, intent_id: str) -> ReconciliationResult | None:
    row = conn.execute(
        "SELECT * FROM paper_execution_reconciliations WHERE intent_id = ?", (intent_id,)
    ).fetchone()
    if row is None:
        return None
    return ReconciliationResult(
        intent_id=row["intent_id"],
        status=row["status"],
        broker_quantity=row["broker_quantity"],
        ledger_quantity=row["ledger_quantity"],
        broker_notional=Decimal(row["broker_notional"]),
        ledger_notional=Decimal(row["ledger_notional"]),
        reasons=tuple(json.loads(row["reasons_json"])),
        reconciled_at=_iso(row["reconciled_at"]),
    )


# -- failures (audit trail) --------------------------------------------------


def record_failure(
    conn: sqlite3.Connection,
    *,
    recommendation_id: str | None,
    intent_id: str | None,
    stage: str,
    reason: str,
    now: datetime,
) -> None:
    conn.execute(
        "INSERT INTO paper_execution_failures (recommendation_id, intent_id, stage, reason, occurred_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (recommendation_id, intent_id, stage, reason, now.isoformat()),
    )
    conn.commit()
