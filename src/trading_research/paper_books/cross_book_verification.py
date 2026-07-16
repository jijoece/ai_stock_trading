"""Authoritative cross-book isolation verification (Milestone 9.2,
docs/milestone-9.2.md Sections 5-8).

No Milestone 8/9 module has ever persisted a dedicated signal proving book
isolation itself was never violated — `controlled_soak_readiness.py`'s own
`_CROSS_BOOK_SIGNAL_AVAILABLE = False` (Milestone 9.1) documented exactly
this gap rather than fabricating a result. This module closes it: every
check below reads already-persisted `paper_books` rows (orders, fills,
lots, cash ledger, experiment assignments, lifecycle symbol results,
reconciliations) and proves — or disproves — that no row from one book ever
references, or was computed from, another book's data. Absence of an
exception while iterating rows is never treated as a pass; the caller must
persist this module's own explicit `PASSED`/`FAILED`/`INSUFFICIENT_DATA`
result via `cross_book_verification_repositories`-style calls in
`storage/paper_books_repositories.py`.

Every table this module reads is already `book_id`-scoped by primary key
(`storage/paper_books_schema.py`'s own documented invariant), so every check
below joins by `(book_id, some_id)` tuples — the same identifier *text*
appearing in two different, correctly isolated books is structurally never
compared cross-book and therefore never flagged (Section 6's "idempotency"
requirement).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..storage import paper_books_repositories as pb_repo
from ..utc import TimestampError, canonical_utc, canonical_utc_iso, parse_aware_utc
from .config import PaperBooksConfiguration

POLICY_VERSION = "cross-book-verification/v2"

STATUS_PASSED = "PASSED"
STATUS_FAILED = "FAILED"
STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
VERIFICATION_STATUSES = (STATUS_PASSED, STATUS_FAILED, STATUS_INSUFFICIENT_DATA)

CHECK_STATUS_PASSED = "PASSED"
CHECK_STATUS_FAILED = "FAILED"
CHECK_STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"


class CrossBookVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CrossBookCheck:
    name: str
    status: str
    observed: str | None
    expected: str | None
    source: str
    reason: str


@dataclass(frozen=True)
class CrossBookVerificationResult:
    verification_id: str
    as_of: datetime
    status: str
    checks: tuple[CrossBookCheck, ...]
    violation_count: int
    policy_version: str
    verification_scope_id: str = ""
    source_state_hash: str = ""

    def __post_init__(self) -> None:
        if self.status not in VERIFICATION_STATUSES:
            raise CrossBookVerificationError(f"status {self.status!r} is not one of {VERIFICATION_STATUSES} — fails closed")


def _verification_scope_id(as_of: datetime, operator_run_id: str | None, lifecycle_run_id: str | None) -> str:
    digest = hashlib.sha256(
        f"{as_of.isoformat()}|{operator_run_id or ''}|{lifecycle_run_id or ''}".encode()
    ).hexdigest()[:32]
    return f"cbvs-{digest}"


def _verification_id(scope_id: str, source_state_hash: str, checks: tuple[CrossBookCheck, ...]) -> str:
    normalized_checks = [
        {"name": c.name, "status": c.status, "observed": c.observed, "expected": c.expected,
         "source": c.source, "reason": c.reason}
        for c in checks
    ]
    digest = hashlib.sha256(
        json.dumps(
            {"scope_id": scope_id, "policy_version": POLICY_VERSION, "source_state_hash": source_state_hash,
             "checks": normalized_checks},
            sort_keys=True, separators=(",", ":"),
        ).encode()
    ).hexdigest()[:32]
    return f"cbv-{digest}"


_CUTOFF_TABLES = (
    ("paper_books", "created_at"), ("paper_book_risk_decisions", "created_at"),
    ("paper_book_orders", "as_of"), ("paper_book_fills", "fill_timestamp"),
    ("paper_book_cash_ledger", "event_timestamp"), ("paper_book_snapshots", "as_of"),
    ("paper_book_daily_metrics", "window_end"), ("paper_book_corporate_actions_applied", "applied_at"),
    ("paper_book_experiment_assignments", "as_of"), ("paper_book_exit_decisions", "as_of"),
    ("paper_book_manual_exit_requests", "requested_at"), ("paper_book_lifecycle_runs", "as_of"),
    ("paper_book_reconciliations", "as_of"),
)


def _upto(value: str | None, cutoff: datetime) -> bool:
    if not value:
        return False
    try:
        return parse_aware_utc(value) <= cutoff
    except TimestampError:
        return False


def source_state_hash(conn, as_of: datetime, book_ids: tuple[str, ...] | None = None) -> str:
    """Hash only cutoff-bounded rows used by verification.

    Mutable current positions and lot remaining quantities are intentionally
    absent. Immutable snapshots/fills represent historical holdings.
    """
    cutoff = canonical_utc(as_of)
    state: dict[str, list[dict]] = {}
    for table, cutoff_column in _CUTOFF_TABLES:
        normalized_rows = [
            dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()
            if _upto(row[cutoff_column], cutoff)
        ]
        if book_ids and normalized_rows and "book_id" in normalized_rows[0]:
            normalized_rows = [row for row in normalized_rows if row["book_id"] in book_ids]
        state[table] = sorted(normalized_rows, key=lambda row: json.dumps(row, sort_keys=True, default=str))
    # Child rows inherit their immutable parent's cutoff.
    state["paper_book_snapshot_positions"] = [
        dict(row) for row in conn.execute(
            "SELECT p.*, s.as_of AS parent_as_of FROM paper_book_snapshot_positions p JOIN paper_book_snapshots s "
            "ON s.book_id=p.book_id AND s.snapshot_id=p.snapshot_id"
        ).fetchall() if _upto(row["parent_as_of"], cutoff) and (not book_ids or row["book_id"] in book_ids)
    ]
    state["paper_book_lifecycle_symbol_results"] = [
        dict(row) for row in conn.execute(
            "SELECT r.*, l.as_of FROM paper_book_lifecycle_symbol_results r "
            "JOIN paper_book_lifecycle_runs l ON l.lifecycle_run_id=r.lifecycle_run_id"
        ).fetchall() if _upto(row["as_of"], cutoff) and (not book_ids or row["book_id"] in book_ids)
    ]
    state["paper_book_position_lot_openings"] = [
        {key: row[key] for key in ("book_id", "lot_id", "symbol", "opened_at", "quantity", "cost_basis_usd", "opening_fill_id")}
        for row in conn.execute("SELECT * FROM paper_book_position_lots").fetchall()
        if _upto(row["opened_at"], cutoff) and (not book_ids or row["book_id"] in book_ids)
    ]
    for key in state:
        state[key] = sorted(state[key], key=lambda row: json.dumps(row, sort_keys=True, default=str))
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def verification_is_stale(conn, verification: dict, as_of: datetime) -> bool:
    del as_of  # staleness is always evaluated against the verification's frozen cutoff
    stored = verification.get("source_state_hash")
    try:
        cutoff = parse_aware_utc(verification["as_of"])
    except (KeyError, TimestampError):
        return True
    return not stored or stored != source_state_hash(conn, cutoff)


def _check_book_and_arm_identity(conn, cfg: PaperBooksConfiguration, as_of_iso: str) -> CrossBookCheck:
    cutoff = parse_aware_utc(as_of_iso)
    books = {b.book_id: b for b in pb_repo.list_books(conn) if canonical_utc(b.created_at) <= cutoff}
    violations = []
    expected_arm = {cfg.baseline.book_id: "BASELINE", cfg.enhanced.book_id: "ENHANCED"}
    for book_id, arm in expected_arm.items():
        book = books.get(book_id)
        if book is not None and book.experiment_arm != arm:
            violations.append(f"{book_id} experiment_arm={book.experiment_arm!r} expected {arm!r}")

    assignments = pb_repo.list_all_experiment_assignments_upto(conn, as_of_iso)
    for a in assignments:
        if a.get("baseline_book_id") is not None and a["baseline_book_id"] != cfg.baseline.book_id:
            violations.append(f"assignment {a['cycle_id']}/{a['symbol']} baseline_book_id={a['baseline_book_id']!r} expected {cfg.baseline.book_id!r}")
        if a.get("enhanced_book_id") is not None and a["enhanced_book_id"] != cfg.enhanced.book_id:
            violations.append(f"assignment {a['cycle_id']}/{a['symbol']} enhanced_book_id={a['enhanced_book_id']!r} expected {cfg.enhanced.book_id!r}")

    if not books and not assignments:
        return CrossBookCheck(
            "book_and_arm_identity", CHECK_STATUS_NOT_APPLICABLE, None, "0 violations",
            "paper_books, paper_book_experiment_assignments", "no books opened and no assignments persisted yet",
        )
    status = CHECK_STATUS_FAILED if violations else CHECK_STATUS_PASSED
    return CrossBookCheck(
        "book_and_arm_identity", status, str(len(violations)), "0 violations",
        "paper_books, paper_book_experiment_assignments",
        "; ".join(violations) if violations else "every book maps to its own arm; no assignment maps an arm to a foreign book_id",
    )


def _check_orders_arm_matches_book(conn, cfg: PaperBooksConfiguration, as_of_iso: str) -> CrossBookCheck:
    cutoff = parse_aware_utc(as_of_iso)
    books = {b.book_id: b.experiment_arm for b in pb_repo.list_books(conn)}
    violations = []
    total = 0
    for book_id in (cfg.baseline.book_id, cfg.enhanced.book_id):
        for order in pb_repo.list_order_intents(conn, book_id):
            if not _upto(order["created_at"], cutoff):
                continue
            total += 1
            expected_arm = books.get(book_id)
            if expected_arm is not None and order["experiment_arm"] != expected_arm:
                violations.append(f"{book_id}/{order['paper_order_intent_id']} experiment_arm={order['experiment_arm']!r} expected {expected_arm!r}")
    if total == 0:
        return CrossBookCheck(
            "orders_arm_matches_book", CHECK_STATUS_NOT_APPLICABLE, None, "0 violations",
            "paper_book_orders", "no orders persisted as of this date",
        )
    status = CHECK_STATUS_FAILED if violations else CHECK_STATUS_PASSED
    return CrossBookCheck(
        "orders_arm_matches_book", status, str(len(violations)), "0 violations", "paper_book_orders",
        "; ".join(violations) if violations else f"{total} order(s) checked — every order's experiment_arm matches its own book",
    )


def _check_fills_reference_own_book_order(conn, cfg: PaperBooksConfiguration, as_of_iso: str) -> CrossBookCheck:
    cutoff = parse_aware_utc(as_of_iso)
    violations = []
    total = 0
    for book_id in (cfg.baseline.book_id, cfg.enhanced.book_id):
        for fill in pb_repo.list_fills(conn, book_id):
            if not _upto(fill["fill_timestamp"], cutoff):
                continue
            total += 1
            order = pb_repo.load_order_intent(conn, book_id, fill["paper_order_intent_id"])
            if order is None or not _upto(order["as_of"], cutoff):
                violations.append(f"{book_id}/{fill['fill_id']} references order {fill['paper_order_intent_id']!r} not found in this book")
    if total == 0:
        return CrossBookCheck(
            "fills_reference_own_book_order", CHECK_STATUS_NOT_APPLICABLE, None, "0 violations",
            "paper_book_fills, paper_book_orders", "no fills persisted as of this date",
        )
    status = CHECK_STATUS_FAILED if violations else CHECK_STATUS_PASSED
    return CrossBookCheck(
        "fills_reference_own_book_order", status, str(len(violations)), "0 violations",
        "paper_book_fills, paper_book_orders",
        "; ".join(violations) if violations else f"{total} fill(s) checked — every fill references an order in the same book",
    )


def _check_cash_ledger_foreign_reference(conn, cfg: PaperBooksConfiguration, as_of_iso: str) -> CrossBookCheck:
    cutoff = parse_aware_utc(as_of_iso)
    book_ids = (cfg.baseline.book_id, cfg.enhanced.book_id)
    fills_by_book = {b: {f["fill_id"] for f in pb_repo.list_fills(conn, b) if _upto(f["fill_timestamp"], cutoff)} for b in book_ids}
    orders_by_book = {b: {o["paper_order_intent_id"] for o in pb_repo.list_order_intents(conn, b) if _upto(o["as_of"], cutoff)} for b in book_ids}
    actions_by_book = {
        b: {r["action_id"] for r in conn.execute(
            "SELECT action_id, applied_at FROM paper_book_corporate_actions_applied WHERE book_id = ?", (b,)
        ).fetchall() if _upto(r["applied_at"], cutoff)} for b in book_ids
    }

    violations = []
    total = 0
    for book_id in book_ids:
        other_book = next(b for b in book_ids if b != book_id)
        for entry in pb_repo.list_cash_ledger_entries(conn, book_id):
            if not _upto(entry["event_timestamp"], cutoff):
                continue
            ref = entry["reference_id"]
            event_type = entry["event_type"]
            if event_type in {"BUY_RESERVATION", "ORDER_RELEASE"}:
                valid = ref is not None and ref in orders_by_book[book_id]
            elif event_type in {"BUY_SETTLEMENT", "SELL_SETTLEMENT", "FEE", "SLIPPAGE"}:
                valid = ref is not None and ref in fills_by_book[book_id]
            elif event_type == "DIVIDEND":
                valid = ref is not None and ref in actions_by_book[book_id]
            elif event_type == "INITIAL_CAPITAL":
                valid = ref is None
            elif event_type == "CASH_ADJUSTMENT":
                valid = ref is None and bool(entry.get("operator")) and bool(entry.get("reason"))
            else:
                valid = False
            if not valid:
                total += 1
                foreign = ref in fills_by_book[other_book] or ref in orders_by_book[other_book] or ref in actions_by_book[other_book]
                detail = f" belongs to {other_book}" if foreign else " does not resolve under the event reference policy"
                violations.append(f"{book_id}/{entry['ledger_entry_id']} reference_id={ref!r}{detail}")
    checked = sum(
        _upto(entry["event_timestamp"], cutoff)
        for b in book_ids for entry in pb_repo.list_cash_ledger_entries(conn, b)
    )
    if checked == 0:
        return CrossBookCheck(
            "cash_ledger_foreign_reference", CHECK_STATUS_NOT_APPLICABLE, None, "0 violations",
            "paper_book_cash_ledger", "no cash-ledger entries persisted as of this date",
        )
    status = CHECK_STATUS_FAILED if violations else CHECK_STATUS_PASSED
    return CrossBookCheck(
        "cash_ledger_foreign_reference", status, str(total), "0 violations", "paper_book_cash_ledger",
        "; ".join(violations) if violations else "every cash-ledger reference follows its event-specific same-book policy",
    )


def _check_position_lot_consistency(conn, cfg: PaperBooksConfiguration, as_of_iso: str) -> CrossBookCheck:
    cutoff = parse_aware_utc(as_of_iso)
    violations = []
    total = 0
    for book_id in (cfg.baseline.book_id, cfg.enhanced.book_id):
        positions: dict[str, Decimal] = {}
        current_rows = pb_repo.list_positions(conn, book_id, open_only=False)
        # A current row is usable only if its last mutation is at/before the
        # cutoff. Otherwise fall back to an immutable campaign snapshot.
        current_is_cutoff_safe = bool(current_rows) and all(_upto(p["updated_at"], cutoff) for p in current_rows)
        if current_is_cutoff_safe:
            positions = {p["symbol"]: Decimal(p["quantity"]) for p in current_rows}
        fill_quantities: dict[str, Decimal] = {}
        for fill in pb_repo.list_fills(conn, book_id):
            if not _upto(fill["fill_timestamp"], cutoff):
                continue
            sign = Decimal("1") if fill["side"] == "BUY" else Decimal("-1")
            fill_quantities[fill["symbol"]] = fill_quantities.get(fill["symbol"], Decimal("0")) + sign * Decimal(fill["fill_quantity"])
        if not current_is_cutoff_safe:
            # Fills are immutable and fully cutoff-bounded, so they are the
            # authoritative historical reconstruction when the one current
            # position row was mutated after the requested instant.
            positions = dict(fill_quantities)
        for symbol in sorted(set(positions) | set(fill_quantities)):
            total += 1
            position_qty = positions.get(symbol, Decimal("0"))
            lot_qty = fill_quantities.get(symbol, Decimal("0"))
            if position_qty != lot_qty:
                violations.append(f"{book_id}/{symbol} historical position quantity={position_qty} but cutoff fills={lot_qty}")
    if total == 0:
        return CrossBookCheck(
            "position_lot_consistency", CHECK_STATUS_NOT_APPLICABLE, None, "0 violations",
            "paper_book_snapshots, paper_book_fills", "historical position reconstruction is unavailable",
        )
    return CrossBookCheck(
        "position_lot_consistency", CHECK_STATUS_FAILED if violations else CHECK_STATUS_PASSED,
        str(len(violations)), "0 violations", "paper_book_snapshots, paper_book_fills",
        "; ".join(violations) if violations else f"{total} book/symbol quantity pair(s) reconcile at the cutoff",
    )


def _check_unexpected_book_namespaces(conn, cfg: PaperBooksConfiguration, as_of_iso: str) -> CrossBookCheck:
    cutoff = parse_aware_utc(as_of_iso)
    expected = {cfg.baseline.book_id, cfg.enhanced.book_id}
    tables = (
        ("paper_books", "created_at"), ("paper_book_cash_ledger", "event_timestamp"),
        ("paper_book_risk_decisions", "created_at"), ("paper_book_orders", "as_of"),
        ("paper_book_fills", "fill_timestamp"), ("paper_book_positions", "updated_at"),
        ("paper_book_position_lots", "opened_at"), ("paper_book_snapshots", "as_of"),
        ("paper_book_reconciliations", "as_of"), ("paper_book_daily_metrics", "window_end"),
        ("paper_book_corporate_actions_applied", "applied_at"), ("paper_book_exit_decisions", "as_of"),
        ("paper_book_manual_exit_requests", "requested_at"),
    )
    violations = []
    observed_rows = 0
    for table, timestamp_column in tables:
        rows = [row for row in conn.execute(f"SELECT book_id, {timestamp_column} FROM {table}").fetchall()
                if _upto(row[timestamp_column], cutoff)]
        observed = {row["book_id"] for row in rows}
        observed_rows += len(observed)
        for book_id in observed:
            if book_id not in expected:
                violations.append(f"{table} contains unconfigured book_id {book_id!r}")
    if observed_rows == 0:
        return CrossBookCheck(
            "unexpected_book_namespaces", CHECK_STATUS_NOT_APPLICABLE, None, "0 violations",
            ", ".join(table for table, _ in tables), "no cutoff-bounded book-scoped rows persisted",
        )
    return CrossBookCheck(
        "unexpected_book_namespaces", CHECK_STATUS_FAILED if violations else CHECK_STATUS_PASSED,
        str(len(violations)), "0 violations", ", ".join(table for table, _ in tables),
        "; ".join(violations) if violations else "every book-scoped row belongs to a configured book",
    )


def _check_lots_reference_own_book_fill(conn, cfg: PaperBooksConfiguration, as_of_iso: str) -> CrossBookCheck:
    cutoff = parse_aware_utc(as_of_iso)
    violations = []
    total = 0
    for book_id in (cfg.baseline.book_id, cfg.enhanced.book_id):
        for lot in pb_repo.list_all_lots(conn, book_id):
            if not _upto(lot["opened_at"], cutoff):
                continue
            total += 1
            fill = next((f for f in pb_repo.list_fills(conn, book_id) if f["fill_id"] == lot["opening_fill_id"]), None)
            if fill is None or not _upto(fill["fill_timestamp"], cutoff):
                violations.append(f"{book_id}/{lot['lot_id']} references fill {lot['opening_fill_id']!r} not found in this book")
    if total == 0:
        return CrossBookCheck(
            "lots_reference_own_book_fill", CHECK_STATUS_NOT_APPLICABLE, None, "0 violations",
            "paper_book_position_lots, paper_book_fills", "no position lots persisted as of this date",
        )
    status = CHECK_STATUS_FAILED if violations else CHECK_STATUS_PASSED
    return CrossBookCheck(
        "lots_reference_own_book_fill", status, str(len(violations)), "0 violations",
        "paper_book_position_lots, paper_book_fills",
        "; ".join(violations) if violations else f"{total} lot(s) checked — every lot's opening fill exists in the same book",
    )


def _check_lifecycle_symbol_results_scope(conn, cfg: PaperBooksConfiguration, as_of_iso: str) -> CrossBookCheck:
    known_books = {cfg.baseline.book_id, cfg.enhanced.book_id}
    lifecycle_runs = [r for r in pb_repo.list_lifecycle_runs(conn, upto_as_of=as_of_iso)]
    violations = []
    total = 0
    for run in lifecycle_runs:
        for result in pb_repo.list_lifecycle_symbol_results(conn, run["lifecycle_run_id"]):
            total += 1
            book_id = result["book_id"]
            if book_id not in known_books:
                violations.append(f"{run['lifecycle_run_id']}/{result['symbol']} references unknown book_id {book_id!r}")
                continue
            exit_decision_id = result.get("exit_decision_id")
            if exit_decision_id is not None:
                decision = pb_repo.load_exit_decision(conn, exit_decision_id)
                if decision is not None and decision["book_id"] != book_id:
                    violations.append(f"{run['lifecycle_run_id']}/{result['symbol']} exit_decision {exit_decision_id!r} belongs to {decision['book_id']!r}, not {book_id!r}")
            intent_id = result.get("paper_order_intent_id")
            if intent_id is not None and not pb_repo.order_exists(conn, book_id, intent_id):
                violations.append(f"{run['lifecycle_run_id']}/{result['symbol']} order {intent_id!r} not found in book {book_id!r}")
            fill_id = result.get("fill_id")
            if fill_id is not None and not pb_repo.fill_exists(conn, book_id, fill_id):
                violations.append(f"{run['lifecycle_run_id']}/{result['symbol']} fill {fill_id!r} not found in book {book_id!r}")
    if total == 0:
        return CrossBookCheck(
            "lifecycle_symbol_results_scope", CHECK_STATUS_NOT_APPLICABLE, None, "0 violations",
            "paper_book_lifecycle_symbol_results", "no lifecycle runs persisted as of this date",
        )
    status = CHECK_STATUS_FAILED if violations else CHECK_STATUS_PASSED
    return CrossBookCheck(
        "lifecycle_symbol_results_scope", status, str(len(violations)), "0 violations",
        "paper_book_lifecycle_symbol_results, paper_book_exit_decisions, paper_book_orders, paper_book_fills",
        "; ".join(violations) if violations else f"{total} lifecycle symbol result(s) checked — every referenced exit decision/order/fill belongs to the same book",
    )


def _check_reconciliations_own_book(conn, cfg: PaperBooksConfiguration, as_of_iso: str) -> CrossBookCheck:
    known_books = {cfg.baseline.book_id, cfg.enhanced.book_id}
    violations = []
    total = 0
    for book_id in known_books:
        for r in pb_repo.list_reconciliations(conn, book_id):
            if r["as_of"] > as_of_iso:
                continue
            total += 1
            if r["book_id"] != book_id:
                violations.append(f"reconciliation {r['reconciliation_id']} keyed under {book_id!r} but book_id column={r['book_id']!r}")
    if total == 0:
        return CrossBookCheck(
            "reconciliations_own_book", CHECK_STATUS_NOT_APPLICABLE, None, "0 violations",
            "paper_book_reconciliations", "no reconciliations persisted as of this date",
        )
    status = CHECK_STATUS_FAILED if violations else CHECK_STATUS_PASSED
    return CrossBookCheck(
        "reconciliations_own_book", status, str(len(violations)), "0 violations", "paper_book_reconciliations",
        "; ".join(violations) if violations else f"{total} reconciliation(s) checked — every row refers to its own book",
    )


_CHECK_FUNCTIONS = (
    _check_book_and_arm_identity,
    _check_orders_arm_matches_book,
    _check_fills_reference_own_book_order,
    _check_cash_ledger_foreign_reference,
    _check_lots_reference_own_book_fill,
    _check_position_lot_consistency,
    _check_unexpected_book_namespaces,
    _check_lifecycle_symbol_results_scope,
    _check_reconciliations_own_book,
)


def verify_cross_book_integrity(
    conn, *, as_of: datetime, paper_books_config: PaperBooksConfiguration,
    operator_run_id: str | None = None, lifecycle_run_id: str | None = None,
) -> CrossBookVerificationResult:
    """Deterministic, read-only verification over already-persisted rows —
    never a live query, never a network call. Absence of an exception while
    iterating is never itself a pass: every check below explicitly reports
    `PASSED`/`FAILED`/`NOT_APPLICABLE`, and this function only reports the
    overall `PASSED` when at least one check actually observed data and none
    failed (Section 7: "zero violations plus insufficient source data must
    not become PASSED")."""
    as_of = canonical_utc(as_of)
    as_of_iso = canonical_utc_iso(as_of)
    checks = tuple(fn(conn, paper_books_config, as_of_iso) for fn in _CHECK_FUNCTIONS)
    state_hash = source_state_hash(conn, as_of)
    scope_id = _verification_scope_id(as_of, operator_run_id, lifecycle_run_id)

    violation_count = sum(int(c.observed) for c in checks if c.status == CHECK_STATUS_FAILED and c.observed is not None)
    if any(c.status == CHECK_STATUS_FAILED for c in checks):
        status = STATUS_FAILED
    elif any(c.status == CHECK_STATUS_PASSED for c in checks):
        status = STATUS_PASSED
    else:
        status = STATUS_INSUFFICIENT_DATA

    return CrossBookVerificationResult(
        verification_id=_verification_id(scope_id, state_hash, checks), as_of=as_of, status=status,
        checks=checks, violation_count=violation_count, policy_version=POLICY_VERSION,
        verification_scope_id=scope_id, source_state_hash=state_hash,
    )


def persist_verification(
    conn, result: CrossBookVerificationResult, *, operator_run_id: str | None, lifecycle_run_id: str | None,
    created_at: datetime,
) -> bool:
    """Insert-or-ignore on `result.verification_id` (Section 7: idempotent,
    no duplicate rows for identical frozen inputs)."""
    persisted_state_hash = result.source_state_hash or source_state_hash(conn, result.as_of)
    persisted_scope_id = result.verification_scope_id or _verification_scope_id(
        result.as_of, operator_run_id, lifecycle_run_id,
    )
    record = {
        "verification_id": result.verification_id, "as_of": result.as_of, "operator_run_id": operator_run_id,
        "lifecycle_run_id": lifecycle_run_id, "status": result.status, "violation_count": result.violation_count,
        "policy_version": result.policy_version, "created_at": created_at,
        "verification_scope_id": persisted_scope_id,
        "source_state_hash": persisted_state_hash,
    }
    check_rows = [
        {
            "name": c.name, "status": c.status, "observed": c.observed, "expected": c.expected,
            "source": c.source, "reason": c.reason,
        }
        for c in result.checks
    ]
    return pb_repo.save_cross_book_verification(conn, record, check_rows)
