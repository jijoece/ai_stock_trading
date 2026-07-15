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


def save_cash_ledger_entry(conn: sqlite3.Connection, entry) -> bool:
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
    conn.commit()
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


def save_order_intent(conn: sqlite3.Connection, intent) -> bool:
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
    conn.commit()
    return True


def update_order_status(conn: sqlite3.Connection, book_id: str, paper_order_intent_id: str, status: str) -> None:
    conn.execute(
        "UPDATE paper_book_orders SET status = ? WHERE book_id = ? AND paper_order_intent_id = ?",
        (status, book_id, paper_order_intent_id),
    )
    conn.commit()


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


def save_fill(conn: sqlite3.Connection, fill: dict) -> bool:
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
    conn.commit()
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


def upsert_position(conn: sqlite3.Connection, book_id: str, symbol: str, fields: dict) -> None:
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
    conn.commit()


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


def save_lot(conn: sqlite3.Connection, lot: dict) -> None:
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
    conn.commit()


def update_lot_remaining(conn: sqlite3.Connection, book_id: str, lot_id: str, remaining_quantity: Decimal, closed_at: datetime | None) -> None:
    conn.execute(
        "UPDATE paper_book_position_lots SET remaining_quantity = ?, closed_at = ? WHERE book_id = ? AND lot_id = ?",
        (str(remaining_quantity), _ts(closed_at) if closed_at else None, book_id, lot_id),
    )
    conn.commit()


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


def save_snapshot(conn: sqlite3.Connection, snapshot, position_rows: list[dict]) -> bool:
    if snapshot_exists(conn, snapshot.book_id, snapshot.snapshot_id):
        return False
    conn.execute(
        "INSERT INTO paper_book_snapshots (book_id, snapshot_id, as_of, cash_available_usd, "
        "cash_reserved_usd, gross_market_value_usd, net_liquidation_value_usd, total_cost_basis_usd, "
        "unrealized_pnl_usd, realized_pnl_usd, position_count, unvalued_position_count, "
        "stale_position_count, valuation_status, source_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            snapshot.book_id, snapshot.snapshot_id, _ts(snapshot.as_of), str(snapshot.cash_available_usd),
            str(snapshot.cash_reserved_usd), _dec_str(snapshot.gross_market_value_usd),
            _dec_str(snapshot.net_liquidation_value_usd), str(snapshot.total_cost_basis_usd),
            _dec_str(snapshot.unrealized_pnl_usd), str(snapshot.realized_pnl_usd),
            snapshot.position_count, snapshot.unvalued_position_count, snapshot.stale_position_count,
            snapshot.valuation_status, snapshot.source_hash, _ts(snapshot.as_of),
        ),
    )
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
    conn.commit()
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
        "quantity, reference_price, reasons_json, policy_version, manual_exit_request_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            exit_decision_id, book_id, symbol, _ts(as_of), decision.decision, str(decision.quantity),
            _dec_str(decision.reference_price), json.dumps(list(decision.reasons)), decision.policy_version,
            manual_exit_request_id, _ts(created_at),
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
    immutable row rather than creating a duplicate."""
    if operator_run_exists(conn, run["operator_run_id"]):
        return False
    conn.execute(
        "INSERT INTO paper_soak_operator_runs (operator_run_id, as_of, requested_cycle_ids_json, "
        "lifecycle_run_id, baseline_reconciliation_status, enhanced_reconciliation_status, soak_report_status, "
        "controlled_readiness_status, failure_reasons_json, policy_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run["operator_run_id"], _ts(run["as_of"]), json.dumps(list(run["requested_cycle_ids"])),
            run["lifecycle_run_id"], run["baseline_reconciliation_status"], run["enhanced_reconciliation_status"],
            run["soak_report_status"], run["controlled_readiness_status"],
            json.dumps(list(run["failure_reasons"])), run["policy_version"], _ts(run["created_at"]),
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
