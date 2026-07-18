"""Persistence for the isolated paper-book subsystem
(`storage/paper_books_schema.py`'s tables) — Milestone 8.

Mirrors `execution_repositories.py`'s idempotency posture: `save_*` functions
here are no-ops on a duplicate primary key rather than raising, so a retried
service invocation can never create a second row. Decimal values are stored
as TEXT (not REAL) so they round-trip exactly.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from ..utc import canonical_utc_iso


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _dec_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ts(value: datetime) -> str:
    return value.isoformat()


def _campaign_ts(value: datetime) -> str:
    return canonical_utc_iso(value)


def _commit_if(conn: sqlite3.Connection, commit: bool) -> None:
    if commit:
        conn.commit()


# -- paper_books ----------------------------------------------------------


def book_exists(conn: sqlite3.Connection, book_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM paper_books WHERE book_id = ?", (book_id,)).fetchone()
    return row is not None


def save_book(conn: sqlite3.Connection, book) -> None:
    if book_exists(conn, book.book_id):
        return
    conn.execute(
        "INSERT INTO paper_books (book_id, experiment_arm, currency, starting_cash_usd, status, "
        "created_at, config_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            book.book_id, book.experiment_arm, book.currency, str(book.starting_cash_usd),
            book.status, _ts(book.created_at), book.config_hash,
        ),
    )
    conn.commit()


def load_book(conn: sqlite3.Connection, book_id: str):
    from ..paper_books.models import PaperBook

    row = conn.execute("SELECT * FROM paper_books WHERE book_id = ?", (book_id,)).fetchone()
    if row is None:
        return None
    return PaperBook(
        book_id=row["book_id"], experiment_arm=row["experiment_arm"], currency=row["currency"],
        starting_cash_usd=_dec(row["starting_cash_usd"]), status=row["status"],
        created_at=_iso(row["created_at"]), config_hash=row["config_hash"],
    )


def list_books(conn: sqlite3.Connection) -> list:
    rows = conn.execute("SELECT book_id FROM paper_books ORDER BY book_id").fetchall()
    return [load_book(conn, row["book_id"]) for row in rows]


# -- cash ledger ------------------------------------------------------------


def cash_ledger_entry_exists(conn: sqlite3.Connection, book_id: str, idempotency_key: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM paper_book_cash_ledger WHERE book_id = ? AND idempotency_key = ?",
        (book_id, idempotency_key),
    ).fetchone()
    return row is not None


def save_cash_ledger_entry(conn: sqlite3.Connection, entry, *, commit: bool = True) -> bool:
    """Insert one append-only ledger entry. Returns False (no-op) if the
    (book_id, idempotency_key) pair already exists — idempotent settlement."""
    if cash_ledger_entry_exists(conn, entry.book_id, entry.idempotency_key):
        return False
    conn.execute(
        "INSERT INTO paper_book_cash_ledger (book_id, ledger_entry_id, event_type, amount_usd, "
        "event_timestamp, idempotency_key, cycle_id, symbol, reference_id, operator, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            entry.book_id, entry.ledger_entry_id, entry.event_type, str(entry.amount_usd),
            _ts(entry.event_timestamp), entry.idempotency_key, getattr(entry, "cycle_id", None),
            getattr(entry, "symbol", None), entry.reference_id, entry.operator, entry.reason, _ts(entry.event_timestamp),
        ),
    )
    _commit_if(conn, commit)
    return True


def list_cash_ledger_entries(conn: sqlite3.Connection, book_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_book_cash_ledger WHERE book_id = ? ORDER BY event_timestamp, ledger_entry_id",
        (book_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# -- risk decisions -----------------------------------------------------------


def save_risk_decision(conn: sqlite3.Connection, risk_decision_id: str, book_id: str, cycle_id: str,
                        recommendation_id: str, symbol: str, decision, portfolio_snapshot_id: str | None,
                        created_at: datetime) -> bool:
    existing = conn.execute(
        "SELECT 1 FROM paper_book_risk_decisions WHERE risk_decision_id = ?", (risk_decision_id,)
    ).fetchone()
    if existing is not None:
        return False
    conn.execute(
        "INSERT INTO paper_book_risk_decisions (risk_decision_id, book_id, cycle_id, recommendation_id, "
        "symbol, decision, requested_notional_usd, approved_notional_usd, approved_quantity, reasons_json, "
        "policy_version, portfolio_snapshot_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            risk_decision_id, book_id, cycle_id, recommendation_id, symbol, decision.decision,
            _dec_str(decision.requested_notional_usd), _dec_str(decision.approved_notional_usd),
            _dec_str(decision.approved_quantity), json.dumps(list(decision.reasons)), decision.policy_version,
            portfolio_snapshot_id, _ts(created_at),
        ),
    )
    conn.commit()
    return True


def load_risk_decision(conn: sqlite3.Connection, risk_decision_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_book_risk_decisions WHERE risk_decision_id = ?", (risk_decision_id,)
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["reasons"] = tuple(json.loads(result["reasons_json"]))
    return result


# -- orders (paper_book_orders) ---------------------------------------------


def order_exists(conn: sqlite3.Connection, book_id: str, paper_order_intent_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM paper_book_orders WHERE book_id = ? AND paper_order_intent_id = ?",
        (book_id, paper_order_intent_id),
    ).fetchone()
    return row is not None


def save_order_intent(conn: sqlite3.Connection, intent, *, commit: bool = True) -> bool:
    if order_exists(conn, intent.book_id, intent.paper_order_intent_id):
        return False
    conn.execute(
        "INSERT INTO paper_book_orders (book_id, paper_order_intent_id, experiment_arm, cycle_id, "
        "recommendation_id, symbol, side, order_type, quantity, limit_price, notional_usd, "
        "time_in_force, as_of, risk_decision_id, portfolio_snapshot_id, config_hash, created_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            intent.book_id, intent.paper_order_intent_id, intent.experiment_arm, intent.cycle_id,
            intent.recommendation_id, intent.symbol, intent.side, intent.order_type,
            str(intent.quantity), str(intent.limit_price), str(intent.notional_usd),
            intent.time_in_force, _ts(intent.as_of), intent.risk_decision_id, intent.portfolio_snapshot_id,
            intent.config_hash, _ts(intent.created_at), intent.status,
        ),
    )
    _commit_if(conn, commit)
    return True


def update_order_status(
    conn: sqlite3.Connection, book_id: str, paper_order_intent_id: str, status: str, *, commit: bool = True,
) -> None:
    conn.execute(
        "UPDATE paper_book_orders SET status = ? WHERE book_id = ? AND paper_order_intent_id = ?",
        (status, book_id, paper_order_intent_id),
    )
    _commit_if(conn, commit)


def load_order_intent(conn: sqlite3.Connection, book_id: str, paper_order_intent_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_book_orders WHERE book_id = ? AND paper_order_intent_id = ?",
        (book_id, paper_order_intent_id),
    ).fetchone()
    return dict(row) if row else None


def list_order_intents(conn: sqlite3.Connection, book_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_book_orders WHERE book_id = ? ORDER BY created_at", (book_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_order_intents_by_recommendation(conn: sqlite3.Connection, book_id: str, recommendation_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_book_orders WHERE book_id = ? AND recommendation_id = ?",
        (book_id, recommendation_id),
    ).fetchall()
    return [dict(r) for r in rows]


# -- fills -------------------------------------------------------------------


def fill_exists(conn: sqlite3.Connection, book_id: str, fill_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM paper_book_fills WHERE book_id = ? AND fill_id = ?", (book_id, fill_id)
    ).fetchone()
    return row is not None


def save_fill(conn: sqlite3.Connection, fill: dict, *, commit: bool = True) -> bool:
    if fill_exists(conn, fill["book_id"], fill["fill_id"]):
        return False
    conn.execute(
        "INSERT INTO paper_book_fills (book_id, fill_id, paper_order_intent_id, symbol, side, "
        "simulated_market_price, limit_price, fill_quantity, fill_price, fees_usd, slippage_usd, "
        "fill_timestamp, simulation_rule_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fill["book_id"], fill["fill_id"], fill["paper_order_intent_id"], fill["symbol"], fill["side"],
            str(fill["simulated_market_price"]), str(fill["limit_price"]), str(fill["fill_quantity"]),
            str(fill["fill_price"]), str(fill.get("fees_usd", "0")), str(fill.get("slippage_usd", "0")),
            _ts(fill["fill_timestamp"]), fill["simulation_rule_version"], _ts(fill["fill_timestamp"]),
        ),
    )
    _commit_if(conn, commit)
    return True


def list_fills(conn: sqlite3.Connection, book_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_book_fills WHERE book_id = ? ORDER BY fill_timestamp", (book_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def list_fills_for_intent(conn: sqlite3.Connection, book_id: str, paper_order_intent_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_book_fills WHERE book_id = ? AND paper_order_intent_id = ? ORDER BY fill_timestamp",
        (book_id, paper_order_intent_id),
    ).fetchall()
    return [dict(r) for r in rows]


# -- positions and lots -------------------------------------------------------


def upsert_position(
    conn: sqlite3.Connection, book_id: str, symbol: str, fields: dict, *, commit: bool = True,
) -> None:
    existing = conn.execute(
        "SELECT 1 FROM paper_book_positions WHERE book_id = ? AND symbol = ?", (book_id, symbol)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO paper_book_positions (book_id, symbol, quantity, available_quantity, "
            "reserved_quantity, average_cost_usd, realized_pnl_usd, fees_usd, latest_valuation_price, "
            "unrealized_pnl_usd, valuation_timestamp, valuation_status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                book_id, symbol, str(fields["quantity"]), str(fields["available_quantity"]),
                str(fields.get("reserved_quantity", "0")), str(fields["average_cost_usd"]),
                str(fields.get("realized_pnl_usd", "0")), str(fields.get("fees_usd", "0")),
                _dec_str(fields.get("latest_valuation_price")), _dec_str(fields.get("unrealized_pnl_usd")),
                fields.get("valuation_timestamp"), fields.get("valuation_status"), fields["updated_at"],
            ),
        )
    else:
        conn.execute(
            "UPDATE paper_book_positions SET quantity = ?, available_quantity = ?, reserved_quantity = ?, "
            "average_cost_usd = ?, realized_pnl_usd = ?, fees_usd = ?, latest_valuation_price = ?, "
            "unrealized_pnl_usd = ?, valuation_timestamp = ?, valuation_status = ?, updated_at = ? "
            "WHERE book_id = ? AND symbol = ?",
            (
                str(fields["quantity"]), str(fields["available_quantity"]),
                str(fields.get("reserved_quantity", "0")), str(fields["average_cost_usd"]),
                str(fields.get("realized_pnl_usd", "0")), str(fields.get("fees_usd", "0")),
                _dec_str(fields.get("latest_valuation_price")), _dec_str(fields.get("unrealized_pnl_usd")),
                fields.get("valuation_timestamp"), fields.get("valuation_status"), fields["updated_at"],
                book_id, symbol,
            ),
        )
    _commit_if(conn, commit)


def load_position(conn: sqlite3.Connection, book_id: str, symbol: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_book_positions WHERE book_id = ? AND symbol = ?", (book_id, symbol)
    ).fetchone()
    return dict(row) if row else None


def list_positions(conn: sqlite3.Connection, book_id: str, *, open_only: bool = True) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_book_positions WHERE book_id = ? ORDER BY symbol", (book_id,)
    ).fetchall()
    result = [dict(r) for r in rows]
    if open_only:
        result = [r for r in result if Decimal(r["quantity"]) > 0]
    return result


def save_lot(conn: sqlite3.Connection, lot: dict, *, commit: bool = True) -> None:
    existing = conn.execute(
        "SELECT 1 FROM paper_book_position_lots WHERE book_id = ? AND lot_id = ?",
        (lot["book_id"], lot["lot_id"]),
    ).fetchone()
    if existing is not None:
        return
    conn.execute(
        "INSERT INTO paper_book_position_lots (book_id, lot_id, symbol, opened_at, quantity, "
        "remaining_quantity, cost_basis_usd, opening_fill_id, closed_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            lot["book_id"], lot["lot_id"], lot["symbol"], _ts(lot["opened_at"]), str(lot["quantity"]),
            str(lot["remaining_quantity"]), str(lot["cost_basis_usd"]), lot["opening_fill_id"],
            None, _ts(lot["opened_at"]),
        ),
    )
    _commit_if(conn, commit)


def update_lot_remaining(
    conn: sqlite3.Connection, book_id: str, lot_id: str, remaining_quantity: Decimal,
    closed_at: datetime | None, *, commit: bool = True,
) -> None:
    conn.execute(
        "UPDATE paper_book_position_lots SET remaining_quantity = ?, closed_at = ? WHERE book_id = ? AND lot_id = ?",
        (str(remaining_quantity), _ts(closed_at) if closed_at else None, book_id, lot_id),
    )
    _commit_if(conn, commit)


def list_open_lots(conn: sqlite3.Connection, book_id: str, symbol: str) -> list[dict]:
    """FIFO order: oldest lot first."""
    rows = conn.execute(
        "SELECT * FROM paper_book_position_lots WHERE book_id = ? AND symbol = ? AND remaining_quantity > '0' "
        "ORDER BY opened_at, lot_id",
        (book_id, symbol),
    ).fetchall()
    return [dict(r) for r in rows]


def list_all_lots(conn: sqlite3.Connection, book_id: str, symbol: str | None = None) -> list[dict]:
    if symbol is None:
        rows = conn.execute(
            "SELECT * FROM paper_book_position_lots WHERE book_id = ? ORDER BY opened_at, lot_id", (book_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM paper_book_position_lots WHERE book_id = ? AND symbol = ? ORDER BY opened_at, lot_id",
            (book_id, symbol),
        ).fetchall()
    return [dict(r) for r in rows]


# -- snapshots -----------------------------------------------------------


def snapshot_exists(conn: sqlite3.Connection, book_id: str, snapshot_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM paper_book_snapshots WHERE book_id = ? AND snapshot_id = ?", (book_id, snapshot_id)
    ).fetchone()
    return row is not None


class SnapshotIdentityConflictError(RuntimeError):
    """Raised if a `snapshot_id` already on file maps to a *different*
    `source_hash` than the one being saved. Milestone 11.3.1 Item 3:
    `snapshot_id` and `source_hash` are both derived from the identical
    canonical payload (`valuation.py::build_portfolio_snapshot`), so this
    should be structurally unreachable outside a SHA-256 collision or a
    bug — persisting a corrected snapshot silently under its predecessor's
    identity is never acceptable, so this fails closed rather than
    discarding the new content or overwriting the old row."""


def save_snapshot(conn: sqlite3.Connection, snapshot, position_rows: list[dict]) -> bool:
    from .transactions import transaction

    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO paper_book_snapshots (book_id, snapshot_id, as_of, cash_available_usd, "
            "cash_reserved_usd, gross_market_value_usd, net_liquidation_value_usd, total_cost_basis_usd, "
            "unrealized_pnl_usd, realized_pnl_usd, position_count, unvalued_position_count, "
            "stale_position_count, valuation_status, source_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(book_id, snapshot_id) DO NOTHING",
            (
                snapshot.book_id, snapshot.snapshot_id, _ts(snapshot.as_of), str(snapshot.cash_available_usd),
                str(snapshot.cash_reserved_usd), _dec_str(snapshot.gross_market_value_usd),
                _dec_str(snapshot.net_liquidation_value_usd), str(snapshot.total_cost_basis_usd),
                _dec_str(snapshot.unrealized_pnl_usd), str(snapshot.realized_pnl_usd),
                snapshot.position_count, snapshot.unvalued_position_count, snapshot.stale_position_count,
                snapshot.valuation_status, snapshot.source_hash, _ts(snapshot.as_of),
            ),
        )
        if cursor.rowcount == 0:
            existing = conn.execute(
                "SELECT source_hash FROM paper_book_snapshots WHERE book_id = ? AND snapshot_id = ?",
                (snapshot.book_id, snapshot.snapshot_id),
            ).fetchone()
            if existing is None or existing["source_hash"] != snapshot.source_hash:
                existing_hash = existing["source_hash"] if existing is not None else None
                raise SnapshotIdentityConflictError(
                    f"snapshot_id {snapshot.snapshot_id!r} for book {snapshot.book_id!r} already exists with a "
                    f"different source_hash ({existing_hash!r} != {snapshot.source_hash!r}) — refusing "
                    "to silently discard or overwrite a corrected snapshot"
                )
            return False
        for pos in position_rows:
            conn.execute(
                "INSERT INTO paper_book_snapshot_positions (book_id, snapshot_id, symbol, quantity, "
                "cost_basis_usd, price, price_provider, price_timestamp, price_available_at, "
                "point_in_time_safe, source_record_id, staleness_seconds, market_value_usd, "
                "unrealized_pnl_usd, valuation_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot.book_id, snapshot.snapshot_id, pos["symbol"], str(pos["quantity"]),
                    str(pos["cost_basis_usd"]), _dec_str(pos.get("price")), pos.get("price_provider"),
                    pos.get("price_timestamp"), pos.get("price_available_at"),
                    int(bool(pos["point_in_time_safe"])) if pos.get("point_in_time_safe") is not None else None,
                    pos.get("source_record_id"), pos.get("staleness_seconds"),
                    _dec_str(pos.get("market_value_usd")), _dec_str(pos.get("unrealized_pnl_usd")),
                    pos["valuation_status"],
                ),
            )
        return True


def load_snapshot(conn: sqlite3.Connection, book_id: str, snapshot_id: str):
    from ..paper_books.models import PaperPortfolioSnapshot

    row = conn.execute(
        "SELECT * FROM paper_book_snapshots WHERE book_id = ? AND snapshot_id = ?", (book_id, snapshot_id)
    ).fetchone()
    if row is None:
        return None
    return PaperPortfolioSnapshot(
        snapshot_id=row["snapshot_id"], book_id=row["book_id"], as_of=_iso(row["as_of"]),
        cash_available_usd=_dec(row["cash_available_usd"]), cash_reserved_usd=_dec(row["cash_reserved_usd"]),
        gross_market_value_usd=_dec(row["gross_market_value_usd"]),
        net_liquidation_value_usd=_dec(row["net_liquidation_value_usd"]),
        total_cost_basis_usd=_dec(row["total_cost_basis_usd"]), unrealized_pnl_usd=_dec(row["unrealized_pnl_usd"]),
        realized_pnl_usd=_dec(row["realized_pnl_usd"]), position_count=row["position_count"],
        unvalued_position_count=row["unvalued_position_count"], stale_position_count=row["stale_position_count"],
        valuation_status=row["valuation_status"], source_hash=row["source_hash"],
    )


def list_snapshot_positions(conn: sqlite3.Connection, book_id: str, snapshot_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_book_snapshot_positions WHERE book_id = ? AND snapshot_id = ? ORDER BY symbol",
        (book_id, snapshot_id),
    ).fetchall()
    return [dict(r) for r in rows]


def list_snapshots(conn: sqlite3.Connection, book_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_book_snapshots WHERE book_id = ? ORDER BY as_of", (book_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def latest_snapshot_before(conn: sqlite3.Connection, book_id: str, as_of: datetime) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_book_snapshots WHERE book_id = ? AND as_of <= ? ORDER BY as_of DESC LIMIT 1",
        (book_id, _ts(as_of)),
    ).fetchone()
    return dict(row) if row else None


# -- reconciliations -------------------------------------------------------


def save_reconciliation(conn: sqlite3.Connection, book_id: str, reconciliation_id: str, as_of: datetime,
                         status: str, mismatch_details: list, reconciliation_version: str) -> bool:
    existing = conn.execute(
        "SELECT 1 FROM paper_book_reconciliations WHERE book_id = ? AND reconciliation_id = ?",
        (book_id, reconciliation_id),
    ).fetchone()
    if existing is not None:
        return False
    conn.execute(
        "INSERT INTO paper_book_reconciliations (book_id, reconciliation_id, as_of, status, "
        "mismatch_details_json, reconciliation_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (book_id, reconciliation_id, _ts(as_of), status, json.dumps(mismatch_details), reconciliation_version, _ts(as_of)),
    )
    conn.commit()
    return True


def list_reconciliations(conn: sqlite3.Connection, book_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_book_reconciliations WHERE book_id = ? ORDER BY as_of", (book_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# -- daily metrics -----------------------------------------------------------


def save_daily_metrics(conn: sqlite3.Connection, book_id: str, metrics_id: str, window_start: datetime,
                        window_end: datetime, metrics: dict, metric_version: str, created_at: datetime) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO paper_book_daily_metrics (book_id, metrics_id, window_start, window_end, "
        "metrics_json, metric_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (book_id, metrics_id, _ts(window_start), _ts(window_end), json.dumps(metrics, default=str), metric_version, _ts(created_at)),
    )
    conn.commit()


def load_daily_metrics(conn: sqlite3.Connection, book_id: str, metrics_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_book_daily_metrics WHERE book_id = ? AND metrics_id = ?", (book_id, metrics_id)
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["metrics"] = json.loads(result["metrics_json"])
    return result


# -- corporate actions --------------------------------------------------------


def corporate_action_applied(conn: sqlite3.Connection, book_id: str, action_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM paper_book_corporate_actions_applied WHERE book_id = ? AND action_id = ?",
        (book_id, action_id),
    ).fetchone()
    return row is not None


def save_corporate_action_applied(conn: sqlite3.Connection, book_id: str, action_id: str, symbol: str,
                                   action_type: str, effective_date: str, ratio: Decimal | None,
                                   dividend_per_share_usd: Decimal | None, applied_at: datetime, source_hash: str) -> bool:
    if corporate_action_applied(conn, book_id, action_id):
        return False
    conn.execute(
        "INSERT INTO paper_book_corporate_actions_applied (book_id, action_id, symbol, action_type, "
        "effective_date, ratio, dividend_per_share_usd, applied_at, source_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (book_id, action_id, symbol, action_type, effective_date, _dec_str(ratio), _dec_str(dividend_per_share_usd),
         _ts(applied_at), source_hash),
    )
    conn.commit()
    return True


# -- experiment assignment ----------------------------------------------------


def save_experiment_assignment(conn: sqlite3.Connection, assignment: dict) -> bool:
    existing = conn.execute(
        "SELECT 1 FROM paper_book_experiment_assignments WHERE cycle_id = ? AND symbol = ?",
        (assignment["cycle_id"], assignment["symbol"]),
    ).fetchone()
    if existing is not None:
        return False
    conn.execute(
        "INSERT INTO paper_book_experiment_assignments (experiment_id, cycle_id, symbol, as_of, "
        "evidence_snapshot_id, baseline_recommendation_id, enhanced_recommendation_id, baseline_book_id, "
        "enhanced_book_id, baseline_intent_id, enhanced_intent_id, assignment_policy_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            assignment["experiment_id"], assignment["cycle_id"], assignment["symbol"], _ts(assignment["as_of"]),
            assignment.get("evidence_snapshot_id"), assignment.get("baseline_recommendation_id"),
            assignment.get("enhanced_recommendation_id"), assignment.get("baseline_book_id"),
            assignment.get("enhanced_book_id"), assignment.get("baseline_intent_id"),
            assignment.get("enhanced_intent_id"), assignment["assignment_policy_version"],
            _ts(assignment.get("created_at") or assignment["as_of"]),
        ),
    )
    conn.commit()
    return True


def load_experiment_assignment(conn: sqlite3.Connection, cycle_id: str, symbol: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_book_experiment_assignments WHERE cycle_id = ? AND symbol = ?", (cycle_id, symbol)
    ).fetchone()
    return dict(row) if row else None


def list_experiment_assignments(conn: sqlite3.Connection, experiment_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_book_experiment_assignments WHERE experiment_id = ? ORDER BY symbol, cycle_id",
        (experiment_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_all_experiment_assignments_upto(conn: sqlite3.Connection, upto_as_of: str) -> list[dict]:
    """All experiment-assignment rows across every experiment, `as_of <=
    upto_as_of` (Milestone 9.2 cross-book verification — unlike
    `list_experiment_assignments`, not scoped to one `experiment_id`)."""
    rows = conn.execute(
        "SELECT * FROM paper_book_experiment_assignments WHERE as_of <= ? ORDER BY cycle_id, symbol",
        (upto_as_of,),
    ).fetchall()
    return [dict(r) for r in rows]


# -- comparisons and promotion evidence ---------------------------------------


def save_experiment_comparison(conn: sqlite3.Connection, comparison: dict) -> bool:
    existing = conn.execute(
        "SELECT 1 FROM paper_book_experiment_comparisons WHERE comparison_id = ?",
        (comparison["comparison_id"],),
    ).fetchone()
    if existing is not None:
        return False
    conn.execute(
        "INSERT INTO paper_book_experiment_comparisons (comparison_id, experiment_id, baseline_book_id, "
        "enhanced_book_id, window_start, window_end, baseline_metrics_id, enhanced_metrics_id, comparable, "
        "comparability_reasons_json, metric_deltas_json, policy_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            comparison["comparison_id"], comparison["experiment_id"], comparison["baseline_book_id"],
            comparison["enhanced_book_id"], _ts(comparison["window_start"]), _ts(comparison["window_end"]),
            comparison["baseline_metrics_id"], comparison["enhanced_metrics_id"], int(comparison["comparable"]),
            json.dumps(list(comparison["comparability_reasons"])),
            json.dumps({k: str(v) if v is not None else None for k, v in comparison["metric_deltas"].items()}),
            comparison["policy_version"], _ts(comparison["created_at"]),
        ),
    )
    conn.commit()
    return True


def load_experiment_comparison(conn: sqlite3.Connection, comparison_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_book_experiment_comparisons WHERE comparison_id = ?", (comparison_id,)
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["comparability_reasons"] = tuple(json.loads(result["comparability_reasons_json"]))
    deltas = json.loads(result["metric_deltas_json"])
    result["metric_deltas"] = {k: (Decimal(v) if v is not None else None) for k, v in deltas.items()}
    return result


def save_promotion_evidence(conn: sqlite3.Connection, evidence: dict) -> bool:
    existing = conn.execute(
        "SELECT 1 FROM paper_book_promotion_evidence WHERE promotion_evidence_id = ?",
        (evidence["promotion_evidence_id"],),
    ).fetchone()
    if existing is not None:
        return False
    conn.execute(
        "INSERT INTO paper_book_promotion_evidence (promotion_evidence_id, experiment_id, comparison_id, "
        "result, reasons_json, policy_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            evidence["promotion_evidence_id"], evidence["experiment_id"], evidence["comparison_id"],
            evidence["result"], json.dumps(list(evidence["reasons"])), evidence["policy_version"],
            _ts(evidence["created_at"]),
        ),
    )
    conn.commit()
    return True


def load_promotion_evidence(conn: sqlite3.Connection, promotion_evidence_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_book_promotion_evidence WHERE promotion_evidence_id = ?",
        (promotion_evidence_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["reasons"] = tuple(json.loads(result["reasons_json"]))
    return result


# -- Milestone 9: exit decisions / manual exit requests / lifecycle runs ----


def exit_decision_exists(conn: sqlite3.Connection, exit_decision_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM paper_book_exit_decisions WHERE exit_decision_id = ?", (exit_decision_id,)
    ).fetchone()
    return row is not None


def save_exit_decision(
    conn: sqlite3.Connection, *, exit_decision_id: str, book_id: str, symbol: str, as_of: datetime,
    decision, manual_exit_request_id: str | None, created_at: datetime,
) -> bool:
    """`decision` is a `paper_books.exit_policy.PaperExitDecision`. Idempotent:
    a retried lifecycle run for the same (book_id, symbol, as_of,
    policy_version) resolves to the same `exit_decision_id` and is a no-op."""
    if exit_decision_exists(conn, exit_decision_id):
        return False
    conn.execute(
        "INSERT INTO paper_book_exit_decisions (exit_decision_id, book_id, symbol, as_of, decision, "
        "quantity, reference_price, reasons_json, policy_version, partial_stage_id, manual_exit_request_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            exit_decision_id, book_id, symbol, _ts(as_of), decision.decision, str(decision.quantity),
            _dec_str(decision.reference_price), json.dumps(list(decision.reasons)), decision.policy_version,
            decision.partial_stage_id, manual_exit_request_id, _ts(created_at),
        ),
    )
    conn.commit()
    return True


def load_exit_decision(conn: sqlite3.Connection, exit_decision_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_book_exit_decisions WHERE exit_decision_id = ?", (exit_decision_id,)
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["reasons"] = tuple(json.loads(result["reasons_json"]))
    return result


def list_exit_decisions(conn: sqlite3.Connection, book_id: str, symbol: str | None = None) -> list[dict]:
    if symbol is None:
        rows = conn.execute(
            "SELECT * FROM paper_book_exit_decisions WHERE book_id = ? ORDER BY as_of", (book_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM paper_book_exit_decisions WHERE book_id = ? AND symbol = ? ORDER BY as_of",
            (book_id, symbol),
        ).fetchall()
    results = []
    for row in rows:
        result = dict(row)
        result["reasons"] = tuple(json.loads(result["reasons_json"]))
        results.append(result)
    return results


def manual_exit_request_exists(conn: sqlite3.Connection, book_id: str, idempotency_key: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM paper_book_manual_exit_requests WHERE book_id = ? AND idempotency_key = ?",
        (book_id, idempotency_key),
    ).fetchone()
    return row is not None


def save_manual_exit_request(
    conn: sqlite3.Connection, *, manual_exit_request_id: str, book_id: str, symbol: str, operator: str,
    reason: str, requested_at: datetime, idempotency_key: str, created_at: datetime,
) -> bool:
    if manual_exit_request_exists(conn, book_id, idempotency_key):
        return False
    conn.execute(
        "INSERT INTO paper_book_manual_exit_requests (manual_exit_request_id, book_id, symbol, operator, "
        "reason, requested_at, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            manual_exit_request_id, book_id, symbol, operator, reason, _ts(requested_at), idempotency_key,
            _ts(created_at),
        ),
    )
    conn.commit()
    return True


def list_unconsumed_manual_exit_requests(conn: sqlite3.Connection, book_id: str, symbol: str) -> list[dict]:
    """A manual request is "consumed" once an exit decision references its
    `manual_exit_request_id` — this query excludes any such request, so a
    lifecycle rerun never re-triggers an already-acted-on manual exit."""
    rows = conn.execute(
        "SELECT * FROM paper_book_manual_exit_requests r WHERE r.book_id = ? AND r.symbol = ? "
        "AND NOT EXISTS (SELECT 1 FROM paper_book_exit_decisions d "
        "WHERE d.manual_exit_request_id = r.manual_exit_request_id) "
        "ORDER BY r.requested_at",
        (book_id, symbol),
    ).fetchall()
    return [dict(r) for r in rows]


def lifecycle_run_exists(conn: sqlite3.Connection, lifecycle_run_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM paper_book_lifecycle_runs WHERE lifecycle_run_id = ?", (lifecycle_run_id,)
    ).fetchone()
    return row is not None


def save_lifecycle_run(conn: sqlite3.Connection, run: dict) -> bool:
    if lifecycle_run_exists(conn, run["lifecycle_run_id"]):
        return False
    conn.execute(
        "INSERT INTO paper_book_lifecycle_runs (lifecycle_run_id, as_of, processed_cycle_ids_json, "
        "books_processed_json, pending_orders_filled, pending_orders_expired, exit_decisions_json, "
        "exit_orders_created, exit_orders_filled, snapshot_ids_json, reconciliation_statuses_json, "
        "metrics_ids_json, failure_reasons_json, config_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run["lifecycle_run_id"], _ts(run["as_of"]), json.dumps(list(run["processed_cycle_ids"])),
            json.dumps(list(run["books_processed"])), run["pending_orders_filled"], run["pending_orders_expired"],
            json.dumps(list(run["exit_decisions"])), run["exit_orders_created"], run["exit_orders_filled"],
            json.dumps(run["snapshot_ids"]), json.dumps(run["reconciliation_statuses"]),
            json.dumps(run["metrics_ids"]), json.dumps(list(run["failure_reasons"])), run["config_hash"],
            _ts(run["created_at"]),
        ),
    )
    conn.commit()
    return True


def list_lifecycle_runs(conn: sqlite3.Connection, upto_as_of: str | None = None) -> list[dict]:
    if upto_as_of is None:
        rows = conn.execute("SELECT lifecycle_run_id FROM paper_book_lifecycle_runs ORDER BY as_of").fetchall()
    else:
        rows = conn.execute(
            "SELECT lifecycle_run_id FROM paper_book_lifecycle_runs WHERE as_of <= ? ORDER BY as_of", (upto_as_of,)
        ).fetchall()
    return [load_lifecycle_run(conn, row["lifecycle_run_id"]) for row in rows]


def load_lifecycle_run(conn: sqlite3.Connection, lifecycle_run_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_book_lifecycle_runs WHERE lifecycle_run_id = ?", (lifecycle_run_id,)
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["processed_cycle_ids"] = json.loads(result["processed_cycle_ids_json"])
    result["books_processed"] = json.loads(result["books_processed_json"])
    result["exit_decisions"] = json.loads(result["exit_decisions_json"])
    result["snapshot_ids"] = json.loads(result["snapshot_ids_json"])
    result["reconciliation_statuses"] = json.loads(result["reconciliation_statuses_json"])
    result["metrics_ids"] = json.loads(result["metrics_ids_json"])
    result["failure_reasons"] = json.loads(result["failure_reasons_json"])
    return result


def save_lifecycle_symbol_result(conn: sqlite3.Connection, *, lifecycle_run_id: str, book_id: str, symbol: str,
                                  stage: str, outcome: str, reasons: tuple, exit_decision_id: str | None,
                                  paper_order_intent_id: str | None, fill_id: str | None, created_at: datetime) -> bool:
    existing = conn.execute(
        "SELECT 1 FROM paper_book_lifecycle_symbol_results WHERE lifecycle_run_id = ? AND book_id = ? "
        "AND symbol = ? AND stage = ?",
        (lifecycle_run_id, book_id, symbol, stage),
    ).fetchone()
    if existing is not None:
        return False
    conn.execute(
        "INSERT INTO paper_book_lifecycle_symbol_results (lifecycle_run_id, book_id, symbol, stage, outcome, "
        "reasons_json, exit_decision_id, paper_order_intent_id, fill_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            lifecycle_run_id, book_id, symbol, stage, outcome, json.dumps(list(reasons)), exit_decision_id,
            paper_order_intent_id, fill_id, _ts(created_at),
        ),
    )
    conn.commit()
    return True


def list_lifecycle_symbol_results(conn: sqlite3.Connection, lifecycle_run_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_book_lifecycle_symbol_results WHERE lifecycle_run_id = ? ORDER BY book_id, symbol, stage",
        (lifecycle_run_id,),
    ).fetchall()
    results = []
    for row in rows:
        result = dict(row)
        result["reasons"] = tuple(json.loads(result["reasons_json"]))
        results.append(result)
    return results


def operator_run_exists(conn: sqlite3.Connection, operator_run_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM paper_soak_operator_runs WHERE operator_run_id = ?", (operator_run_id,)
    ).fetchone()
    return row is not None


def save_operator_run(conn: sqlite3.Connection, run: dict) -> bool:
    """Milestone 9.1 `paper-soak-run`: insert-or-ignore on the deterministic
    `operator_run_id`, mirroring `save_lifecycle_run` above — a replayed
    command for the identical `as_of`/cycle IDs resolves to the same
    immutable row rather than creating a duplicate. Milestone 9.2 adds the
    optional `cross_book_verification_id`/`cross_book_verification_status`
    (both `None` when the caller has none — never fabricated)."""
    if operator_run_exists(conn, run["operator_run_id"]):
        return False
    conn.execute(
        "INSERT INTO paper_soak_operator_runs (operator_run_id, as_of, requested_cycle_ids_json, "
        "lifecycle_run_id, baseline_reconciliation_status, enhanced_reconciliation_status, soak_report_status, "
        "controlled_readiness_status, failure_reasons_json, policy_version, created_at, "
        "cross_book_verification_id, cross_book_verification_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run["operator_run_id"], _ts(run["as_of"]), json.dumps(list(run["requested_cycle_ids"])),
            run["lifecycle_run_id"], run["baseline_reconciliation_status"], run["enhanced_reconciliation_status"],
            run["soak_report_status"], run["controlled_readiness_status"],
            json.dumps(list(run["failure_reasons"])), run["policy_version"], _ts(run["created_at"]),
            run.get("cross_book_verification_id"), run.get("cross_book_verification_status"),
        ),
    )
    conn.commit()
    return True


def load_operator_run(conn: sqlite3.Connection, operator_run_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_soak_operator_runs WHERE operator_run_id = ?", (operator_run_id,)
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["requested_cycle_ids"] = json.loads(result["requested_cycle_ids_json"])
    result["failure_reasons"] = json.loads(result["failure_reasons_json"])
    return result


def list_operator_runs(conn: sqlite3.Connection, upto_as_of: str | None = None) -> list[dict]:
    if upto_as_of is None:
        rows = conn.execute("SELECT operator_run_id FROM paper_soak_operator_runs ORDER BY as_of").fetchall()
    else:
        rows = conn.execute(
            "SELECT operator_run_id FROM paper_soak_operator_runs WHERE as_of <= ? ORDER BY as_of", (upto_as_of,)
        ).fetchall()
    return [load_operator_run(conn, row["operator_run_id"]) for row in rows]


# -- cross-book verification (Milestone 9.2) ----------------------------------


def cross_book_verification_exists(conn: sqlite3.Connection, verification_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM paper_book_cross_book_verifications WHERE verification_id = ?", (verification_id,)
    ).fetchone()
    return row is not None


def save_cross_book_verification(
    conn: sqlite3.Connection, verification: dict, checks: list[dict],
) -> bool:
    """Insert-or-ignore on the deterministic `verification_id` — a replay for
    identical inputs resolves to the same immutable header row plus its
    already-persisted check rows, never a duplicate (mirrors
    `save_operator_run`'s own convention)."""
    if cross_book_verification_exists(conn, verification["verification_id"]):
        return False
    conn.execute(
        "INSERT INTO paper_book_cross_book_verifications "
        "(verification_id, verification_scope_id, source_state_hash, as_of, operator_run_id, lifecycle_run_id, "
        "status, violation_count, policy_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            verification["verification_id"], verification.get("verification_scope_id"),
            verification.get("source_state_hash"), _ts(verification["as_of"]), verification.get("operator_run_id"),
            verification.get("lifecycle_run_id"), verification["status"], verification["violation_count"],
            verification["policy_version"], _ts(verification["created_at"]),
        ),
    )
    for check in checks:
        conn.execute(
            "INSERT OR IGNORE INTO paper_book_cross_book_verification_checks "
            "(verification_id, check_name, status, observed, expected, source, reason, policy_version, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                verification["verification_id"], check["name"], check["status"], check["observed"],
                check["expected"], check["source"], check["reason"], verification["policy_version"],
                _ts(verification["created_at"]),
            ),
        )
    conn.commit()
    return True


def load_cross_book_verification(conn: sqlite3.Connection, verification_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_book_cross_book_verifications WHERE verification_id = ?", (verification_id,)
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["checks"] = list_cross_book_verification_checks(conn, verification_id)
    return result


def list_cross_book_verification_checks(conn: sqlite3.Connection, verification_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_book_cross_book_verification_checks WHERE verification_id = ? ORDER BY check_name",
        (verification_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def latest_cross_book_verification_upto(conn: sqlite3.Connection, upto_as_of: str) -> dict | None:
    """Latest persisted verification with `as_of <= upto_as_of` (Section 8:
    "the latest applicable persisted verification at or before `as_of`")."""
    row = conn.execute(
        "SELECT verification_id FROM paper_book_cross_book_verifications WHERE as_of <= ? "
        "ORDER BY as_of DESC, created_at DESC, rowid DESC LIMIT 1",
        (upto_as_of,),
    ).fetchone()
    if row is None:
        return None
    return load_cross_book_verification(conn, row["verification_id"])


def list_cross_book_verifications_upto(conn: sqlite3.Connection, upto_as_of: str) -> list[dict]:
    rows = conn.execute(
        "SELECT verification_id FROM paper_book_cross_book_verifications WHERE as_of <= ? "
        "ORDER BY as_of, created_at, verification_id", (upto_as_of,),
    ).fetchall()
    return [load_cross_book_verification(conn, row["verification_id"]) for row in rows]


# -- controlled soak campaigns (Milestone 9.3) -------------------------------


def save_soak_campaign(conn: sqlite3.Connection, record: dict, *, commit: bool = True) -> bool:
    if load_soak_campaign(conn, record["campaign_id"]) is not None:
        return False
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_soak_campaigns (campaign_id, manifest_hash, config_hash, start_as_of, end_as_of, "
        "requested_date_count, requested_cycle_count, status, first_blocking_date, first_blocking_status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (record["campaign_id"], record["manifest_hash"], record["config_hash"], _campaign_ts(record["start_as_of"]),
         _campaign_ts(record["end_as_of"]), record["requested_date_count"], record["requested_cycle_count"],
         record["status"], record.get("first_blocking_date"), record.get("first_blocking_status"),
         _campaign_ts(record["created_at"])),
    )
    _commit_if(conn, commit)
    return cursor.rowcount > 0


def load_soak_campaign(conn: sqlite3.Connection, campaign_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM paper_soak_campaigns WHERE campaign_id = ?", (campaign_id,)).fetchone()
    return dict(row) if row is not None else None


def save_soak_campaign_definition_date(
    conn: sqlite3.Connection, record: dict, *, commit: bool = True,
) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_soak_campaign_definition_dates "
        "(campaign_id, as_of, requested_cycle_ids_json, lifecycle_only, created_at) VALUES (?, ?, ?, ?, ?)",
        (
            record["campaign_id"], _campaign_ts(record["as_of"]),
            json.dumps(list(record["requested_cycle_ids"])), int(record.get("lifecycle_only", False)),
            _campaign_ts(record["created_at"]),
        ),
    )
    _commit_if(conn, commit)
    return cursor.rowcount > 0


def list_soak_campaign_definition_dates(conn: sqlite3.Connection, campaign_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_soak_campaign_definition_dates WHERE campaign_id = ? ORDER BY as_of",
        (campaign_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["requested_cycle_ids"] = json.loads(item["requested_cycle_ids_json"])
        item["lifecycle_only"] = bool(item["lifecycle_only"])
        result.append(item)
    return result


def save_soak_campaign_day(conn: sqlite3.Connection, record: dict) -> bool:
    existing = conn.execute(
        "SELECT 1 FROM paper_soak_campaign_days WHERE campaign_id = ? AND as_of = ?",
        (record["campaign_id"], _ts(record["as_of"])),
    ).fetchone()
    if existing is not None:
        return False
    conn.execute(
        "INSERT INTO paper_soak_campaign_days (campaign_id, as_of, requested_cycle_ids_json, operator_run_id, "
        "lifecycle_run_id, cross_book_verification_id, cross_book_verification_status, controlled_readiness_status, "
        "all_failed_checks_json, failure_reasons_json, day_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (record["campaign_id"], _ts(record["as_of"]), json.dumps(list(record["requested_cycle_ids"])),
         record.get("operator_run_id"), record.get("lifecycle_run_id"), record.get("cross_book_verification_id"),
         record.get("cross_book_verification_status"), record["controlled_readiness_status"],
         json.dumps(list(record["all_failed_checks"])), json.dumps(list(record["failure_reasons"])),
         record["day_status"], _ts(record["created_at"])),
    )
    conn.commit()
    return True


def list_soak_campaign_days(conn: sqlite3.Connection, campaign_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_soak_campaign_days WHERE campaign_id = ? ORDER BY as_of", (campaign_id,)
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["requested_cycle_ids"] = json.loads(item["requested_cycle_ids_json"])
        item["all_failed_checks"] = json.loads(item["all_failed_checks_json"])
        item["failure_reasons"] = json.loads(item["failure_reasons_json"])
        result.append(item)
    return result


def save_soak_campaign_attempt(conn: sqlite3.Connection, record: dict, *, commit: bool = True) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_soak_campaign_attempts "
        "(campaign_attempt_id, campaign_id, manifest_hash, config_hash, previous_attempt_id, attempt_number, "
        "continue_after_blocker, status, started_at, completed_at, first_blocking_date, first_blocking_status, "
        "failure_code, failure_stage, sanitized_message, operator, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record["campaign_attempt_id"], record["campaign_id"], record["manifest_hash"], record["config_hash"],
            record.get("previous_attempt_id"), record["attempt_number"], int(record.get("continue_after_blocker", False)),
            record["status"], _campaign_ts(record["started_at"]),
            _campaign_ts(record["completed_at"]) if record.get("completed_at") else None,
            record.get("first_blocking_date"), record.get("first_blocking_status"), record.get("failure_code"),
            record.get("failure_stage"), record.get("sanitized_message"), record.get("operator"),
            record.get("reason"), _campaign_ts(record["created_at"]),
        ),
    )
    _commit_if(conn, commit)
    return cursor.rowcount > 0


def finalize_soak_campaign_attempt(
    conn: sqlite3.Connection, campaign_attempt_id: str, values: dict, *, commit: bool = True,
) -> bool:
    cursor = conn.execute(
        "UPDATE paper_soak_campaign_attempts SET status = ?, completed_at = ?, first_blocking_date = ?, "
        "first_blocking_status = ?, failure_code = ?, failure_stage = ?, sanitized_message = ? "
        "WHERE campaign_attempt_id = ? AND status = 'RUNNING'",
        (
            values["status"], _campaign_ts(values["completed_at"]), values.get("first_blocking_date"),
            values.get("first_blocking_status"), values.get("failure_code"), values.get("failure_stage"),
            values.get("sanitized_message"), campaign_attempt_id,
        ),
    )
    _commit_if(conn, commit)
    return cursor.rowcount > 0


def load_soak_campaign_attempt(conn: sqlite3.Connection, campaign_attempt_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_soak_campaign_attempts WHERE campaign_attempt_id = ?", (campaign_attempt_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def list_soak_campaign_attempts(conn: sqlite3.Connection, campaign_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_soak_campaign_attempts WHERE campaign_id = ? "
        "ORDER BY attempt_number, campaign_attempt_id", (campaign_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def save_soak_campaign_attempt_day(conn: sqlite3.Connection, record: dict, *, commit: bool = True) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_soak_campaign_attempt_days "
        "(campaign_attempt_id, campaign_id, as_of, requested_cycle_ids_json, lifecycle_only, operator_run_id, "
        "lifecycle_run_id, cross_book_verification_id, cross_book_verification_status, controlled_readiness_status, "
        "all_failed_checks_json, failure_codes_json, failure_reasons_json, day_status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record["campaign_attempt_id"], record["campaign_id"], _campaign_ts(record["as_of"]),
            json.dumps(list(record["requested_cycle_ids"])), int(record.get("lifecycle_only", False)),
            record.get("operator_run_id"), record.get("lifecycle_run_id"),
            record.get("cross_book_verification_id"), record.get("cross_book_verification_status"),
            record["controlled_readiness_status"], json.dumps(list(record.get("all_failed_checks", []))),
            json.dumps(list(record.get("failure_codes", []))), json.dumps(list(record.get("failure_reasons", []))),
            record["day_status"], _campaign_ts(record["created_at"]),
        ),
    )
    _commit_if(conn, commit)
    return cursor.rowcount > 0


def list_soak_campaign_attempt_days(
    conn: sqlite3.Connection, campaign_attempt_id: str | None = None, *, campaign_id: str | None = None,
) -> list[dict]:
    if campaign_attempt_id is not None:
        rows = conn.execute(
            "SELECT * FROM paper_soak_campaign_attempt_days WHERE campaign_attempt_id = ? ORDER BY as_of",
            (campaign_attempt_id,),
        ).fetchall()
    elif campaign_id is not None:
        rows = conn.execute(
            "SELECT d.* FROM paper_soak_campaign_attempt_days d JOIN paper_soak_campaign_attempts a "
            "ON a.campaign_attempt_id = d.campaign_attempt_id WHERE d.campaign_id = ? "
            "ORDER BY a.attempt_number, d.as_of", (campaign_id,),
        ).fetchall()
    else:
        raise ValueError("campaign_attempt_id or campaign_id is required")
    result = []
    for row in rows:
        item = dict(row)
        item["requested_cycle_ids"] = json.loads(item["requested_cycle_ids_json"])
        item["all_failed_checks"] = json.loads(item["all_failed_checks_json"])
        item["failure_codes"] = json.loads(item["failure_codes_json"])
        item["failure_reasons"] = json.loads(item["failure_reasons_json"])
        item["lifecycle_only"] = bool(item["lifecycle_only"])
        result.append(item)
    return result


_ACTIVATION_JSON_FIELDS = (
    "provider_provenance_counts", "provider_success_counts", "cross_book_verification_history",
    "reconciliation_history", "valuation_history", "alert_summary", "pause_and_kill_summary",
    "performance_metrics", "controlled_readiness_history", "reasons",
)


def save_soak_activation_review(conn: sqlite3.Connection, record: dict, *, commit: bool = True) -> bool:
    if load_soak_activation_review(conn, record["activation_review_id"]) is not None:
        return False
    conn.execute(
        "INSERT INTO paper_soak_activation_reviews (activation_review_id, activation_review_scope_id, campaign_id, "
        "campaign_attempt_id, campaign_manifest_hash, config_hash, evidence_state_hash, "
        "supersedes_activation_review_id, campaign_start_as_of, campaign_end_as_of, "
        "completed_market_days, completed_cycles, provider_provenance_counts_json, provider_success_counts_json, "
        "cross_book_verification_history_json, reconciliation_history_json, valuation_history_json, alert_summary_json, "
        "pause_and_kill_summary_json, performance_metrics_json, comparison_id, promotion_evidence_status, "
        "controlled_readiness_history_json, final_recommendation, reasons_json, policy_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (record["activation_review_id"], record.get("activation_review_scope_id"), record["campaign_id"],
         record.get("campaign_attempt_id"), record["campaign_manifest_hash"], record.get("config_hash"),
         record.get("evidence_state_hash"), record.get("supersedes_activation_review_id"),
         record.get("campaign_start_as_of"), record.get("campaign_end_as_of"),
         record["completed_market_days"], record["completed_cycles"],
         json.dumps(record["provider_provenance_counts"], sort_keys=True),
         json.dumps(record["provider_success_counts"], sort_keys=True),
         json.dumps(record["cross_book_verification_history"], sort_keys=True),
         json.dumps(record["reconciliation_history"], sort_keys=True),
         json.dumps(record["valuation_history"], sort_keys=True), json.dumps(record["alert_summary"], sort_keys=True),
         json.dumps(record["pause_and_kill_summary"], sort_keys=True),
         json.dumps(record["performance_metrics"], sort_keys=True), record.get("comparison_id"),
         record["promotion_evidence_status"], json.dumps(record["controlled_readiness_history"], sort_keys=True),
         record["final_recommendation"], json.dumps(list(record["reasons"])), record["policy_version"],
         _campaign_ts(record["created_at"])),
    )
    _commit_if(conn, commit)
    return True


def load_soak_activation_review(conn: sqlite3.Connection, activation_review_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_soak_activation_reviews WHERE activation_review_id = ?", (activation_review_id,)
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    for name in _ACTIVATION_JSON_FIELDS:
        result[name] = json.loads(result[f"{name}_json"])
    return result


def load_soak_activation_review_for_campaign(conn: sqlite3.Connection, campaign_id: str) -> dict | None:
    row = conn.execute(
        "SELECT r.activation_review_id FROM paper_soak_activation_reviews r "
        "LEFT JOIN paper_soak_campaign_attempts a ON a.campaign_attempt_id = r.campaign_attempt_id "
        "WHERE r.campaign_id = ? ORDER BY COALESCE(a.attempt_number, 0) DESC, r.created_at DESC, "
        "r.rowid DESC, r.activation_review_id DESC LIMIT 1", (campaign_id,),
    ).fetchone()
    return None if row is None else load_soak_activation_review(conn, row["activation_review_id"])


def list_soak_activation_reviews(conn: sqlite3.Connection, campaign_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT r.activation_review_id FROM paper_soak_activation_reviews r "
        "LEFT JOIN paper_soak_campaign_attempts a ON a.campaign_attempt_id = r.campaign_attempt_id "
        "WHERE r.campaign_id = ? ORDER BY COALESCE(a.attempt_number, 0), r.created_at, r.rowid, r.activation_review_id",
        (campaign_id,),
    ).fetchall()
    return [load_soak_activation_review(conn, row["activation_review_id"]) for row in rows]


# -- controlled recurring local paper (Milestone 10) -------------------------


def save_recurring_activation_event(conn: sqlite3.Connection, event: dict) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_recurring_activation_events "
        "(activation_event_id, event_type, previous_state, new_state, activation_review_id, campaign_id, "
        "request_event_id, operator, reason, requested_schedule_json, created_at, policy_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (event["activation_event_id"], event["event_type"], event["previous_state"], event["new_state"],
         event.get("activation_review_id"), event.get("campaign_id"), event.get("request_event_id"),
         event["operator"], event["reason"], json.dumps(event.get("requested_schedule", {}), sort_keys=True),
         _ts(event["created_at"]), event["policy_version"]),
    )
    conn.commit()
    return cursor.rowcount > 0


def _recurring_activation_row(row) -> dict:
    result = dict(row)
    result["requested_schedule"] = json.loads(result["requested_schedule_json"])
    return result


def load_recurring_activation_event(conn: sqlite3.Connection, activation_event_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_recurring_activation_events WHERE activation_event_id = ?", (activation_event_id,)
    ).fetchone()
    return None if row is None else _recurring_activation_row(row)


def latest_recurring_activation_event(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_recurring_activation_events ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return None if row is None else _recurring_activation_row(row)


def list_recurring_activation_events(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_recurring_activation_events ORDER BY created_at, rowid"
    ).fetchall()
    return [_recurring_activation_row(row) for row in rows]


def save_recurring_queue_item(conn: sqlite3.Connection, item: dict) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_recurring_cycle_queue "
        "(queue_item_id, cycle_id, status, frozen_state_hash, retry_of_queue_item_id, enqueued_by, enqueue_reason, "
        "enqueued_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (item["queue_item_id"], item["cycle_id"], item["status"], item["frozen_state_hash"],
         item.get("retry_of_queue_item_id"), item["enqueued_by"], item["enqueue_reason"],
         _ts(item["enqueued_at"]), _ts(item["created_at"])),
    )
    conn.commit()
    return cursor.rowcount > 0


def load_recurring_queue_item(conn: sqlite3.Connection, queue_item_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_recurring_cycle_queue WHERE queue_item_id = ?", (queue_item_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def active_recurring_queue_item_for_cycle(conn: sqlite3.Connection, cycle_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_recurring_cycle_queue WHERE cycle_id = ? AND status IN ('QUEUED','CLAIMED') "
        "ORDER BY enqueued_at, queue_item_id LIMIT 1", (cycle_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def list_recurring_queue_items(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    if status is None:
        rows = conn.execute(
            "SELECT * FROM paper_recurring_cycle_queue ORDER BY enqueued_at, queue_item_id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM paper_recurring_cycle_queue WHERE status = ? ORDER BY enqueued_at, queue_item_id",
            (status,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_recurring_scheduler_run_started(conn: sqlite3.Connection, run: dict) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_recurring_scheduler_runs "
        "(scheduler_run_id, intended_schedule_id, intended_at, started_at, owner_id, lease_name, "
        "activation_event_id, activation_review_id, status, config_hash, policy_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?, ?)",
        (run["scheduler_run_id"], run["intended_schedule_id"], _ts(run["intended_at"]), _ts(run["started_at"]),
         run["owner_id"], run["lease_name"], run.get("activation_event_id"), run.get("activation_review_id"),
         run["config_hash"], run["policy_version"], _ts(run["created_at"])),
    )
    conn.commit()
    return cursor.rowcount > 0


def finalize_recurring_scheduler_run(conn: sqlite3.Connection, scheduler_run_id: str, values: dict) -> bool:
    cursor = conn.execute(
        "UPDATE paper_recurring_scheduler_runs SET ended_at = ?, queue_item_ids_json = ?, "
        "requested_cycle_ids_json = ?, processed_cycle_ids_json = ?, operator_run_id = ?, lifecycle_run_id = ?, "
        "cross_book_verification_id = ?, cross_book_verification_status = ?, controlled_readiness_status = ?, "
        "all_failed_checks_json = ?, lifecycle_only = ?, status = ?, failure_reasons_json = ? "
        "WHERE scheduler_run_id = ? AND status = 'RUNNING'",
        (_ts(values["ended_at"]), json.dumps(list(values.get("queue_item_ids", []))),
         json.dumps(list(values.get("requested_cycle_ids", []))), json.dumps(list(values.get("processed_cycle_ids", []))),
         values.get("operator_run_id"), values.get("lifecycle_run_id"), values.get("cross_book_verification_id"),
         values.get("cross_book_verification_status"), values.get("controlled_readiness_status"),
         json.dumps(list(values.get("all_failed_checks", []))), int(bool(values.get("lifecycle_only"))),
         values["status"], json.dumps(list(values.get("failure_reasons", []))), scheduler_run_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def _recurring_run_row(row) -> dict:
    result = dict(row)
    for name in ("queue_item_ids", "requested_cycle_ids", "processed_cycle_ids", "all_failed_checks", "failure_reasons"):
        result[name] = json.loads(result[f"{name}_json"])
    result["lifecycle_only"] = bool(result["lifecycle_only"])
    return result


def load_recurring_scheduler_run(conn: sqlite3.Connection, scheduler_run_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_recurring_scheduler_runs WHERE scheduler_run_id = ?", (scheduler_run_id,)
    ).fetchone()
    return None if row is None else _recurring_run_row(row)


def load_recurring_scheduler_run_for_schedule(conn: sqlite3.Connection, intended_schedule_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_recurring_scheduler_runs WHERE intended_schedule_id = ?", (intended_schedule_id,)
    ).fetchone()
    return None if row is None else _recurring_run_row(row)


def list_recurring_scheduler_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_recurring_scheduler_runs ORDER BY intended_at DESC LIMIT ?", (max(1, min(limit, 200)),)
    ).fetchall()
    return [_recurring_run_row(row) for row in rows]


# -- Milestone 11 external paper broker ---------------------------------------


def save_external_preview(conn: sqlite3.Connection, preview: dict, *, commit: bool = True) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_external_order_previews "
        "(preview_id, paper_order_intent_id, payload_hash, book_id, client_order_id, account_fingerprint, "
        "previewed_at, expires_at, operator, result, reasons_json, config_hash, policy_version) "
        "VALUES (:preview_id, :paper_order_intent_id, :payload_hash, :book_id, :client_order_id, "
        ":account_fingerprint, :previewed_at, :expires_at, :operator, :result, :reasons_json, "
        ":config_hash, :policy_version)",
        {**preview, "reasons_json": json.dumps(list(preview.get("reasons", ())), sort_keys=True)},
    )
    _commit_if(conn, commit)
    return cursor.rowcount > 0


def load_external_preview(conn: sqlite3.Connection, preview_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM paper_external_order_previews WHERE preview_id = ?", (preview_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["reasons"] = tuple(json.loads(result.pop("reasons_json")))
    return result


def save_external_order_event(conn: sqlite3.Connection, event: dict, *, commit: bool = True) -> bool:
    columns = (
        "external_order_event_id", "external_order_scope_id", "book_id", "paper_order_intent_id",
        "client_order_id", "broker_order_id", "account_fingerprint", "previous_state", "new_state",
        "payload_hash", "quantity", "limit_price", "operator", "reason", "runtime_request_id",
        "error_code", "created_at", "policy_version", "config_hash", "attempt_number", "scope_sequence",
    )
    cursor = conn.execute(
        f"INSERT OR IGNORE INTO paper_external_order_events ({', '.join(columns)}) "
        f"VALUES ({', '.join(':' + column for column in columns)})",
        {**event, "quantity": str(event["quantity"]), "limit_price": str(event["limit_price"])},
    )
    _commit_if(conn, commit)
    return cursor.rowcount > 0


def list_external_order_events(
    conn: sqlite3.Connection, *, book_id: str | None = None, client_order_id: str | None = None,
    paper_order_intent_id: str | None = None,
) -> list[dict]:
    clauses, params = [], []
    for column, value in (
        ("book_id", book_id), ("client_order_id", client_order_id),
        ("paper_order_intent_id", paper_order_intent_id),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    query = "SELECT * FROM paper_external_order_events"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    # Part 11: scope_sequence is the chain-ordering authority; legacy rows
    # with a NULL sequence sort first (SQLite's default NULL-lowest-in-ASC),
    # which is correct since they necessarily predate the upgrade.
    query += " ORDER BY scope_sequence, created_at, rowid"
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def load_latest_external_order_event(conn: sqlite3.Connection, book_id: str, client_order_id: str) -> dict | None:
    # Milestone 11.2 Part 11: scope_sequence is the chain-ordering
    # authority, not created_at (a clock regression must never select an
    # earlier event as current). SQLite sorts NULL lowest, so legacy
    # pre-upgrade rows with scope_sequence IS NULL naturally rank behind
    # every sequenced row; created_at/rowid remain tiebreakers only among
    # rows that share a NULL sequence.
    row = conn.execute(
        "SELECT * FROM paper_external_order_events WHERE book_id = ? AND client_order_id = ? "
        "ORDER BY scope_sequence DESC, created_at DESC, rowid DESC LIMIT 1", (book_id, client_order_id),
    ).fetchone()
    return dict(row) if row else None


def load_latest_external_order_event_for_intent(
    conn: sqlite3.Connection, book_id: str, paper_order_intent_id: str,
) -> dict | None:
    """`client_order_id` is deterministically derived from intent fields, so
    one intent maps to exactly one external order-event chain."""
    row = conn.execute(
        "SELECT * FROM paper_external_order_events WHERE book_id = ? AND paper_order_intent_id = ? "
        "ORDER BY scope_sequence DESC, created_at DESC, rowid DESC LIMIT 1", (book_id, paper_order_intent_id),
    ).fetchone()
    return dict(row) if row else None


def save_external_broker_fill(conn: sqlite3.Connection, fill: dict, *, commit: bool = True) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_external_broker_fills "
        "(external_fill_id, book_id, paper_order_intent_id, client_order_id, broker_order_id, "
        "account_fingerprint, symbol, side, quantity, price, filled_at, payload_hash, created_at) "
        "VALUES (:external_fill_id, :book_id, :paper_order_intent_id, :client_order_id, :broker_order_id, "
        ":account_fingerprint, :symbol, :side, :quantity, :price, :filled_at, :payload_hash, :created_at)",
        {**fill, "quantity": str(fill["quantity"]), "price": str(fill["price"])},
    )
    _commit_if(conn, commit)
    return cursor.rowcount > 0


def list_external_broker_fills(conn: sqlite3.Connection, book_id: str, client_order_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_external_broker_fills WHERE book_id = ? AND client_order_id = ? "
        "ORDER BY filled_at, external_fill_id", (book_id, client_order_id),
    ).fetchall()
    return [dict(row) for row in rows]


def save_external_lookup(conn: sqlite3.Connection, lookup: dict, *, commit: bool = True) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_external_order_lookups "
        "(lookup_id, book_id, paper_order_intent_id, client_order_id, account_fingerprint, result, "
        "authoritative, runtime_request_id, created_at, attempt_number, ambiguous_event_id, payload_hash, "
        "lookup_started_at, lookup_completed_at, consumed_by_retry_event_id) "
        "VALUES (:lookup_id, :book_id, :paper_order_intent_id, :client_order_id, :account_fingerprint, "
        ":result, :authoritative, :runtime_request_id, :created_at, :attempt_number, :ambiguous_event_id, "
        ":payload_hash, :lookup_started_at, :lookup_completed_at, :consumed_by_retry_event_id)",
        {
            "consumed_by_retry_event_id": None, "attempt_number": None, "ambiguous_event_id": None,
            "payload_hash": None, "lookup_started_at": None, "lookup_completed_at": None, **lookup,
        },
    )
    _commit_if(conn, commit)
    return cursor.rowcount > 0


def load_latest_external_lookup(conn: sqlite3.Connection, book_id: str, client_order_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_external_order_lookups WHERE book_id = ? AND client_order_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (book_id, client_order_id),
    ).fetchone()
    return dict(row) if row else None


def consume_external_lookup(
    conn: sqlite3.Connection, lookup_id: str, retry_event_id: str, *, commit: bool = True,
) -> bool:
    """One-time transition of `consumed_by_retry_event_id` from NULL. The
    trigger `trg_paper_external_lookups_no_update` aborts any further update
    once set, so a consumed lookup can never authorize a second retry."""
    cursor = conn.execute(
        "UPDATE paper_external_order_lookups SET consumed_by_retry_event_id = ? "
        "WHERE lookup_id = ? AND consumed_by_retry_event_id IS NULL",
        (retry_event_id, lookup_id),
    )
    _commit_if(conn, commit)
    return cursor.rowcount > 0


def save_external_reconciliation(conn: sqlite3.Connection, record: dict, *, commit: bool = True) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_external_reconciliations "
        "(reconciliation_id, book_id, paper_order_intent_id, client_order_id, account_fingerprint, "
        "status, statuses_json, details_json, critical, created_at, policy_version, config_hash) "
        "VALUES (:reconciliation_id, :book_id, :paper_order_intent_id, :client_order_id, "
        ":account_fingerprint, :status, :statuses_json, :details_json, :critical, :created_at, "
        ":policy_version, :config_hash)",
        {
            **record, "statuses_json": json.dumps(list(record["statuses"]), sort_keys=True),
            "details_json": json.dumps(record.get("details", {}), sort_keys=True),
        },
    )
    _commit_if(conn, commit)
    return cursor.rowcount > 0


def list_external_reconciliations(
    conn: sqlite3.Connection, book_id: str, client_order_id: str | None = None,
) -> list[dict]:
    query = "SELECT * FROM paper_external_reconciliations WHERE book_id = ?"
    params: list[object] = [book_id]
    if client_order_id is not None:
        query += " AND client_order_id = ?"
        params.append(client_order_id)
    query += " ORDER BY created_at, rowid"
    result = []
    for row in conn.execute(query, params).fetchall():
        item = dict(row)
        item["statuses"] = tuple(json.loads(item.pop("statuses_json")))
        item["details"] = json.loads(item.pop("details_json"))
        result.append(item)
    return result


def enqueue_external_submission(
    conn: sqlite3.Connection, *, queue_id: str, book_id: str, paper_order_intent_id: str,
    source: str, created_at: str,
) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_external_submission_queue "
        "(queue_id, book_id, paper_order_intent_id, status, source, created_at) "
        "VALUES (?, ?, ?, 'AWAITING_OPERATOR_EXTERNAL_SUBMISSION', ?, ?)",
        (queue_id, book_id, paper_order_intent_id, source, created_at),
    )
    conn.commit()
    return cursor.rowcount > 0


def list_external_submission_queue(conn: sqlite3.Connection, book_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_external_submission_queue WHERE book_id = ? ORDER BY created_at, queue_id",
        (book_id,),
    ).fetchall()
    return [dict(row) for row in rows]


EXECUTION_NAMESPACE_LOCAL = "LOCAL_SIMULATED"
EXECUTION_NAMESPACE_EXTERNAL = "EXTERNAL_PAPER"


class ExecutionNamespaceConflictError(RuntimeError):
    """Raised when a caller tries to claim a `(book_id, paper_order_intent_id)`
    for one execution namespace while it is already durably claimed for the
    other. Milestone 11.3.1 Item 6 Part B: local simulation and external
    paper submission must never both be able to act on the same intent."""

    def __init__(self, message: str, *, existing_namespace: str) -> None:
        super().__init__(message)
        self.existing_namespace = existing_namespace


def load_execution_namespace_claim(
    conn: sqlite3.Connection, book_id: str, paper_order_intent_id: str,
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_order_execution_claims WHERE book_id = ? AND paper_order_intent_id = ?",
        (book_id, paper_order_intent_id),
    ).fetchone()
    return dict(row) if row else None


def claim_execution_namespace(
    conn: sqlite3.Connection, book_id: str, paper_order_intent_id: str, execution_namespace: str,
    claimed_at: datetime, claimed_by: str, *, commit: bool = True,
) -> bool:
    """Atomically claim `(book_id, paper_order_intent_id)` for exactly one
    execution namespace ('LOCAL_SIMULATED' or 'EXTERNAL_PAPER').

    Idempotent no-op (returns False) if this exact namespace already holds
    the claim -- a retried service invocation never re-claims. Raises
    `ExecutionNamespaceConflictError` if the intent is already claimed by
    the *other* namespace -- never silently overwritten. Callers running
    inside an outer `begin_immediate`/`transaction()` block (the common
    case: the claim is inserted atomically alongside the intent and its
    first reservation/preview) must pass `commit=False`; SQLite's
    BEGIN IMMEDIATE write lock is what actually serializes concurrent
    claim attempts against the same intent -- the read-then-insert here is
    only safe because it always runs under that lock.
    """
    existing = load_execution_namespace_claim(conn, book_id, paper_order_intent_id)
    if existing is not None:
        if existing["execution_namespace"] != execution_namespace:
            raise ExecutionNamespaceConflictError(
                f"paper intent {paper_order_intent_id!r} in book {book_id!r} is already claimed by "
                f"{existing['execution_namespace']} execution — cannot also claim {execution_namespace}",
                existing_namespace=existing["execution_namespace"],
            )
        return False
    conn.execute(
        "INSERT INTO paper_order_execution_claims "
        "(book_id, paper_order_intent_id, execution_namespace, claim_generation, claimed_at, claimed_by) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        (book_id, paper_order_intent_id, execution_namespace, _ts(claimed_at), claimed_by),
    )
    _commit_if(conn, commit)
    return True


def has_external_execution_evidence(
    conn: sqlite3.Connection, book_id: str, paper_order_intent_id: str,
) -> bool:
    """True once an intent has crossed into the external-paper namespace."""
    for table in (
        "paper_external_order_previews", "paper_external_order_events",
        "paper_external_broker_fills", "paper_external_submission_queue",
    ):
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE book_id = ? AND paper_order_intent_id = ? LIMIT 1",
            (book_id, paper_order_intent_id),
        ).fetchone()
        if row is not None:
            return True
    return False


# -- Milestone 11.1: external share reservations and order-scope leases ------


def save_external_position_reservation_event(conn: sqlite3.Connection, event: dict, *, commit: bool = True) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_external_position_reservation_events "
        "(reservation_event_id, book_id, symbol, paper_order_intent_id, client_order_id, quantity, "
        "event_type, operator, reason, created_at) "
        "VALUES (:reservation_event_id, :book_id, :symbol, :paper_order_intent_id, :client_order_id, "
        ":quantity, :event_type, :operator, :reason, :created_at)",
        {**event, "quantity": str(event["quantity"])},
    )
    _commit_if(conn, commit)
    return cursor.rowcount > 0


def list_external_position_reservation_events(
    conn: sqlite3.Connection, *, book_id: str, paper_order_intent_id: str,
) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_external_position_reservation_events "
        "WHERE book_id = ? AND paper_order_intent_id = ? ORDER BY created_at, reservation_event_id",
        (book_id, paper_order_intent_id),
    ).fetchall()
    return [dict(row) for row in rows]


def acquire_external_order_lease(
    conn: sqlite3.Connection, *, lease_key: str, book_id: str, client_order_id: str, owner_id: str,
    operation: str, now: str, expires_at: str,
) -> int | None:
    """Milestone 11.2 Part 10: returns the newly-acquired fencing
    generation on success (1 for a brand-new lease row, or the prior
    generation + 1 on a fresh/reclaimed-stale acquisition), or `None` if
    another owner currently holds an unexpired lease."""
    cursor = conn.execute(
        "INSERT INTO paper_external_order_leases "
        "(lease_key, book_id, client_order_id, owner_id, operation, acquired_at, heartbeat_at, "
        "expires_at, released_at, status, generation) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'ACTIVE', 1) "
        "ON CONFLICT(lease_key) DO UPDATE SET "
        "owner_id = excluded.owner_id, operation = excluded.operation, acquired_at = excluded.acquired_at, "
        "heartbeat_at = excluded.heartbeat_at, expires_at = excluded.expires_at, released_at = NULL, "
        "status = 'ACTIVE', generation = paper_external_order_leases.generation + 1 "
        "WHERE paper_external_order_leases.status <> 'ACTIVE' OR paper_external_order_leases.expires_at <= ?",
        (lease_key, book_id, client_order_id, owner_id, operation, now, now, expires_at, now),
    )
    _commit_if(conn, True)
    if cursor.rowcount == 0:
        return None
    row = conn.execute(
        "SELECT generation FROM paper_external_order_leases WHERE lease_key = ? AND owner_id = ?",
        (lease_key, owner_id),
    ).fetchone()
    return row["generation"] if row is not None else None


def heartbeat_external_order_lease(
    conn: sqlite3.Connection, *, lease_key: str, owner_id: str, generation: int, now: str, expires_at: str,
) -> bool:
    """Renews `expires_at`/`heartbeat_at` for the current owner+generation
    without releasing the lease. Fails (returns False, no write) if the
    lease was reclaimed by another owner (generation advanced) or already
    released/expired — a stale owner can never extend its own dead lease."""
    cursor = conn.execute(
        "UPDATE paper_external_order_leases SET heartbeat_at = ?, expires_at = ? "
        "WHERE lease_key = ? AND owner_id = ? AND generation = ? AND status = 'ACTIVE' AND expires_at > ?",
        (now, expires_at, lease_key, owner_id, generation, now),
    )
    conn.commit()
    return cursor.rowcount > 0


def verify_external_order_lease(
    conn: sqlite3.Connection, *, lease_key: str, owner_id: str, generation: int, now: str,
) -> bool:
    """Read-only fencing check: does this owner+generation still hold an
    unexpired ACTIVE lease right now? Used to gate a write immediately
    before it happens, so a stale owner whose lease was reclaimed mid-
    operation cannot proceed to write after all."""
    row = conn.execute(
        "SELECT 1 FROM paper_external_order_leases "
        "WHERE lease_key = ? AND owner_id = ? AND generation = ? AND status = 'ACTIVE' AND expires_at > ?",
        (lease_key, owner_id, generation, now),
    ).fetchone()
    return row is not None


def load_external_order_lease(conn: sqlite3.Connection, lease_key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_external_order_leases WHERE lease_key = ?", (lease_key,)
    ).fetchone()
    return dict(row) if row is not None else None


def release_external_order_lease(
    conn: sqlite3.Connection, *, lease_key: str, owner_id: str, now: str, generation: int | None = None,
) -> bool:
    """`generation`, when supplied, fences the release the same way as
    `heartbeat_external_order_lease` — a stale owner whose lease was
    reclaimed cannot release the *new* owner's lease out from under it."""
    if generation is None:
        cursor = conn.execute(
            "UPDATE paper_external_order_leases SET status = 'RELEASED', released_at = ? "
            "WHERE lease_key = ? AND owner_id = ? AND status = 'ACTIVE'",
            (now, lease_key, owner_id),
        )
    else:
        cursor = conn.execute(
            "UPDATE paper_external_order_leases SET status = 'RELEASED', released_at = ? "
            "WHERE lease_key = ? AND owner_id = ? AND generation = ? AND status = 'ACTIVE'",
            (now, lease_key, owner_id, generation),
        )
    conn.commit()
    return cursor.rowcount > 0


# -- Milestone 13 advanced risk controls -----------------------------------


def save_daily_risk_state(conn: sqlite3.Connection, state, *, commit: bool = True) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_book_daily_risk_states "
        "(risk_state_id, book_id, market_date, as_of, start_of_day_equity, current_equity, "
        "realized_pnl_today, unrealized_pnl_today, total_pnl_today, net_external_cash_flow, "
        "daily_loss_fraction, historical_peak_equity, current_drawdown_fraction, valuation_status, "
        "source_snapshot_ids_json, reconciliation_status, calculation_policy_version, config_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (state.risk_state_id, state.book_id, state.market_date.isoformat(), _ts(state.as_of),
         str(state.start_of_day_equity), str(state.current_equity), str(state.realized_pnl_today),
         str(state.unrealized_pnl_today), str(state.total_pnl_today), str(state.net_external_cash_flow),
         str(state.daily_loss_fraction), str(state.historical_peak_equity), str(state.current_drawdown_fraction),
         state.valuation_status, json.dumps(list(state.source_snapshot_ids)), state.reconciliation_status,
         state.calculation_policy_version, state.config_hash, _ts(state.created_at)),
    )
    _commit_if(conn, commit)
    return cursor.rowcount == 1


def latest_daily_risk_state(conn: sqlite3.Connection, book_id: str, as_of: datetime | None = None) -> dict | None:
    sql = "SELECT * FROM paper_book_daily_risk_states WHERE book_id = ?"
    params: list[object] = [book_id]
    if as_of is not None:
        sql += " AND as_of <= ?"
        params.append(_ts(as_of))
    row = conn.execute(sql + " ORDER BY as_of DESC, created_at DESC LIMIT 1", params).fetchone()
    return dict(row) if row is not None else None


def save_position_lifecycle_state(conn: sqlite3.Connection, state, *, commit: bool = True) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_book_position_lifecycle_states "
        "(lifecycle_state_id, book_id, symbol, originating_intent_id, entry_fill_id, opened_at, "
        "original_quantity, remaining_quantity, average_entry_price, entry_atr, atr_period, "
        "initial_stop_price, current_stop_price, initial_target_price, highest_eligible_price_since_entry, "
        "trailing_stop_active, breakeven_active, partial_profit_stage, policy_version, config_hash, "
        "last_evaluated_at, source_market_data_id, stop_change_reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (state.lifecycle_state_id, state.book_id, state.symbol, state.originating_intent_id,
         state.entry_fill_id, _ts(state.opened_at), str(state.original_quantity), str(state.remaining_quantity),
         str(state.average_entry_price), str(state.entry_atr), state.atr_period, str(state.initial_stop_price),
         str(state.current_stop_price), str(state.initial_target_price),
         str(state.highest_eligible_price_since_entry), int(state.trailing_stop_active),
         int(state.breakeven_active), state.partial_profit_stage, state.policy_version, state.config_hash,
         _ts(state.last_evaluated_at), state.source_market_data_id, state.stop_change_reason,
         _ts(state.last_evaluated_at)),
    )
    _commit_if(conn, commit)
    return cursor.rowcount == 1


def latest_position_lifecycle_state(conn: sqlite3.Connection, book_id: str, symbol: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_book_position_lifecycle_states WHERE book_id = ? AND symbol = ? "
        "ORDER BY last_evaluated_at DESC, created_at DESC LIMIT 1", (book_id, symbol),
    ).fetchone()
    return dict(row) if row is not None else None


def save_partial_exit_stage_event(conn: sqlite3.Connection, event: dict, *, commit: bool = True) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_book_partial_exit_stages "
        "(partial_stage_event_id, book_id, symbol, stage_id, trigger_r_multiple, evaluated_price, "
        "quantity_before, quantity_requested, quantity_approved, quantity_filled, quantity_remaining, "
        "resulting_stop_state_id, decision_id, lifecycle_evaluation_id, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (event["partial_stage_event_id"], event["book_id"], event["symbol"], event["stage_id"],
         str(event["trigger_r_multiple"]), str(event["evaluated_price"]), str(event["quantity_before"]),
         str(event["quantity_requested"]), str(event["quantity_approved"]), str(event["quantity_filled"]),
         str(event["quantity_remaining"]), event["resulting_stop_state_id"], event["decision_id"],
         event["lifecycle_evaluation_id"], event["status"], _ts(event["created_at"])),
    )
    _commit_if(conn, commit)
    return cursor.rowcount == 1


def save_lifecycle_state_event(
    conn: sqlite3.Connection, *, lifecycle_event_id: str, book_id: str, symbol: str,
    previous_state_id: str | None, resulting_state_id: str, event_type: str,
    complete: bool, reasons: tuple[str, ...], created_at: datetime, commit: bool = True,
) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_book_lifecycle_state_events "
        "(lifecycle_event_id, book_id, symbol, previous_state_id, resulting_state_id, event_type, "
        "complete, reasons_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (lifecycle_event_id, book_id, symbol, previous_state_id, resulting_state_id, event_type,
         int(complete), json.dumps(list(reasons)), _ts(created_at)),
    )
    _commit_if(conn, commit)
    return cursor.rowcount == 1


def save_economic_event(conn: sqlite3.Connection, event, *, commit: bool = True) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO economic_calendar_events "
        "(event_id, content_hash, title, category, market, scheduled_at, originally_published_at, "
        "last_updated_at, importance, status, actual_value, forecast_value, previous_value, "
        "source_provider, source_locator, retrieved_at, available_at, point_in_time_safe, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (event.event_id, event.content_hash, event.title, event.category, event.market,
         _ts(event.scheduled_at), _ts(event.originally_published_at), _ts(event.last_updated_at),
         event.importance, event.status, event.actual_value, event.forecast_value, event.previous_value,
         event.source_provider, event.source_locator, _ts(event.retrieved_at), _ts(event.available_at),
         int(event.point_in_time_safe), _ts(event.retrieved_at)),
    )
    _commit_if(conn, commit)
    return cursor.rowcount == 1


def save_economic_blackout_decision(
    conn: sqlite3.Connection, *, book_id: str, order_evaluation_id: str,
    as_of: datetime, decision, created_at: datetime, commit: bool = True,
) -> bool:
    from ..evidence_providers.economic_calendar import blackout_decision_id
    cursor = conn.execute(
        "INSERT OR IGNORE INTO economic_blackout_decisions "
        "(blackout_decision_id, book_id, order_evaluation_id, as_of, allowed, matched_event_ids_json, "
        "blackout_start, blackout_end, reason_codes_json, policy_version, configuration_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (blackout_decision_id(book_id, order_evaluation_id, as_of), book_id, order_evaluation_id,
         _ts(as_of), int(decision.allowed), json.dumps(list(decision.matched_event_ids)),
         _ts(decision.blackout_start) if decision.blackout_start else None,
         _ts(decision.blackout_end) if decision.blackout_end else None,
         json.dumps(list(decision.reason_codes)), decision.policy_version, decision.configuration_hash,
         _ts(created_at)),
    )
    _commit_if(conn, commit)
    return cursor.rowcount == 1


def record_book_safety_event(
    conn: sqlite3.Connection, *, safety_event_id: str, book_id: str, state: str,
    reason_code: str, source_risk_state_id: str | None, operator: str | None,
    reason: str, created_at: datetime, commit: bool = True,
) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO paper_book_safety_events "
        "(safety_event_id, book_id, state, reason_code, source_risk_state_id, operator, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (safety_event_id, book_id, state, reason_code, source_risk_state_id, operator, reason, _ts(created_at)),
    )
    _commit_if(conn, commit)
    return cursor.rowcount == 1


def latest_book_safety_event(conn: sqlite3.Connection, book_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_book_safety_events WHERE book_id = ? ORDER BY created_at DESC, safety_event_id DESC LIMIT 1",
        (book_id,),
    ).fetchone()
    return dict(row) if row is not None else None
