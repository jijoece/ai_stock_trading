"""Manual, ambiguity-safe Alpaca PAPER coordinator for isolated paper books.

The main process owns approved intents, risk/safety checks, audit state and
ledger application. It speaks only normalized JSON payloads to a supplied
runtime client; credentials and Alpaca/LumiBot imports remain in the child
``paper_runtime`` process.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol

from ..shadow import pause as pause_mod
from ..storage import paper_books_repositories as repo
from ..storage.database import begin_immediate
from ..storage.shadow_alerts_repositories import list_alerts
from . import cash_ledger, positions
from .config import PaperBooksConfiguration
from .exit_policy import EXIT_DECISIONS
from .models import APPROVED_RISK_DECISIONS, BOOK_STATUS_ACTIVE

POLICY_VERSION = "external-alpaca-paper-v1"

# Part 10: bounded allowance for clock skew between this process and the
# isolated runtime/broker when judging whether a supplied timestamp is
# "in the future". Documented and small — never used to silently accept a
# materially future timestamp.
_CLOCK_SKEW = timedelta(seconds=5)

STATE_NOT_SUBMITTED = "NOT_SUBMITTED"
STATE_PREVIEWED = "PREVIEWED"
STATE_SUBMISSION_REQUESTED = "SUBMISSION_REQUESTED"
STATE_SUBMITTED = "SUBMITTED"
STATE_PARTIALLY_FILLED = "PARTIALLY_FILLED"
STATE_FILLED = "FILLED"
STATE_CANCEL_REQUESTED = "CANCEL_REQUESTED"
STATE_CANCELLED = "CANCELLED"
STATE_REJECTED = "REJECTED"
STATE_EXPIRED = "EXPIRED"
STATE_UNKNOWN = "UNKNOWN_REQUIRES_RECONCILIATION"

TERMINAL_STATES = frozenset({STATE_FILLED, STATE_CANCELLED, STATE_REJECTED, STATE_EXPIRED})
CRITICAL_RECONCILIATION_STATUSES = frozenset({
    "ORDER_MISSING_LOCALLY", "ORDER_MISSING_AT_BROKER", "AMBIGUOUS_SUBMISSION",
    "BROKER_ORDER_DUPLICATE", "BOOK_NAMESPACE_MISMATCH", "ACCOUNT_FINGERPRINT_MISMATCH",
    "SYMBOL_MISMATCH", "SIDE_MISMATCH", "QUANTITY_MISMATCH", "FILL_QUANTITY_MISMATCH",
    "PRICE_MISMATCH", "CASH_MISMATCH", "POSITION_MISMATCH", "UNKNOWN",
    "FILL_APPLICATION_FAILED", "MALFORMED_BROKER_ORDER", "MALFORMED_BROKER_FILL",
    "BROKER_STATE_UNKNOWN", "RECONCILIATION_INTERNAL_ERROR", "RESERVATION_MISMATCH",
    "SHARE_RESERVATION_MISMATCH", "FROZEN_INTENT_MISMATCH", "EXTERNAL_NOTIONAL_LIMIT",
})

_TRANSITIONS = {
    STATE_NOT_SUBMITTED: {STATE_PREVIEWED},
    STATE_PREVIEWED: {STATE_PREVIEWED, STATE_SUBMISSION_REQUESTED},
    STATE_SUBMISSION_REQUESTED: {
        STATE_SUBMITTED, STATE_PARTIALLY_FILLED, STATE_FILLED, STATE_CANCELLED,
        STATE_REJECTED, STATE_EXPIRED, STATE_UNKNOWN,
    },
    STATE_SUBMITTED: {
        STATE_SUBMITTED, STATE_PARTIALLY_FILLED, STATE_FILLED, STATE_CANCEL_REQUESTED,
        STATE_CANCELLED, STATE_REJECTED, STATE_EXPIRED, STATE_UNKNOWN,
    },
    STATE_PARTIALLY_FILLED: {
        STATE_PARTIALLY_FILLED, STATE_FILLED, STATE_CANCEL_REQUESTED, STATE_CANCELLED, STATE_UNKNOWN,
    },
    STATE_CANCEL_REQUESTED: {
        STATE_CANCEL_REQUESTED, STATE_CANCELLED, STATE_PARTIALLY_FILLED, STATE_FILLED, STATE_UNKNOWN,
    },
    STATE_UNKNOWN: {
        STATE_UNKNOWN, STATE_SUBMITTED, STATE_PARTIALLY_FILLED, STATE_FILLED, STATE_CANCEL_REQUESTED,
        STATE_CANCELLED, STATE_REJECTED, STATE_EXPIRED, STATE_SUBMISSION_REQUESTED,
    },
    STATE_REJECTED: set(), STATE_EXPIRED: set(), STATE_CANCELLED: set(), STATE_FILLED: set(),
}


class ExternalPaperRuntime(Protocol):
    def account_check(self, book_id: str) -> dict: ...
    def preview_limit_order(self, payload: dict) -> dict: ...
    def submit_limit_order(self, payload: dict) -> dict: ...
    def get_order_by_client_order_id(self, book_id: str, client_order_id: str) -> dict | None: ...
    def cancel_external_order(self, book_id: str, client_order_id: str, account_fingerprint: str) -> dict: ...
    def list_order_fills(self, book_id: str, client_order_id: str) -> list[dict]: ...
    def get_external_positions(self, book_id: str) -> dict: ...
    def get_external_account_snapshot(self, book_id: str) -> dict: ...
    def list_recent_external_orders(self, book_id: str, *, limit: int = 50) -> list[dict]: ...


class ExternalPaperError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now(clock) -> datetime:
    value = clock() if clock else datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ExternalPaperError("INVALID_TIMESTAMP", "clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _bounded(value: str, name: str, maximum: int = 256) -> str:
    value = str(value).strip()
    if not value or len(value) > maximum:
        raise ExternalPaperError("INVALID_OPERATOR_INPUT", f"{name} must contain 1..{maximum} characters")
    return value


def _digest(prefix: str, payload: object, length: int = 32) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{prefix}{hashlib.sha256(encoded).hexdigest()[:length]}"


def _exact_int(value: object, name: str) -> int:
    """Part 13: never truncate a safety-sensitive quantity via
    `int(Decimal(...))`/`int(float(...))` — require an exact, finite whole
    number or fail closed."""
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ExternalPaperError("QUANTITY_NOT_WHOLE", f"{name} is not a valid number: {value!r}") from exc
    if not parsed.is_finite():
        raise ExternalPaperError("QUANTITY_NOT_WHOLE", f"{name} must be finite, got {value!r}")
    if parsed != parsed.to_integral_value():
        raise ExternalPaperError("QUANTITY_NOT_WHOLE", f"{name} must be a whole number, got {value!r}")
    return int(parsed)


def _validate_frozen_notional(intent: dict, cfg: PaperBooksConfiguration, risk: dict | None) -> None:
    """Part 14: never trust the persisted `notional_usd` alone — recompute
    `quantity * limit_price` from the frozen intent and require it to match
    exactly, match the approved risk decision's notional (when the BUY path
    approved via a risk decision), and pass the strictest configured cap
    applied to the *recomputed* value. A stored row with a tampered/stale
    low `notional_usd` next to a high quantity/limit_price fails closed here
    rather than only being checked against its own (possibly wrong) field.
    """
    try:
        quantity = Decimal(intent["quantity"])
        limit_price = Decimal(intent["limit_price"])
        notional_usd = Decimal(intent["notional_usd"])
    except (InvalidOperation, TypeError) as exc:
        raise ExternalPaperError("FROZEN_INTENT_MISMATCH", "intent notional fields are not valid decimals") from exc
    if not (quantity.is_finite() and limit_price.is_finite() and notional_usd.is_finite()):
        raise ExternalPaperError("FROZEN_INTENT_MISMATCH", "intent quantity/limit_price/notional_usd must be finite")
    recomputed = quantity * limit_price
    if recomputed != notional_usd:
        raise ExternalPaperError(
            "FROZEN_INTENT_MISMATCH", "recomputed notional does not match the frozen intent notional_usd",
        )
    if (
        intent["side"] == "BUY" and risk is not None and risk.get("decision") in APPROVED_RISK_DECISIONS
        and risk.get("approved_notional_usd") is not None
        and recomputed != Decimal(risk["approved_notional_usd"])
    ):
        raise ExternalPaperError(
            "FROZEN_INTENT_MISMATCH", "recomputed notional does not match the approved risk decision notional",
        )
    if recomputed > min(cfg.risk.max_order_notional_usd, cfg.external_broker.maximum_order_notional_usd):
        raise ExternalPaperError("EXTERNAL_NOTIONAL_LIMIT", "recomputed notional exceeds the strictest configured cap")


def derive_external_order_identity(intent: dict) -> tuple[str, str]:
    immutable = {
        "book_id": intent["book_id"], "paper_order_intent_id": intent["paper_order_intent_id"],
        "symbol": intent["symbol"], "side": intent["side"], "quantity": str(intent["quantity"]),
        "limit_price": str(intent["limit_price"]), "execution_policy_version": POLICY_VERSION,
    }
    payload_hash = _digest("", immutable, 64)
    prefix = f"epb-{intent['book_id'].lower()}-"
    client_order_id = prefix + hashlib.sha256(
        json.dumps(immutable, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]
    if len(client_order_id) > 64 or not re.fullmatch(r"[a-z0-9-]+", client_order_id):
        raise ExternalPaperError("CLIENT_ORDER_ID_INVALID", "derived client order ID is not broker safe")
    return client_order_id, payload_hash


def _current_event(conn: sqlite3.Connection, book_id: str, client_order_id: str) -> dict | None:
    return repo.load_latest_external_order_event(conn, book_id, client_order_id)


def _append_event(
    conn: sqlite3.Connection, *, intent: dict, client_order_id: str, payload_hash: str,
    account_fingerprint: str, new_state: str, operator: str, reason: str, now: datetime,
    broker_order_id: str | None = None, runtime_request_id: str | None = None,
    error_code: str | None = None, attempt_number: int = 0,
) -> dict:
    current = _current_event(conn, intent["book_id"], client_order_id)
    previous = current["new_state"] if current else STATE_NOT_SUBMITTED
    if new_state not in _TRANSITIONS.get(previous, set()):
        raise ExternalPaperError("INVALID_STATE_TRANSITION", f"cannot transition {previous} -> {new_state}")
    previous_sequence = current.get("scope_sequence") if current else None
    next_sequence = (previous_sequence + 1) if previous_sequence is not None else 0
    event = {
        "external_order_event_id": _digest(
            "peoe_", [client_order_id, previous, new_state, now.isoformat(), runtime_request_id, attempt_number], 40,
        ),
        "external_order_scope_id": _digest("peos_", [intent["book_id"], intent["paper_order_intent_id"]], 40),
        "book_id": intent["book_id"], "paper_order_intent_id": intent["paper_order_intent_id"],
        "client_order_id": client_order_id, "broker_order_id": broker_order_id,
        "account_fingerprint": account_fingerprint, "previous_state": previous, "new_state": new_state,
        "payload_hash": payload_hash, "quantity": intent["quantity"], "limit_price": intent["limit_price"],
        "operator": operator, "reason": reason, "runtime_request_id": runtime_request_id,
        "error_code": error_code, "created_at": now.isoformat(), "policy_version": POLICY_VERSION,
        "config_hash": intent["_external_config_hash"], "attempt_number": attempt_number,
        "scope_sequence": next_sequence,
    }
    inserted = repo.save_external_order_event(conn, event)
    if not inserted:
        raise ExternalPaperError(
            "EVENT_CHAIN_CONFLICT",
            "external order event insertion did not occur; a concurrent writer already claimed this transition",
        )
    return event


def _require_external_config(cfg: PaperBooksConfiguration, book_id: str, *, submission: bool = False) -> None:
    external = cfg.external_broker
    if not cfg.enabled:
        raise ExternalPaperError("PAPER_BOOKS_DISABLED", "paper_books.enabled is false")
    if not cfg.is_book_enabled(book_id):
        raise ExternalPaperError("BOOK_DISABLED", f"paper book {book_id} is not enabled")
    if not external.enabled:
        raise ExternalPaperError("EXTERNAL_BROKER_DISABLED", "external paper broker is disabled")
    if book_id not in external.enabled_book_ids:
        raise ExternalPaperError("BOOK_NOT_EXTERNAL_ENABLED", f"book {book_id} is not externally enabled")
    if len(external.enabled_book_ids) != 1:
        raise ExternalPaperError("ACCOUNT_ISOLATION_INVALID", "exactly one book must map to the paper account")
    if submission and not external.allow_order_submission:
        raise ExternalPaperError("SUBMISSION_DISABLED", "external paper order submission is disabled")


def _intent(conn: sqlite3.Connection, cfg: PaperBooksConfiguration, book_id: str, intent_id: str, now: datetime) -> dict:
    _require_external_config(cfg, book_id)
    intent = repo.load_order_intent(conn, book_id, intent_id)
    if intent is None:
        raise ExternalPaperError("INTENT_NOT_FOUND", f"paper intent {intent_id!r} was not found in book {book_id}")
    # Milestone 11.2 Part 9: the local simulator already refuses to fill an
    # intent once external evidence exists (has_external_execution_evidence
    # in execution.py). This is the reverse invariant — external preview/
    # submit/retry must refuse an intent whose local `paper_book_orders`
    # status is already terminal, whether that terminal state was reached
    # by a local fill/cancel/expire or by a prior external fill/cancel/
    # reject/expire (submit_external_paper_order itself writes those same
    # terminal strings back into this shared column, so a terminal status
    # always means "done" regardless of which namespace produced it — no
    # intent may ever be resubmitted once terminal). Non-terminal external
    # in-flight statuses (SUBMITTED, PARTIALLY_FILLED, etc.) remain eligible
    # so a legitimate retry after acquiring the order lease still works.
    if intent["status"] in TERMINAL_STATES:
        raise ExternalPaperError(
            "INTENT_NOT_ELIGIBLE_FOR_EXTERNAL",
            f"paper intent {intent_id!r} has terminal local status {intent['status']!r} — a terminal intent "
            "can never be (re)submitted, previewed, or retried externally",
        )
    book = repo.load_book(conn, book_id)
    if book is None or book.status != BOOK_STATUS_ACTIVE:
        raise ExternalPaperError("BOOK_INACTIVE", f"paper book {book_id} is not ACTIVE")
    risk = repo.load_risk_decision(conn, intent["risk_decision_id"])
    risk_approved = (
        risk is not None and risk["book_id"] == book_id and risk["decision"] in APPROVED_RISK_DECISIONS
    )
    if not risk_approved and intent["side"] == "SELL":
        exit_decision = repo.load_exit_decision(conn, intent["risk_decision_id"])
        risk_approved = bool(
            exit_decision and exit_decision["book_id"] == book_id
            and exit_decision["symbol"] == intent["symbol"]
            and exit_decision["decision"] in EXIT_DECISIONS
            and Decimal(exit_decision["quantity"]) == Decimal(intent["quantity"])
        )
    if not risk_approved:
        raise ExternalPaperError("INTENT_NOT_APPROVED", "paper intent has no matching approved risk/exit decision")
    if intent["order_type"] != "LIMIT" or "limit" not in cfg.external_broker.permitted_order_types:
        raise ExternalPaperError("ORDER_TYPE_NOT_ALLOWED", "external execution supports LIMIT orders only")
    if intent["time_in_force"].upper() != "DAY" or "day" not in cfg.external_broker.permitted_time_in_force:
        raise ExternalPaperError("TIME_IN_FORCE_NOT_ALLOWED", "external execution supports DAY only")
    quantity = Decimal(intent["quantity"])
    if quantity <= 0 or quantity != quantity.to_integral_value():
        raise ExternalPaperError("QUANTITY_NOT_WHOLE", "external quantity must be positive whole shares")
    if intent["side"] not in ("BUY", "SELL"):
        raise ExternalPaperError("SIDE_NOT_ALLOWED", "external execution is long-only BUY/closing SELL")
    _validate_frozen_notional(intent, cfg, risk)
    as_of = datetime.fromisoformat(intent["as_of"])
    if as_of.tzinfo is None:
        raise ExternalPaperError("INVALID_TIMESTAMP", "paper intent as_of must be timezone-aware")
    as_of = as_of.astimezone(timezone.utc)
    if as_of > now + _CLOCK_SKEW:
        raise ExternalPaperError("FUTURE_TIMESTAMP", "paper intent as_of is in the future")
    if now - as_of > timedelta(seconds=cfg.risk.reject_stale_market_price_seconds):
        raise ExternalPaperError("STALE_INTENT", "paper intent is stale for external submission")
    created_at = datetime.fromisoformat(intent["created_at"])
    if created_at.tzinfo is None:
        raise ExternalPaperError("INVALID_TIMESTAMP", "paper intent created_at must be timezone-aware")
    if created_at.astimezone(timezone.utc) > now + _CLOCK_SKEW:
        raise ExternalPaperError("FUTURE_TIMESTAMP", "paper intent created_at is in the future")
    if intent["side"] == "SELL":
        position = repo.load_position(conn, book_id, intent["symbol"])
        confirmed = Decimal(position["available_quantity"]) if position else Decimal("0")
        if quantity > confirmed:
            raise ExternalPaperError("OVERSELL", "SELL exceeds the book's confirmed available long position")
    intent["_external_config_hash"] = cfg.config_hash
    return intent


def _safety_checks(
    conn: sqlite3.Connection, book_id: str, *, allow_confirmed_not_found_retry: bool = False,
    retry_client_order_id: str | None = None,
) -> None:
    state = pause_mod.current_state(conn)
    if state.is_blocking:
        raise ExternalPaperError("SAFETY_PAUSE_ACTIVE", f"shadow safety state is {state.state}")
    critical = list_alerts(conn, severity="CRITICAL", unresolved_only=True, limit=1)
    if critical:
        raise ExternalPaperError("CRITICAL_ALERT_ACTIVE", "an unresolved CRITICAL operational alert exists")
    latest_by_scope = {}
    for reconciliation in repo.list_external_reconciliations(conn, book_id):
        latest_by_scope[reconciliation["client_order_id"] or "__book__"] = reconciliation
    active_critical = []
    for scope, reconciliation in latest_by_scope.items():
        allowed_retry_evidence = (
            allow_confirmed_not_found_retry and scope == retry_client_order_id
            and reconciliation["status"] == "ORDER_MISSING_AT_BROKER"
        )
        if reconciliation["critical"] and not allowed_retry_evidence:
            active_critical.append(reconciliation)
    if active_critical:
        raise ExternalPaperError("CRITICAL_RECONCILIATION_ACTIVE", "latest external reconciliation is critical")


def _account_check(runtime: ExternalPaperRuntime, book_id: str) -> dict:
    result = runtime.account_check(book_id)
    if set(result) != {
        "provider", "environment", "book_id", "account_fingerprint", "paper_endpoint_verified",
    }:
        raise ExternalPaperError("MALFORMED_RUNTIME_RESPONSE", "account-check response shape is invalid")
    if result["provider"] != "alpaca_paper" or result["environment"] != "paper":
        raise ExternalPaperError("NOT_PAPER_ENDPOINT", "runtime did not prove Alpaca paper environment")
    if result["book_id"] != book_id or result["paper_endpoint_verified"] is not True:
        raise ExternalPaperError("ACCOUNT_BOOK_MISMATCH", "runtime account check did not match the requested book")
    fingerprint = result["account_fingerprint"]
    if not isinstance(fingerprint, str) or not fingerprint.startswith("acct_") or len(fingerprint) > 80:
        raise ExternalPaperError("ACCOUNT_FINGERPRINT_INVALID", "runtime returned an invalid account fingerprint")
    return result


def check_external_paper_account(
    conn: sqlite3.Connection, *, book_id: str, runtime: ExternalPaperRuntime,
    config: PaperBooksConfiguration,
) -> dict:
    _require_external_config(config, book_id)
    return _account_check(runtime, book_id)


def _payload(intent: dict, client_order_id: str, payload_hash: str, fingerprint: str, now: datetime) -> dict:
    return {
        "book_id": intent["book_id"], "paper_order_intent_id": intent["paper_order_intent_id"],
        "client_order_id": client_order_id, "symbol": intent["symbol"], "side": intent["side"],
        "quantity": _exact_int(intent["quantity"], "intent quantity"), "limit_price": str(intent["limit_price"]),
        "time_in_force": "DAY", "asset_type": "equity", "extended_hours": False,
        "payload_hash": payload_hash, "account_fingerprint": fingerprint,
        "expires_at": (now + timedelta(seconds=300)).isoformat(),
    }


def _verify_fingerprint_history(conn: sqlite3.Connection, book_id: str, fingerprint: str) -> None:
    row = conn.execute(
        "SELECT account_fingerprint FROM paper_external_order_events WHERE book_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (book_id,),
    ).fetchone()
    if row is not None and row["account_fingerprint"] != fingerprint:
        raise ExternalPaperError("ACCOUNT_FINGERPRINT_MISMATCH", "external paper account fingerprint changed")
    other_book = conn.execute(
        "SELECT book_id FROM paper_external_order_events WHERE account_fingerprint = ? AND book_id <> ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (fingerprint, book_id),
    ).fetchone()
    if other_book is not None:
        raise ExternalPaperError(
            "ACCOUNT_ALREADY_MAPPED",
            f"this external paper account fingerprint is already mapped to book {other_book['book_id']}",
        )


# Fallback defaults when no config is supplied (kept for callers/tests that
# still invoke `_order_lease` without a `config=` — matches the pre-Part-10
# fixed 30s TTL exactly, so unmigrated call sites are unaffected).
_DEFAULT_ORDER_LEASE_TTL_SECONDS = 30
_DEFAULT_ORDER_LEASE_HEARTBEAT_SECONDS = 10


class OrderLeaseHandle:
    """Milestone 11.2 Part 10: a renewable, fenced order-scope lease.

    `heartbeat()` extends `expires_at` without releasing, so an operation
    whose runtime calls collectively exceed the original TTL can keep
    ownership as long as it heartbeats. `verify()` is a read-only fencing
    check a caller can perform immediately before a write it wants to gate
    on still holding the lease. Both fail closed (return False) once the
    lease has been reclaimed by another owner (its `generation` no longer
    matches) — a stale owner can never renew or gate a write past a
    takeover."""

    def __init__(self, conn: sqlite3.Connection, lease_key: str, owner_id: str, generation: int, ttl_seconds: int):
        self._conn = conn
        self.lease_key = lease_key
        self.owner_id = owner_id
        self.generation = generation
        self._ttl_seconds = ttl_seconds

    def heartbeat(self, now: datetime) -> bool:
        expires_at = (now + timedelta(seconds=self._ttl_seconds)).isoformat()
        return repo.heartbeat_external_order_lease(
            self._conn, lease_key=self.lease_key, owner_id=self.owner_id, generation=self.generation,
            now=now.isoformat(), expires_at=expires_at,
        )

    def verify(self, now: datetime) -> bool:
        return repo.verify_external_order_lease(
            self._conn, lease_key=self.lease_key, owner_id=self.owner_id, generation=self.generation,
            now=now.isoformat(),
        )


@contextlib.contextmanager
def _order_lease(
    conn: sqlite3.Connection, book_id: str, client_order_id: str, *, operation: str, now: datetime,
    config: PaperBooksConfiguration | None = None,
):
    """Atomic order-scope claim keyed by (book_id, client_order_id).

    Prevents concurrent preview/submit/retry/cancel/reconciliation calls on
    the same external order from forking the event chain: acquisition is a
    single conditional SQL write (`acquire_external_order_lease`), a stale
    lease (past `expires_at`) is recoverable by a new owner, and failure to
    acquire raises immediately rather than waiting. Released in `finally` so
    a raised exception never leaves the lease held. Yields an
    `OrderLeaseHandle` the caller may heartbeat around individual runtime
    calls when a single operation's total runtime-call time can approach or
    exceed the TTL.
    """
    ttl_seconds = _DEFAULT_ORDER_LEASE_TTL_SECONDS
    if config is not None:
        ttl_seconds = config.external_broker.order_lease_ttl_seconds
    lease_key = f"{book_id}:{client_order_id}"
    owner_id = f"call_{uuid.uuid4().hex}"
    expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
    generation = repo.acquire_external_order_lease(
        conn, lease_key=lease_key, book_id=book_id, client_order_id=client_order_id,
        owner_id=owner_id, operation=operation, now=now.isoformat(), expires_at=expires_at,
    )
    if generation is None:
        raise ExternalPaperError(
            "ORDER_LEASE_HELD", f"another operation holds the order-scope lease for {client_order_id!r}",
        )
    handle = OrderLeaseHandle(conn, lease_key, owner_id, generation, ttl_seconds)
    try:
        yield handle
    finally:
        repo.release_external_order_lease(
            conn, lease_key=lease_key, owner_id=owner_id, now=now.isoformat(), generation=generation,
        )


def preview_external_paper_order(
    conn: sqlite3.Connection, *, book_id: str, paper_order_intent_id: str, operator: str,
    runtime: ExternalPaperRuntime, config: PaperBooksConfiguration, clock=None,
) -> dict:
    now = _now(clock)
    operator = _bounded(operator, "operator", 128)
    intent = _intent(conn, config, book_id, paper_order_intent_id, now)
    _safety_checks(conn, book_id)
    client_order_id, payload_hash = derive_external_order_identity(intent)
    with _order_lease(conn, book_id, client_order_id, operation="PREVIEW", now=now, config=config) as lease:
        current = _current_event(conn, book_id, client_order_id)
        if current and current["new_state"] not in (STATE_PREVIEWED,):
            raise ExternalPaperError("ORDER_ALREADY_EXTERNAL", f"external order is already {current['new_state']}")
        account = _account_check(runtime, book_id)
        fingerprint = account["account_fingerprint"]
        _verify_fingerprint_history(conn, book_id, fingerprint)
        lease.heartbeat(now)
        runtime_result = runtime.preview_limit_order(
            _payload(intent, client_order_id, payload_hash, fingerprint, now)
        )
        if not isinstance(runtime_result, dict) or set(runtime_result) != {
            "provider", "environment", "book_id", "client_order_id", "account_fingerprint", "result", "reasons",
        }:
            raise ExternalPaperError("MALFORMED_RUNTIME_RESPONSE", "runtime preview response shape is invalid")
        if (
            runtime_result.get("provider") != "alpaca_paper" or runtime_result.get("environment") != "paper"
            or runtime_result.get("book_id") != book_id or runtime_result.get("client_order_id") != client_order_id
            or not isinstance(runtime_result.get("reasons"), list)
            or runtime_result.get("result") != "APPROVED" or runtime_result.get("account_fingerprint") != fingerprint
        ):
            raise ExternalPaperError("RUNTIME_PREVIEW_REJECTED", "isolated runtime rejected the paper preflight")
        expires = now + timedelta(seconds=config.external_broker.require_recent_preview_seconds)
        preview_id = _digest("pepv_", [book_id, paper_order_intent_id, payload_hash, fingerprint, now.isoformat()], 40)
        record = {
            "preview_id": preview_id, "paper_order_intent_id": paper_order_intent_id,
            "payload_hash": payload_hash, "book_id": book_id, "client_order_id": client_order_id,
            "account_fingerprint": fingerprint, "previewed_at": now.isoformat(), "expires_at": expires.isoformat(),
            "operator": operator, "result": "APPROVED", "reasons": (), "config_hash": config.config_hash,
            "policy_version": POLICY_VERSION,
        }
        repo.save_external_preview(conn, record)
        _append_event(
            conn, intent=intent, client_order_id=client_order_id, payload_hash=payload_hash,
            account_fingerprint=fingerprint, new_state=STATE_PREVIEWED, operator=operator,
            reason="explicit external paper preview approved", now=now,
        )
        return record


def _validated_preview(
    conn: sqlite3.Connection, *, preview_id: str, intent: dict, client_order_id: str,
    payload_hash: str, fingerprint: str, now: datetime, config: PaperBooksConfiguration,
) -> dict:
    preview = repo.load_external_preview(conn, preview_id)
    if preview is None:
        raise ExternalPaperError("PREVIEW_NOT_FOUND", f"preview {preview_id!r} was not found")
    if preview["result"] != "APPROVED":
        raise ExternalPaperError("PREVIEW_FAILED", "preview was not approved")
    expected = (intent["book_id"], intent["paper_order_intent_id"], client_order_id, payload_hash, fingerprint)
    actual = (
        preview["book_id"], preview["paper_order_intent_id"], preview["client_order_id"],
        preview["payload_hash"], preview["account_fingerprint"],
    )
    if actual != expected:
        raise ExternalPaperError("PREVIEW_PAYLOAD_DRIFT", "preview does not match the frozen order/account payload")
    if datetime.fromisoformat(preview["expires_at"]) < now:
        raise ExternalPaperError("PREVIEW_EXPIRED", "external paper preview has expired")
    if preview["config_hash"] != config.config_hash:
        raise ExternalPaperError("PREVIEW_CONFIG_DRIFT", "configuration changed after preview")
    return preview


def _state_from_order(order: dict) -> str:
    mapping = {
        "ACCEPTED": STATE_SUBMITTED, "SUBMITTED": STATE_SUBMITTED, "NEW": STATE_SUBMITTED,
        "PARTIALLY_FILLED": STATE_PARTIALLY_FILLED, "FILLED": STATE_FILLED,
        "CANCEL_REQUESTED": STATE_CANCEL_REQUESTED,
        "CANCELLED": STATE_CANCELLED, "CANCELED": STATE_CANCELLED,
        "REJECTED": STATE_REJECTED, "EXPIRED": STATE_EXPIRED,
    }
    status = str(order.get("status", "")).upper()
    if status not in mapping:
        raise ExternalPaperError("UNKNOWN_BROKER_STATUS", f"unknown normalized broker status {status!r}")
    return mapping[status]


_DUPLICATE_WINDOW_SECONDS = 300


def _detect_duplicate_broker_order(
    runtime: ExternalPaperRuntime, *, book_id: str, intent: dict, client_order_id: str, order: dict,
) -> dict:
    """Bounded, offline-safe duplicate check across the *full* recent-order
    result (Milestone 11.2 Part 15): compares every recent broker order
    against the frozen external intent, regardless of whether its
    client_order_id carries this project's `epb-{book_id}-` prefix. A
    manually-created Alpaca order, or one placed by an unrelated
    application against the same paper account, never carries that prefix
    — skipping non-prefixed candidates (the pre-Part-15 behavior) made
    exactly those conflicts undetectable. Malformed or oversized
    recent-order results now raise (fail closed) rather than silently
    reporting "no duplicate"; `_reconcile_locked`'s outer wrapper persists
    that as a critical `RECONCILIATION_INTERNAL_ERROR`. Returns a bounded,
    non-secret details dict (never a raw broker object) when a duplicate is
    found, or an empty dict otherwise. Never flags an ordinary unrelated
    order (different symbol/side/quantity/price or outside the time
    window).
    """
    try:
        recent = runtime.list_recent_external_orders(book_id, limit=100)
    except AttributeError:
        return {}  # runtime does not implement this optional capability
    if not isinstance(recent, list) or len(recent) > 200:
        raise ExternalPaperError(
            "MALFORMED_RUNTIME_RESPONSE", "runtime recent-orders response is invalid or unbounded",
        )
    try:
        own_submitted_at = datetime.fromisoformat(str(order["submitted_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError):
        own_submitted_at = None
    same_client_id_other_broker_ids: set[str] = set()
    duplicate_same_client_id: str | None = None
    duplicate_manual_or_foreign_client_id: str | None = None
    own_prefix = f"epb-{book_id.lower()}-"
    for candidate in recent:
        if not isinstance(candidate, dict):
            raise ExternalPaperError(
                "MALFORMED_RUNTIME_RESPONSE", "runtime recent-orders entry is not a mapping",
            )
        candidate_client_id = candidate.get("client_order_id")
        candidate_broker_id = candidate.get("broker_order_id")
        if not isinstance(candidate_client_id, str) or not candidate_client_id:
            raise ExternalPaperError(
                "MALFORMED_RUNTIME_RESPONSE", "runtime recent-orders entry has an invalid client_order_id",
            )
        if candidate_client_id == client_order_id:
            if candidate_broker_id and candidate_broker_id != order.get("broker_order_id"):
                same_client_id_other_broker_ids.add(str(candidate_broker_id))
            continue
        try:
            same_shape = (
                candidate.get("symbol") == intent["symbol"] and candidate.get("side") == intent["side"]
                and Decimal(str(candidate.get("quantity"))) == Decimal(intent["quantity"])
                and Decimal(str(candidate.get("limit_price"))) == Decimal(intent["limit_price"])
            )
        except (InvalidOperation, TypeError, KeyError):
            same_shape = False
        if not same_shape or own_submitted_at is None:
            continue
        try:
            candidate_time = datetime.fromisoformat(str(candidate.get("submitted_at", "")).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if abs((candidate_time - own_submitted_at).total_seconds()) <= _DUPLICATE_WINDOW_SECONDS:
            if candidate_client_id.startswith(own_prefix):
                duplicate_same_client_id = str(candidate_client_id)
            else:
                duplicate_manual_or_foreign_client_id = str(candidate_client_id)
    if same_client_id_other_broker_ids:
        return {
            "duplicate_broker_order_ids": sorted(same_client_id_other_broker_ids)[:5],
            "duplicate_reason": "same client_order_id mapped to more than one broker order",
        }
    if duplicate_same_client_id is not None:
        return {
            "duplicate_client_order_id": duplicate_same_client_id,
            "duplicate_reason": "materially identical order under a different client_order_id",
        }
    if duplicate_manual_or_foreign_client_id is not None:
        return {
            "duplicate_client_order_id": duplicate_manual_or_foreign_client_id,
            "duplicate_reason": (
                "materially identical order under a client_order_id outside this project's namespace "
                "(manually created, or placed by another application against this paper account)"
            ),
        }
    return {}


def _validate_order_response(
    order: dict, intent: dict, client_order_id: str, fingerprint: str, now: datetime,
) -> None:
    expected_fields = {
        "provider", "environment", "account_fingerprint", "book_id", "client_order_id",
        "broker_order_id", "symbol", "side", "quantity", "limit_price", "time_in_force",
        "status", "submitted_at", "updated_at", "filled_quantity", "average_fill_price",
        "rejection_code",
    }
    if not isinstance(order, dict) or set(order) != expected_fields:
        raise ExternalPaperError("MALFORMED_RUNTIME_RESPONSE", "runtime order response shape is invalid")
    if order.get("provider") != "alpaca_paper" or order.get("environment") != "paper":
        raise ExternalPaperError("NOT_PAPER_ENDPOINT", "runtime order response is not paper scoped")
    if order.get("client_order_id") != client_order_id:
        raise ExternalPaperError("MALFORMED_RUNTIME_RESPONSE", "runtime order response has wrong client order ID")
    for key in ("broker_order_id", "status"):
        if not order.get(key):
            raise ExternalPaperError("MALFORMED_RUNTIME_RESPONSE", f"runtime order response lacks {key}")
    for key, expected in (
        ("book_id", intent["book_id"]), ("symbol", intent["symbol"]), ("side", intent["side"]),
        ("account_fingerprint", fingerprint),
    ):
        if order.get(key) != expected:
            raise ExternalPaperError("BROKER_ORDER_MISMATCH", f"broker order {key} does not match approved intent")
    try:
        quantity = Decimal(str(order["quantity"]))
        limit_price = Decimal(str(order["limit_price"]))
        filled_quantity = Decimal(str(order["filled_quantity"]))
        submitted_at = datetime.fromisoformat(str(order["submitted_at"]).replace("Z", "+00:00"))
        updated_at = datetime.fromisoformat(str(order["updated_at"]).replace("Z", "+00:00"))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ExternalPaperError("MALFORMED_RUNTIME_RESPONSE", "runtime order values are malformed") from exc
    if not quantity.is_finite() or not limit_price.is_finite() or not filled_quantity.is_finite():
        raise ExternalPaperError("MALFORMED_RUNTIME_RESPONSE", "runtime order values must be finite")
    if quantity != quantity.to_integral_value() or filled_quantity != filled_quantity.to_integral_value():
        raise ExternalPaperError("MALFORMED_RUNTIME_RESPONSE", "runtime order quantities must be whole numbers")
    if quantity != Decimal(intent["quantity"]):
        raise ExternalPaperError("BROKER_ORDER_MISMATCH", "broker order quantity does not match approved intent")
    if limit_price != Decimal(intent["limit_price"]):
        raise ExternalPaperError("BROKER_ORDER_MISMATCH", "broker order limit price does not match approved intent")
    if order.get("time_in_force") != "DAY":
        raise ExternalPaperError("BROKER_ORDER_MISMATCH", "broker order time-in-force is not DAY")
    if filled_quantity < 0 or filled_quantity > Decimal(intent["quantity"]):
        raise ExternalPaperError("BROKER_ORDER_MISMATCH", "broker filled quantity exceeds approved quantity")
    if submitted_at.tzinfo is None or updated_at.tzinfo is None:
        raise ExternalPaperError("MALFORMED_RUNTIME_RESPONSE", "runtime order timestamps must be timezone aware")
    submitted_at, updated_at = submitted_at.astimezone(timezone.utc), updated_at.astimezone(timezone.utc)
    if submitted_at > now + _CLOCK_SKEW or updated_at > now + _CLOCK_SKEW:
        raise ExternalPaperError("FUTURE_TIMESTAMP", "runtime order timestamp is in the future")
    if submitted_at > updated_at:
        raise ExternalPaperError("MALFORMED_RUNTIME_RESPONSE", "broker order submitted_at is after updated_at")


def _record_unknown(
    conn, *, intent, client_order_id, payload_hash, fingerprint, operator, reason, now,
    runtime_request_id, error_code, attempt_number,
) -> dict:
    return _append_event(
        conn, intent=intent, client_order_id=client_order_id, payload_hash=payload_hash,
        account_fingerprint=fingerprint, new_state=STATE_UNKNOWN, operator=operator, reason=reason,
        now=now, runtime_request_id=runtime_request_id, error_code=error_code,
        attempt_number=attempt_number,
    )


def _submit_once(
    conn, *, intent, client_order_id, payload_hash, fingerprint, operator, reason, runtime,
    config, now, attempt_number,
) -> dict:
    runtime_request_id = f"m11_{uuid.uuid4().hex}"
    reservation_inserted = False
    share_reservation_inserted = False
    if intent["side"] == "BUY":
        reservation_inserted = cash_ledger.reserve_for_order(
            conn, intent["book_id"], intent["paper_order_intent_id"], Decimal(intent["notional_usd"]), now,
        )
    else:
        share_reservation_inserted = positions.reserve_shares_for_sell(
            conn, intent["book_id"], intent["symbol"], intent["paper_order_intent_id"], client_order_id,
            Decimal(intent["quantity"]), now,
        )
    try:
        _append_event(
            conn, intent=intent, client_order_id=client_order_id, payload_hash=payload_hash,
            account_fingerprint=fingerprint, new_state=STATE_SUBMISSION_REQUESTED, operator=operator,
            reason=reason, now=now, runtime_request_id=runtime_request_id, attempt_number=attempt_number,
        )
    except Exception:
        if reservation_inserted:
            cash_ledger.release_reservation(
                conn, intent["book_id"], intent["paper_order_intent_id"], Decimal(intent["notional_usd"]),
                now, reason="submission-event-failed",
            )
        if share_reservation_inserted:
            positions.release_remaining_share_reservation(
                conn, intent["book_id"], intent["symbol"], intent["paper_order_intent_id"], now,
                release_event_id="submission-event-failed", event_type="RELEASED_CANCELLED",
            )
        raise
    try:
        order = runtime.submit_limit_order(_payload(intent, client_order_id, payload_hash, fingerprint, now))
        _validate_order_response(order, intent, client_order_id, fingerprint, now)
        new_state = _state_from_order(order)
    except Exception as exc:
        code = getattr(exc, "code", "RUNTIME_OUTCOME_UNKNOWN")
        event = _record_unknown(
            conn, intent=intent, client_order_id=client_order_id, payload_hash=payload_hash,
            fingerprint=fingerprint, operator=operator,
            reason="runtime submission outcome is ambiguous; broker lookup required", now=now,
            runtime_request_id=runtime_request_id, error_code=str(code), attempt_number=attempt_number,
        )
        return {"status": STATE_UNKNOWN, "event": event, "error_code": str(code)}
    event = _append_event(
        conn, intent=intent, client_order_id=client_order_id, payload_hash=payload_hash,
        account_fingerprint=fingerprint, new_state=new_state, operator=operator,
        reason="normalized broker order response", now=now, broker_order_id=order["broker_order_id"],
        runtime_request_id=runtime_request_id, attempt_number=attempt_number,
    )
    repo.update_order_status(conn, intent["book_id"], intent["paper_order_intent_id"], new_state)
    order_submitted_at = datetime.fromisoformat(str(order["submitted_at"]).replace("Z", "+00:00")).astimezone(timezone.utc)
    # Milestone 11.2 Part 12: the broker event above is already persisted —
    # no fill-related failure past this point may escape without persisted
    # critical reconciliation evidence. Never let an unprotected fill sweep
    # raise straight out of a successful submission with zero DB trace.
    fill_error_codes = {
        "MALFORMED_FILL", "FILL_QUANTITY_INVALID", "FILL_NAMESPACE_MISMATCH",
        "FILL_PRICE_INVALID", "MALFORMED_RUNTIME_RESPONSE", "FUTURE_TIMESTAMP",
    }
    try:
        fills = apply_external_fills(
            conn, intent=intent, client_order_id=client_order_id, payload_hash=payload_hash,
            fingerprint=fingerprint, runtime=runtime, now=now, not_before=order_submitted_at,
        )
    except ExternalPaperError as exc:
        _persist_reconciliation(
            conn, book_id=intent["book_id"], intent=intent, client_order_id=client_order_id,
            fingerprint=fingerprint,
            statuses=("MALFORMED_BROKER_FILL" if exc.code in fill_error_codes else "FILL_APPLICATION_FAILED",),
            details={"stage": "post_submit_fill_sweep"}, now=now, config=config,
        )
        raise
    except Exception:
        _persist_reconciliation(
            conn, book_id=intent["book_id"], intent=intent, client_order_id=client_order_id,
            fingerprint=fingerprint, statuses=("FILL_APPLICATION_FAILED",),
            details={"stage": "post_submit_fill_sweep"}, now=now, config=config,
        )
        raise
    _release_terminal_reservation(conn, intent, new_state, now)
    return {"status": new_state, "event": event, "order": order, "new_fills": fills}


_RELEASE_EVENT_TYPE_FOR_STATE = {
    STATE_CANCELLED: "RELEASED_CANCELLED", STATE_REJECTED: "RELEASED_REJECTED", STATE_EXPIRED: "RELEASED_EXPIRED",
}


def _release_terminal_reservation(conn, intent: dict, state: str, now: datetime) -> None:
    """Release whatever remains reserved once the broker confirms no more fills will arrive.

    FILLED is deliberately excluded here: it is only ever released inside
    ``apply_external_fills`` once the full approved quantity is durably
    applied locally, so an empty or delayed fill response for a broker-FILLED
    order leaves the reservation intact rather than releasing it on trust.
    """
    if state not in _RELEASE_EVENT_TYPE_FOR_STATE:
        return
    if intent["side"] == "BUY":
        cash_ledger.release_remaining_buy_reservation(
            conn, intent["book_id"], intent["paper_order_intent_id"], now, release_event_id="terminal-closed",
        )
    else:
        positions.release_remaining_share_reservation(
            conn, intent["book_id"], intent["symbol"], intent["paper_order_intent_id"], now,
            release_event_id="terminal-closed", event_type=_RELEASE_EVENT_TYPE_FOR_STATE[state],
        )


def submit_external_paper_order(
    conn: sqlite3.Connection, *, book_id: str, paper_order_intent_id: str, preview_id: str,
    operator: str, reason: str, runtime: ExternalPaperRuntime, config: PaperBooksConfiguration, clock=None,
) -> dict:
    now = _now(clock)
    operator, reason = _bounded(operator, "operator", 128), _bounded(reason, "reason", 512)
    _require_external_config(config, book_id, submission=True)
    intent = _intent(conn, config, book_id, paper_order_intent_id, now)
    _safety_checks(conn, book_id)
    client_order_id, payload_hash = derive_external_order_identity(intent)
    with _order_lease(conn, book_id, client_order_id, operation="SUBMIT", now=now, config=config) as lease:
        account = _account_check(runtime, book_id)
        fingerprint = account["account_fingerprint"]
        _verify_fingerprint_history(conn, book_id, fingerprint)
        _validated_preview(
            conn, preview_id=preview_id, intent=intent, client_order_id=client_order_id,
            payload_hash=payload_hash, fingerprint=fingerprint, now=now, config=config,
        )
        current = _current_event(conn, book_id, client_order_id)
        if current and current["new_state"] == STATE_UNKNOWN:
            raise ExternalPaperError("AMBIGUOUS_SUBMISSION", "broker lookup is required before any retry")
        if current and current["new_state"] not in (STATE_PREVIEWED,):
            lease.heartbeat(now)
            order = runtime.get_order_by_client_order_id(book_id, client_order_id)
            if order is None:
                raise ExternalPaperError("ORDER_MISSING_AT_BROKER", "existing external order was not found at broker")
            return {"status": current["new_state"], "event": current, "order": order, "duplicate_submit": False}
        lease.heartbeat(now)
        result = _submit_once(
            conn, intent=intent, client_order_id=client_order_id, payload_hash=payload_hash,
            fingerprint=fingerprint, operator=operator, reason=reason, runtime=runtime,
            config=config, now=now, attempt_number=0,
        )
        if result["status"] != STATE_UNKNOWN:
            lease.heartbeat(now)
            result["reconciliation"] = _reconcile_locked(
                conn, book_id=book_id, client_order_id=client_order_id,
                runtime=runtime, config=config, now=now,
            )
        return result


def apply_external_fills(
    conn: sqlite3.Connection, *, intent: dict, client_order_id: str, payload_hash: str,
    fingerprint: str, runtime: ExternalPaperRuntime, now: datetime,
    not_before: datetime | None = None,
) -> list[dict]:
    try:
        fills = runtime.list_order_fills(intent["book_id"], client_order_id)
    except AttributeError:
        return []
    if not isinstance(fills, list) or len(fills) > 1_000:
        raise ExternalPaperError("MALFORMED_RUNTIME_RESPONSE", "runtime fills response is invalid or oversized")
    existing_fills = repo.list_fills_for_intent(
        conn, intent["book_id"], intent["paper_order_intent_id"]
    )
    existing_total = sum((Decimal(fill["fill_quantity"]) for fill in existing_fills), Decimal("0"))
    existing_notional = sum(
        (Decimal(fill["fill_quantity"]) * Decimal(fill["fill_price"]) for fill in existing_fills),
        Decimal("0"),
    )
    approved = Decimal(intent["quantity"])
    applied = []
    for fill in fills:
        if not isinstance(fill, dict) or set(fill) != {
            "fill_id", "broker_order_id", "client_order_id", "book_id", "symbol", "side",
            "quantity", "price", "filled_at", "account_fingerprint",
        }:
            raise ExternalPaperError("MALFORMED_FILL", "runtime fill response shape is invalid")
        external_identity = str(fill.get("fill_id", ""))
        if not external_identity or len(external_identity) > 256 or not str(fill.get("broker_order_id", "")):
            raise ExternalPaperError("MALFORMED_FILL", "runtime fill identity is missing or oversized")
        external_fill_id = _digest("pebf_", [intent["book_id"], external_identity], 40)
        local_fill_id = f"external:{external_fill_id}"
        if repo.fill_exists(conn, intent["book_id"], local_fill_id):
            continue
        try:
            quantity, price = Decimal(str(fill["quantity"])), Decimal(str(fill["price"]))
        except (KeyError, InvalidOperation) as exc:
            raise ExternalPaperError("MALFORMED_FILL", "runtime fill has invalid quantity/price") from exc
        if not quantity.is_finite() or not price.is_finite():
            raise ExternalPaperError("MALFORMED_FILL", "runtime fill quantity/price must be finite")
        if quantity != quantity.to_integral_value():
            raise ExternalPaperError("MALFORMED_FILL", "runtime fill quantity must be a whole number")
        if external_identity.startswith("alpaca-cumulative-"):
            cumulative_quantity = quantity
            if cumulative_quantity <= existing_total:
                continue
            cumulative_notional = cumulative_quantity * price
            quantity = cumulative_quantity - existing_total
            delta_notional = cumulative_notional - existing_notional
            if delta_notional <= 0:
                raise ExternalPaperError("FILL_PRICE_INVALID", "cumulative fill notional did not advance")
            price = delta_notional / quantity
        if quantity <= 0 or price <= 0 or existing_total + quantity > approved:
            raise ExternalPaperError("FILL_QUANTITY_INVALID", "fill is non-positive or exceeds approved quantity")
        for key, expected in (
            ("book_id", intent["book_id"]), ("client_order_id", client_order_id),
            ("symbol", intent["symbol"]), ("side", intent["side"]),
            ("account_fingerprint", fingerprint),
        ):
            if fill.get(key) != expected:
                raise ExternalPaperError("FILL_NAMESPACE_MISMATCH", f"fill {key} does not match approved order")
        filled_at = datetime.fromisoformat(str(fill["filled_at"]).replace("Z", "+00:00"))
        if filled_at.tzinfo is None:
            raise ExternalPaperError("MALFORMED_FILL", "fill timestamp must be timezone aware")
        filled_at = filled_at.astimezone(timezone.utc)
        if filled_at > now + _CLOCK_SKEW:
            raise ExternalPaperError("FUTURE_TIMESTAMP", "fill timestamp is in the future")
        if not_before is not None and filled_at < not_before - _CLOCK_SKEW:
            raise ExternalPaperError("MALFORMED_FILL", "fill timestamp precedes the order's own submission")
        record = {
            "external_fill_id": external_fill_id, "book_id": intent["book_id"],
            "paper_order_intent_id": intent["paper_order_intent_id"], "client_order_id": client_order_id,
            "broker_order_id": str(fill["broker_order_id"]), "account_fingerprint": fingerprint,
            "symbol": intent["symbol"], "side": intent["side"], "quantity": quantity, "price": price,
            "filled_at": filled_at.astimezone(timezone.utc).isoformat(),
            "payload_hash": payload_hash, "created_at": now.isoformat(),
        }
        local = {
            "book_id": intent["book_id"], "fill_id": local_fill_id,
            "paper_order_intent_id": intent["paper_order_intent_id"], "symbol": intent["symbol"],
            "side": intent["side"], "simulated_market_price": price,
            "limit_price": Decimal(intent["limit_price"]), "fill_quantity": quantity, "fill_price": price,
            "fees_usd": Decimal("0"), "slippage_usd": Decimal("0"), "fill_timestamp": filled_at,
            "simulation_rule_version": POLICY_VERSION,
        }
        try:
            begin_immediate(conn)
            repo.save_external_broker_fill(conn, record, commit=False)
            inserted = repo.save_fill(conn, local, commit=False)
            if inserted:
                if intent["side"] == "BUY":
                    positions.apply_buy_fill(
                        conn, intent["book_id"], intent["symbol"], local_fill_id, quantity, price,
                        filled_at, commit=False,
                    )
                    cash_ledger.settle_buy(
                        conn, intent["book_id"], local_fill_id, quantity * price, Decimal("0"),
                        Decimal("0"), filled_at, commit=False,
                    )
                    cash_ledger.release_settled_buy_reservation(
                        conn, intent["book_id"], intent["paper_order_intent_id"], local_fill_id,
                        quantity * price, filled_at, commit=False,
                    )
                else:
                    positions.apply_sell_fill(
                        conn, intent["book_id"], intent["symbol"], local_fill_id, quantity, price,
                        filled_at, commit=False, already_reserved=True,
                    )
                    cash_ledger.settle_sell(
                        conn, intent["book_id"], local_fill_id, quantity * price, Decimal("0"),
                        Decimal("0"), filled_at, commit=False,
                    )
                    positions.consume_share_reservation_for_fill(
                        conn, intent["book_id"], intent["symbol"], intent["paper_order_intent_id"],
                        local_fill_id, quantity, filled_at, commit=False,
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if inserted:
            existing_total += quantity
            existing_notional += quantity * price
            applied.append(record)
    current = _current_event(conn, intent["book_id"], client_order_id)
    if existing_total > 0 and current and current["new_state"] not in TERMINAL_STATES:
        state = STATE_FILLED if existing_total == approved else STATE_PARTIALLY_FILLED
        if state != current["new_state"]:
            _append_event(
                conn, intent=intent, client_order_id=client_order_id, payload_hash=payload_hash,
                account_fingerprint=fingerprint, new_state=state, operator="SYSTEM_RECONCILIATION",
                reason="authoritative normalized broker fills applied", now=now,
                broker_order_id=current.get("broker_order_id"), attempt_number=current.get("attempt_number", 0),
            )
            repo.update_order_status(conn, intent["book_id"], intent["paper_order_intent_id"], state)
    if existing_total == approved:
        if intent["side"] == "BUY":
            cash_ledger.release_remaining_buy_reservation(
                conn, intent["book_id"], intent["paper_order_intent_id"], now, release_event_id="fully-filled",
            )
        else:
            positions.release_remaining_share_reservation(
                conn, intent["book_id"], intent["symbol"], intent["paper_order_intent_id"], now,
                release_event_id="fully-filled", event_type="CONSUMED_BY_FILL",
            )
    return applied


def retry_external_paper_order(
    conn: sqlite3.Connection, *, book_id: str, paper_order_intent_id: str, operator: str, reason: str,
    runtime: ExternalPaperRuntime, config: PaperBooksConfiguration, clock=None,
) -> dict:
    now = _now(clock)
    operator, reason = _bounded(operator, "operator", 128), _bounded(reason, "reason", 512)
    _require_external_config(config, book_id, submission=True)
    intent = _intent(conn, config, book_id, paper_order_intent_id, now)
    client_order_id, payload_hash = derive_external_order_identity(intent)
    with _order_lease(conn, book_id, client_order_id, operation="RETRY", now=now, config=config):
        current = _current_event(conn, book_id, client_order_id)
        if current is None or current["new_state"] != STATE_UNKNOWN:
            raise ExternalPaperError("RETRY_NOT_ALLOWED", "retry requires an ambiguous submission state")
        lookup = repo.load_latest_external_lookup(conn, book_id, client_order_id)
        if (
            lookup is None or lookup["result"] != "NOT_FOUND" or not lookup["authoritative"]
            or lookup.get("consumed_by_retry_event_id") is not None
            or lookup.get("ambiguous_event_id") != current["external_order_event_id"]
            or lookup.get("attempt_number") != current["attempt_number"]
            or lookup.get("payload_hash") != current["payload_hash"]
            or lookup.get("account_fingerprint") != current["account_fingerprint"]
        ):
            raise ExternalPaperError(
                "NOT_FOUND_NOT_CONFIRMED",
                "fresh, unconsumed authoritative broker NOT_FOUND evidence for this exact ambiguous attempt is required",
            )
        _safety_checks(
            conn, book_id, allow_confirmed_not_found_retry=True, retry_client_order_id=client_order_id,
        )
        retries = max(event["attempt_number"] for event in repo.list_external_order_events(
            conn, book_id=book_id, client_order_id=client_order_id,
        ))
        if retries >= config.external_broker.maximum_retry_attempts:
            raise ExternalPaperError("RETRY_LIMIT_REACHED", "external submission retry limit reached")
        account = _account_check(runtime, book_id)
        fingerprint = account["account_fingerprint"]
        if fingerprint != current["account_fingerprint"]:
            raise ExternalPaperError("ACCOUNT_FINGERPRINT_MISMATCH", "account changed before retry")
        preview = conn.execute(
            "SELECT preview_id FROM paper_external_order_previews WHERE book_id = ? AND paper_order_intent_id = ? "
            "AND result = 'APPROVED' ORDER BY previewed_at DESC LIMIT 1", (book_id, paper_order_intent_id),
        ).fetchone()
        if preview is None:
            raise ExternalPaperError("PREVIEW_NOT_FOUND", "retry requires a matching explicit preview")
        _validated_preview(
            conn, preview_id=preview["preview_id"], intent=intent, client_order_id=client_order_id,
            payload_hash=payload_hash, fingerprint=fingerprint, now=now, config=config,
        )
        result = _submit_once(
            conn, intent=intent, client_order_id=client_order_id, payload_hash=payload_hash,
            fingerprint=fingerprint, operator=operator, reason=reason, runtime=runtime, config=config,
            now=now, attempt_number=retries + 1,
        )
        repo.consume_external_lookup(conn, lookup["lookup_id"], result["event"]["external_order_event_id"])
        if result["status"] != STATE_UNKNOWN:
            result["reconciliation"] = _reconcile_locked(
                conn, book_id=book_id, client_order_id=client_order_id,
                runtime=runtime, config=config, now=now,
            )
        return result


def refresh_retry_preview(
    conn: sqlite3.Connection, *, book_id: str, paper_order_intent_id: str, operator: str, reason: str,
    config: PaperBooksConfiguration, clock=None,
) -> dict:
    """Milestone 11.2 Part 17: an explicit, read-only operator action that
    replaces an *expired* preview for an order already confirmed
    `UNKNOWN_REQUIRES_RECONCILIATION` with a fresh, authoritative
    `NOT_FOUND` lookup — without which a confirmed-safe-to-retry order
    could become permanently stuck once its original preview's TTL lapses.

    Makes no broker/runtime call whatsoever (pure DB read + a new preview
    row) and never consumes the authoritative lookup — only an actual
    `retry_external_paper_order` call consumes it, and that call still runs
    every one of its own checks (order lease, retry limit, account
    fingerprint, frozen preview/payload match) against the fresh preview
    this creates. This action cannot itself submit anything.
    """
    now = _now(clock)
    operator, reason = _bounded(operator, "operator", 128), _bounded(reason, "reason", 512)
    _require_external_config(config, book_id, submission=True)
    intent = _intent(conn, config, book_id, paper_order_intent_id, now)
    client_order_id, payload_hash = derive_external_order_identity(intent)
    with _order_lease(conn, book_id, client_order_id, operation="REFRESH_RETRY_PREVIEW", now=now, config=config):
        current = _current_event(conn, book_id, client_order_id)
        # Refresh is only for an order still ambiguous; once a broker order
        # has actually been found (reconciliation moves the chain off
        # UNKNOWN), there is nothing left to refresh a preview for.
        if current is None or current["new_state"] != STATE_UNKNOWN:
            raise ExternalPaperError(
                "REFRESH_NOT_ALLOWED", "refresh requires the order to be in UNKNOWN_REQUIRES_RECONCILIATION",
            )
        lookup = repo.load_latest_external_lookup(conn, book_id, client_order_id)
        if (
            lookup is None or lookup["result"] != "NOT_FOUND" or not lookup["authoritative"]
            or lookup.get("consumed_by_retry_event_id") is not None
            or lookup.get("ambiguous_event_id") != current["external_order_event_id"]
            or lookup.get("attempt_number") != current["attempt_number"]
            or lookup.get("payload_hash") != current["payload_hash"]
            or lookup.get("account_fingerprint") != current["account_fingerprint"]
        ):
            raise ExternalPaperError(
                "NOT_FOUND_NOT_CONFIRMED",
                "fresh, unconsumed authoritative broker NOT_FOUND evidence for this exact ambiguous attempt is required",
            )
        retries = max(event["attempt_number"] for event in repo.list_external_order_events(
            conn, book_id=book_id, client_order_id=client_order_id,
        ))
        if retries >= config.external_broker.maximum_retry_attempts:
            raise ExternalPaperError("RETRY_LIMIT_REACHED", "external submission retry limit reached")
        expires = now + timedelta(seconds=config.external_broker.require_recent_preview_seconds)
        preview_id = _digest(
            "pepv_", [book_id, paper_order_intent_id, payload_hash, lookup["account_fingerprint"], now.isoformat(), "refresh"], 40,
        )
        record = {
            "preview_id": preview_id, "paper_order_intent_id": paper_order_intent_id,
            "payload_hash": payload_hash, "book_id": book_id, "client_order_id": client_order_id,
            "account_fingerprint": lookup["account_fingerprint"], "previewed_at": now.isoformat(),
            "expires_at": expires.isoformat(), "operator": operator, "result": "APPROVED",
            "reasons": (
                f"refresh: {reason}",
                f"ambiguous_event_id={current['external_order_event_id']}",
                f"authoritative_lookup_id={lookup['lookup_id']}",
            ),
            "config_hash": config.config_hash, "policy_version": POLICY_VERSION,
        }
        repo.save_external_preview(conn, record)
        return record


def cancel_external_paper_order(
    conn: sqlite3.Connection, *, book_id: str, client_order_id: str, operator: str, reason: str,
    runtime: ExternalPaperRuntime, config: PaperBooksConfiguration, clock=None,
) -> dict:
    now = _now(clock)
    operator, reason = _bounded(operator, "operator", 128), _bounded(reason, "reason", 512)
    # Cancellation is an explicit risk-reducing operation, not permission to
    # create exposure. Keep it available after new submission is disabled and
    # while a reconciliation/safety incident is active.
    _require_external_config(config, book_id)
    with _order_lease(conn, book_id, client_order_id, operation="CANCEL", now=now, config=config):
        current = _current_event(conn, book_id, client_order_id)
        if current is None:
            raise ExternalPaperError("ORDER_NOT_FOUND", "no local external order exists")
        if current["new_state"] in TERMINAL_STATES or current["new_state"] == STATE_UNKNOWN:
            raise ExternalPaperError("CANCEL_NOT_ALLOWED", f"cannot cancel order in {current['new_state']} state")
        intent = repo.load_order_intent(conn, book_id, current["paper_order_intent_id"])
        if intent is None:
            raise ExternalPaperError("INTENT_NOT_FOUND", "the external order's frozen intent was not found")
        intent["_external_config_hash"] = config.config_hash
        _append_event(
            conn, intent=intent, client_order_id=client_order_id, payload_hash=current["payload_hash"],
            account_fingerprint=current["account_fingerprint"], new_state=STATE_CANCEL_REQUESTED,
            operator=operator, reason=reason, now=now, broker_order_id=current.get("broker_order_id"),
            attempt_number=current["attempt_number"],
        )
        request_id = f"m11_{uuid.uuid4().hex}"
        try:
            order = runtime.cancel_external_order(
                book_id, client_order_id, current["account_fingerprint"],
            )
            _validate_order_response(order, intent, client_order_id, current["account_fingerprint"], now)
            state = _state_from_order(order)
        except Exception as exc:
            event = _record_unknown(
                conn, intent=intent, client_order_id=client_order_id, payload_hash=current["payload_hash"],
                fingerprint=current["account_fingerprint"], operator=operator,
                reason="cancellation outcome is ambiguous; reconciliation required", now=now,
                runtime_request_id=request_id, error_code=getattr(exc, "code", "CANCEL_UNKNOWN"),
                attempt_number=current["attempt_number"],
            )
            return {"status": STATE_UNKNOWN, "event": event}
        event = _append_event(
            conn, intent=intent, client_order_id=client_order_id, payload_hash=current["payload_hash"],
            account_fingerprint=current["account_fingerprint"], new_state=state, operator=operator,
            reason="explicit cancellation broker response", now=now, broker_order_id=order.get("broker_order_id"),
            runtime_request_id=request_id, attempt_number=current["attempt_number"],
        )
        order_submitted_at = datetime.fromisoformat(
            str(order["submitted_at"]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        # Milestone 11.2 Part 13: a cancellation response may carry fills
        # that occurred before the cancel completed. If reconciling those
        # fails, the reservation must NOT be released and the order status
        # must NOT be marked terminal — persist a critical blocker and
        # leave the exposure visibly unresolved rather than silently
        # dropping it via an unprotected exception.
        try:
            apply_external_fills(
                conn, intent=intent, client_order_id=client_order_id, payload_hash=current["payload_hash"],
                fingerprint=current["account_fingerprint"], runtime=runtime, now=now,
                not_before=order_submitted_at,
            )
        except ExternalPaperError as exc:
            fill_error_codes = {
                "MALFORMED_FILL", "FILL_QUANTITY_INVALID", "FILL_NAMESPACE_MISMATCH",
                "FILL_PRICE_INVALID", "MALFORMED_RUNTIME_RESPONSE", "FUTURE_TIMESTAMP",
            }
            _persist_reconciliation(
                conn, book_id=book_id, intent=intent, client_order_id=client_order_id,
                fingerprint=current["account_fingerprint"],
                statuses=("MALFORMED_BROKER_FILL" if exc.code in fill_error_codes else "FILL_APPLICATION_FAILED",),
                details={"stage": "post_cancel_fill_sweep"}, now=now, config=config,
            )
            raise
        except Exception:
            _persist_reconciliation(
                conn, book_id=book_id, intent=intent, client_order_id=client_order_id,
                fingerprint=current["account_fingerprint"], statuses=("FILL_APPLICATION_FAILED",),
                details={"stage": "post_cancel_fill_sweep"}, now=now, config=config,
            )
            raise
        _release_terminal_reservation(conn, intent, state, now)
        repo.update_order_status(conn, book_id, intent["paper_order_intent_id"], state)
        return {"status": state, "event": event, "order": order}


QUEUE_STATUS_AWAITING_SUBMISSION = "AWAITING_OPERATOR_EXTERNAL_SUBMISSION"
QUEUE_STATUS_BLOCKED_BY_RECONCILIATION = "BLOCKED_BY_RECONCILIATION"
QUEUE_STATUSES = (
    QUEUE_STATUS_AWAITING_SUBMISSION, STATE_PREVIEWED, STATE_SUBMISSION_REQUESTED, STATE_SUBMITTED,
    STATE_PARTIALLY_FILLED, STATE_FILLED, STATE_CANCELLED, STATE_REJECTED, STATE_EXPIRED,
    STATE_UNKNOWN, QUEUE_STATUS_BLOCKED_BY_RECONCILIATION,
)


def derive_external_queue_status(conn: sqlite3.Connection, *, book_id: str, paper_order_intent_id: str) -> dict:
    """Part 16: the queue status is always derived fresh from the external
    order-event chain (never a separately-maintained column, which is
    exactly what let the queue silently stay `AWAITING_OPERATOR_EXTERNAL_
    SUBMISSION` forever regardless of what actually happened at the
    broker). Terminal states (FILLED/CANCELLED/REJECTED/EXPIRED) are
    immutable once reached; a non-terminal order with an active critical
    reconciliation is surfaced as `BLOCKED_BY_RECONCILIATION` rather than
    its raw (stale-looking) last event state, so the block is visible.
    """
    event = repo.load_latest_external_order_event_for_intent(conn, book_id, paper_order_intent_id)
    client_order_id = event["client_order_id"] if event else None
    status = event["new_state"] if event else QUEUE_STATUS_AWAITING_SUBMISSION
    if client_order_id is not None and status not in TERMINAL_STATES:
        reconciliations = repo.list_external_reconciliations(conn, book_id, client_order_id)
        if reconciliations and reconciliations[-1]["critical"]:
            status = QUEUE_STATUS_BLOCKED_BY_RECONCILIATION
    return {
        "book_id": book_id, "paper_order_intent_id": paper_order_intent_id,
        "client_order_id": client_order_id, "status": status,
        "external_state": event["new_state"] if event else None,
    }


def list_external_submission_queue_view(conn: sqlite3.Connection, *, book_id: str) -> list[dict]:
    """Read-only queue display (no mutation, no order-scope lease needed):
    one row per queued intent, each linked to its client_order_id and
    current derived external status. Never stores or returns credentials or
    raw broker response bodies."""
    view = []
    for row in repo.list_external_submission_queue(conn, book_id):
        derived = derive_external_queue_status(
            conn, book_id=book_id, paper_order_intent_id=row["paper_order_intent_id"],
        )
        view.append({
            "queue_id": row["queue_id"], "paper_order_intent_id": row["paper_order_intent_id"],
            "source": row["source"], "created_at": row["created_at"],
            "client_order_id": derived["client_order_id"], "status": derived["status"],
        })
    return view


def show_external_paper_order(conn: sqlite3.Connection, *, book_id: str, client_order_id: str) -> dict:
    events = repo.list_external_order_events(conn, book_id=book_id, client_order_id=client_order_id)
    if not events:
        raise ExternalPaperError("ORDER_NOT_FOUND", "external paper order was not found")
    return {"current": events[-1], "events": events, "fills": repo.list_external_broker_fills(conn, book_id, client_order_id)}


def reconcile_external_paper_order(
    conn: sqlite3.Connection, *, book_id: str, runtime: ExternalPaperRuntime,
    config: PaperBooksConfiguration, client_order_id: str | None = None, clock=None,
) -> dict:
    """Public entry point: resolves the target order (if unspecified) and
    acquires the order-scope lease before reconciling. Internal callers that
    already hold the lease for this client_order_id (submit/retry, right
    after their own submission) call `_reconcile_locked` directly instead —
    re-entering this wrapper would try to acquire a lease already held by the
    same logical call and fail closed rather than deadlock or double-acquire.
    """
    now = _now(clock)
    _require_external_config(config, book_id)
    resolved_client_order_id = client_order_id
    if resolved_client_order_id is None:
        row = conn.execute(
            "SELECT client_order_id FROM paper_external_order_events WHERE book_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (book_id,),
        ).fetchone()
        if row is None:
            return _persist_reconciliation(
                conn, book_id=book_id, intent=None, client_order_id=None, fingerprint=None,
                statuses=("ORDER_MISSING_LOCALLY",), details={}, now=now, config=config,
            )
        resolved_client_order_id = row["client_order_id"]
    with _order_lease(conn, book_id, resolved_client_order_id, operation="RECONCILE", now=now, config=config):
        return _reconcile_locked(
            conn, book_id=book_id, runtime=runtime, config=config,
            client_order_id=resolved_client_order_id, now=now,
        )


def _reconcile_locked(
    conn: sqlite3.Connection, *, book_id: str, runtime: ExternalPaperRuntime,
    config: PaperBooksConfiguration, client_order_id: str, now: datetime,
) -> dict:
    """Fail-safe wrapper: reconciliation must never exit on an unexpected
    exception without persisting critical evidence first (Part 8). Known,
    precisely-classified failures are handled inline inside
    `_run_reconciliation` and never reach this except-clause; only a truly
    unexpected exception (a bug, a malformed value that slipped past the
    inline checks, a storage error) does.
    """
    try:
        return _run_reconciliation(
            conn, book_id=book_id, runtime=runtime, config=config, client_order_id=client_order_id, now=now,
        )
    except Exception as exc:
        try:
            return _persist_reconciliation(
                conn, book_id=book_id, intent=None, client_order_id=client_order_id, fingerprint=None,
                statuses=("RECONCILIATION_INTERNAL_ERROR",), details={}, now=now, config=config,
            )
        except Exception as persist_exc:
            raise ExternalPaperError(
                "RECONCILIATION_PERSIST_FAILED", "failed to persist critical reconciliation evidence",
            ) from persist_exc


def _run_reconciliation(
    conn: sqlite3.Connection, *, book_id: str, runtime: ExternalPaperRuntime,
    config: PaperBooksConfiguration, client_order_id: str, now: datetime,
) -> dict:
    current = _current_event(conn, book_id, client_order_id)
    if current is None:
        return _persist_reconciliation(
            conn, book_id=book_id, intent=None, client_order_id=client_order_id, fingerprint=None,
            statuses=("ORDER_MISSING_LOCALLY",), details={}, now=now, config=config,
        )
    intent = repo.load_order_intent(conn, book_id, current["paper_order_intent_id"])
    if intent is None:
        return _persist_reconciliation(
            conn, book_id=book_id, intent=None, client_order_id=client_order_id,
            fingerprint=current["account_fingerprint"], statuses=("ORDER_MISSING_LOCALLY",),
            details={}, now=now, config=config,
        )
    intent["_external_config_hash"] = config.config_hash
    try:
        risk_for_notional = repo.load_risk_decision(conn, intent["risk_decision_id"])
        _validate_frozen_notional(intent, config, risk_for_notional)
    except ExternalPaperError as exc:
        return _persist_reconciliation(
            conn, book_id=book_id, intent=intent, client_order_id=client_order_id,
            fingerprint=current["account_fingerprint"], statuses=(exc.code,), details={}, now=now, config=config,
        )
    account_check = _account_check(runtime, book_id)
    fingerprint = account_check["account_fingerprint"]
    statuses: list[str] = []
    if fingerprint != current["account_fingerprint"]:
        statuses.append("ACCOUNT_FINGERPRINT_MISMATCH")
    request_id = f"m11_{uuid.uuid4().hex}"
    try:
        order = runtime.get_order_by_client_order_id(book_id, client_order_id)
    except Exception:
        order = None
        statuses.append("UNKNOWN")
    lookup_result = "FOUND" if order else "NOT_FOUND"
    repo.save_external_lookup(conn, {
        "lookup_id": _digest(
            "peol_", [client_order_id, current["external_order_event_id"], lookup_result, request_id], 40,
        ),
        "book_id": book_id, "paper_order_intent_id": intent["paper_order_intent_id"],
        "client_order_id": client_order_id, "account_fingerprint": fingerprint,
        "result": lookup_result, "authoritative": int(order is None and "UNKNOWN" not in statuses),
        "runtime_request_id": request_id, "created_at": now.isoformat(),
        "attempt_number": current["attempt_number"], "ambiguous_event_id": current["external_order_event_id"],
        "payload_hash": current["payload_hash"], "lookup_started_at": now.isoformat(),
        "lookup_completed_at": now.isoformat(),
    })
    if order is None:
        if "UNKNOWN" not in statuses:
            statuses.append("ORDER_MISSING_AT_BROKER")
        if current["new_state"] == STATE_UNKNOWN and "UNKNOWN" in statuses:
            statuses.append("AMBIGUOUS_SUBMISSION")
        return _persist_reconciliation(
            conn, book_id=book_id, intent=intent, client_order_id=client_order_id,
            fingerprint=fingerprint, statuses=tuple(dict.fromkeys(statuses)), details={}, now=now, config=config,
        )
    order_valid = True
    try:
        _validate_order_response(order, intent, client_order_id, fingerprint, now)
    except ExternalPaperError:
        order_valid = False
        statuses.append("MALFORMED_BROKER_ORDER")
    try:
        broker_state = _state_from_order(order)
    except ExternalPaperError:
        broker_state = None
        statuses.append("BROKER_STATE_UNKNOWN")
    if order_valid and broker_state is not None and broker_state != current["new_state"] and broker_state in _TRANSITIONS.get(
        current["new_state"], set()
    ):
        _append_event(
            conn, intent=intent, client_order_id=client_order_id, payload_hash=current["payload_hash"],
            account_fingerprint=fingerprint, new_state=broker_state, operator="SYSTEM_RECONCILIATION",
            reason="broker lookup synchronized external order state", now=now,
            broker_order_id=order.get("broker_order_id"), runtime_request_id=request_id,
            attempt_number=current["attempt_number"],
        )
        current = _current_event(conn, book_id, client_order_id)
    if order_valid:
        fill_error_codes = {
            "MALFORMED_FILL", "FILL_QUANTITY_INVALID", "FILL_NAMESPACE_MISMATCH",
            "FILL_PRICE_INVALID", "MALFORMED_RUNTIME_RESPONSE", "FUTURE_TIMESTAMP",
        }
        order_submitted_at = datetime.fromisoformat(
            str(order["submitted_at"]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        try:
            apply_external_fills(
                conn, intent=intent, client_order_id=client_order_id, payload_hash=current["payload_hash"],
                fingerprint=fingerprint, runtime=runtime, now=now, not_before=order_submitted_at,
            )
        except ExternalPaperError as exc:
            statuses.append("MALFORMED_BROKER_FILL" if exc.code in fill_error_codes else "FILL_APPLICATION_FAILED")
        except Exception:
            statuses.append("FILL_APPLICATION_FAILED")
        else:
            _release_terminal_reservation(conn, intent, broker_state or current["new_state"], now)
            reservation_status = "RESERVATION_MISMATCH" if intent["side"] == "BUY" else "SHARE_RESERVATION_MISMATCH"
            remaining = (
                cash_ledger.remaining_buy_reservation(conn, book_id, intent["paper_order_intent_id"])
                if intent["side"] == "BUY"
                else positions.remaining_share_reservation(conn, book_id, intent["paper_order_intent_id"])
            )
            if remaining < 0:
                statuses.append(reservation_status)
    if not client_order_id.startswith(f"epb-{book_id.lower()}-") or order.get("book_id") not in (None, book_id):
        statuses.append("BOOK_NAMESPACE_MISMATCH")
    if order.get("symbol") not in (None, intent["symbol"]):
        statuses.append("SYMBOL_MISMATCH")
    if order.get("side") not in (None, intent["side"]):
        statuses.append("SIDE_MISMATCH")
    if order.get("quantity") is not None and Decimal(str(order["quantity"])) != Decimal(intent["quantity"]):
        statuses.append("QUANTITY_MISMATCH")
    if order.get("limit_price") is not None and Decimal(str(order["limit_price"])) != Decimal(intent["limit_price"]):
        statuses.append("PRICE_MISMATCH")
    local_fill_qty = sum((Decimal(fill["fill_quantity"]) for fill in repo.list_fills_for_intent(
        conn, book_id, intent["paper_order_intent_id"]
    )), Decimal("0"))
    broker_filled = Decimal(str(order.get("filled_quantity", local_fill_qty)))
    if local_fill_qty != broker_filled:
        statuses.append("FILL_QUANTITY_MISMATCH")
    duplicate_details = _detect_duplicate_broker_order(
        runtime, book_id=book_id, intent=intent, client_order_id=client_order_id, order=order,
    )
    if duplicate_details:
        statuses.append("BROKER_ORDER_DUPLICATE")
    try:
        broker_positions_payload = runtime.get_external_positions(book_id)
        broker_account = runtime.get_external_account_snapshot(book_id)
        if set(broker_positions_payload) != {"book_id", "account_fingerprint", "positions"}:
            raise ExternalPaperError("MALFORMED_RUNTIME_RESPONSE", "positions response shape is invalid")
        if set(broker_account) != {
            "provider", "environment", "book_id", "account_fingerprint", "cash", "equity",
            "buying_power", "currency", "as_of",
        }:
            raise ExternalPaperError("MALFORMED_RUNTIME_RESPONSE", "account response shape is invalid")
        if (
            broker_positions_payload.get("book_id") != book_id or broker_account.get("book_id") != book_id
            or broker_account.get("provider") != "alpaca_paper" or broker_account.get("environment") != "paper"
        ):
            raise ExternalPaperError("MALFORMED_RUNTIME_RESPONSE", "reconciliation response scope is invalid")
        for position in broker_positions_payload["positions"]:
            if not isinstance(position, dict) or set(position) != {
                "symbol", "quantity", "average_entry_price", "market_value", "as_of",
            }:
                raise ExternalPaperError("MALFORMED_RUNTIME_RESPONSE", "position response shape is invalid")
        if broker_positions_payload.get("account_fingerprint") != fingerprint or broker_account.get("account_fingerprint") != fingerprint:
            statuses.append("ACCOUNT_FINGERPRINT_MISMATCH")
        local_positions = {p["symbol"]: Decimal(p["quantity"]) for p in repo.list_positions(conn, book_id)}
        broker_positions = {p["symbol"]: Decimal(str(p["quantity"])) for p in broker_positions_payload["positions"]}
        if local_positions != broker_positions:
            statuses.append("POSITION_MISMATCH")
        if cash_ledger.settled_cash(conn, book_id) != Decimal(str(broker_account["cash"])):
            statuses.append("CASH_MISMATCH")
    except ExternalPaperError:
        statuses.append("UNKNOWN")
    except (InvalidOperation, ValueError, TypeError, KeyError, AttributeError):
        statuses.append("RECONCILIATION_INTERNAL_ERROR")
    except Exception:
        statuses.append("UNKNOWN")
    if not statuses:
        statuses.append("MATCHED")
    return _persist_reconciliation(
        conn, book_id=book_id, intent=intent, client_order_id=client_order_id,
        fingerprint=fingerprint, statuses=tuple(dict.fromkeys(statuses)),
        details={
            "local_fill_quantity": str(local_fill_qty), "broker_filled_quantity": str(broker_filled),
            **duplicate_details,
        },
        now=now, config=config,
    )


def _persist_reconciliation(
    conn, *, book_id, intent, client_order_id, fingerprint, statuses, details, now, config,
) -> dict:
    statuses = tuple(statuses) or ("UNKNOWN",)
    critical = any(status in CRITICAL_RECONCILIATION_STATUSES for status in statuses)
    record = {
        "reconciliation_id": _digest(
            "per_", [book_id, client_order_id, statuses, details, now.isoformat()], 40,
        ),
        "book_id": book_id,
        "paper_order_intent_id": intent["paper_order_intent_id"] if intent else None,
        "client_order_id": client_order_id, "account_fingerprint": fingerprint,
        "status": statuses[0], "statuses": statuses, "details": details, "critical": int(critical),
        "created_at": now.isoformat(), "policy_version": POLICY_VERSION, "config_hash": config.config_hash,
    }
    repo.save_external_reconciliation(conn, record)
    return record
