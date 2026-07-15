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
from dataclasses import dataclass
from datetime import datetime

from ..storage import paper_books_repositories as pb_repo
from .config import PaperBooksConfiguration

POLICY_VERSION = "cross-book-verification/v1"

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

    def __post_init__(self) -> None:
        if self.status not in VERIFICATION_STATUSES:
            raise CrossBookVerificationError(f"status {self.status!r} is not one of {VERIFICATION_STATUSES} — fails closed")


def _verification_id(as_of: datetime, operator_run_id: str | None, lifecycle_run_id: str | None) -> str:
    digest = hashlib.sha256(
        f"{as_of.isoformat()}|{operator_run_id or ''}|{lifecycle_run_id or ''}|{POLICY_VERSION}".encode()
    ).hexdigest()[:32]
    return f"cbv-{digest}"


def _check_book_and_arm_identity(conn, cfg: PaperBooksConfiguration, as_of_iso: str) -> CrossBookCheck:
    books = {b.book_id: b for b in pb_repo.list_books(conn)}
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
    books = {b.book_id: b.experiment_arm for b in pb_repo.list_books(conn)}
    violations = []
    total = 0
    for book_id in (cfg.baseline.book_id, cfg.enhanced.book_id):
        for order in pb_repo.list_order_intents(conn, book_id):
            if order["created_at"] > as_of_iso:
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
    violations = []
    total = 0
    for book_id in (cfg.baseline.book_id, cfg.enhanced.book_id):
        for fill in pb_repo.list_fills(conn, book_id):
            if fill["fill_timestamp"] > as_of_iso:
                continue
            total += 1
            if not pb_repo.order_exists(conn, book_id, fill["paper_order_intent_id"]):
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
    book_ids = (cfg.baseline.book_id, cfg.enhanced.book_id)
    fills_by_book = {b: {f["fill_id"] for f in pb_repo.list_fills(conn, b)} for b in book_ids}
    orders_by_book = {b: {o["paper_order_intent_id"] for o in pb_repo.list_order_intents(conn, b)} for b in book_ids}

    violations = []
    total = 0
    for book_id in book_ids:
        other_book = next(b for b in book_ids if b != book_id)
        for entry in pb_repo.list_cash_ledger_entries(conn, book_id):
            if entry["event_timestamp"] > as_of_iso or entry.get("reference_id") is None:
                continue
            ref = entry["reference_id"]
            belongs_to_own_book = ref in fills_by_book[book_id] or ref in orders_by_book[book_id]
            belongs_to_other_book = ref in fills_by_book[other_book] or ref in orders_by_book[other_book]
            if belongs_to_other_book and not belongs_to_own_book:
                total += 1
                violations.append(f"{book_id}/{entry['ledger_entry_id']} reference_id={ref!r} belongs to {other_book}")
    checked = sum(len(pb_repo.list_cash_ledger_entries(conn, b)) for b in book_ids)
    if checked == 0:
        return CrossBookCheck(
            "cash_ledger_foreign_reference", CHECK_STATUS_NOT_APPLICABLE, None, "0 violations",
            "paper_book_cash_ledger", "no cash-ledger entries persisted as of this date",
        )
    status = CHECK_STATUS_FAILED if violations else CHECK_STATUS_PASSED
    return CrossBookCheck(
        "cash_ledger_foreign_reference", status, str(total), "0 violations", "paper_book_cash_ledger",
        "; ".join(violations) if violations else "every cash-ledger reference_id resolves within its own book (or matches nothing at all)",
    )


def _check_lots_reference_own_book_fill(conn, cfg: PaperBooksConfiguration, as_of_iso: str) -> CrossBookCheck:
    violations = []
    total = 0
    for book_id in (cfg.baseline.book_id, cfg.enhanced.book_id):
        for lot in pb_repo.list_all_lots(conn, book_id):
            if lot["created_at"] > as_of_iso:
                continue
            total += 1
            if not pb_repo.fill_exists(conn, book_id, lot["opening_fill_id"]):
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
    as_of_iso = as_of.isoformat()
    checks = tuple(fn(conn, paper_books_config, as_of_iso) for fn in _CHECK_FUNCTIONS)

    violation_count = sum(int(c.observed) for c in checks if c.status == CHECK_STATUS_FAILED and c.observed is not None)
    if any(c.status == CHECK_STATUS_FAILED for c in checks):
        status = STATUS_FAILED
    elif any(c.status == CHECK_STATUS_PASSED for c in checks):
        status = STATUS_PASSED
    else:
        status = STATUS_INSUFFICIENT_DATA

    return CrossBookVerificationResult(
        verification_id=_verification_id(as_of, operator_run_id, lifecycle_run_id), as_of=as_of, status=status,
        checks=checks, violation_count=violation_count, policy_version=POLICY_VERSION,
    )


def persist_verification(
    conn, result: CrossBookVerificationResult, *, operator_run_id: str | None, lifecycle_run_id: str | None,
    created_at: datetime,
) -> bool:
    """Insert-or-ignore on `result.verification_id` (Section 7: idempotent,
    no duplicate rows for identical frozen inputs)."""
    record = {
        "verification_id": result.verification_id, "as_of": result.as_of, "operator_run_id": operator_run_id,
        "lifecycle_run_id": lifecycle_run_id, "status": result.status, "violation_count": result.violation_count,
        "policy_version": result.policy_version, "created_at": created_at,
    }
    check_rows = [
        {
            "name": c.name, "status": c.status, "observed": c.observed, "expected": c.expected,
            "source": c.source, "reason": c.reason,
        }
        for c in result.checks
    ]
    return pb_repo.save_cross_book_verification(conn, record, check_rows)
