"""Manual, ambiguity-safe Alpaca PAPER coordinator for isolated paper books.

The main process owns approved intents, risk/safety checks, audit state and
ledger application. It speaks only normalized JSON payloads to a supplied
runtime client; credentials and Alpaca/LumiBot imports remain in the child
``paper_runtime`` process.
"""
from __future__ import annotations

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
from ..storage.shadow_alerts_repositories import list_alerts
from . import cash_ledger, positions
from .config import PaperBooksConfiguration
from .exit_policy import EXIT_DECISIONS
from .models import APPROVED_RISK_DECISIONS, BOOK_STATUS_ACTIVE

POLICY_VERSION = "external-alpaca-paper-v1"

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
    }
    repo.save_external_order_event(conn, event)
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
    if Decimal(intent["notional_usd"]) > min(
        cfg.risk.max_order_notional_usd, cfg.external_broker.maximum_order_notional_usd,
    ):
        raise ExternalPaperError("EXTERNAL_NOTIONAL_LIMIT", "order notional exceeds the strictest configured cap")
    as_of = datetime.fromisoformat(intent["as_of"])
    if as_of.tzinfo is None or now - as_of.astimezone(timezone.utc) > timedelta(
        seconds=cfg.risk.reject_stale_market_price_seconds
    ):
        raise ExternalPaperError("STALE_INTENT", "paper intent is stale for external submission")
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
        "quantity": int(Decimal(intent["quantity"])), "limit_price": str(intent["limit_price"]),
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


def preview_external_paper_order(
    conn: sqlite3.Connection, *, book_id: str, paper_order_intent_id: str, operator: str,
    runtime: ExternalPaperRuntime, config: PaperBooksConfiguration, clock=None,
) -> dict:
    now = _now(clock)
    operator = _bounded(operator, "operator", 128)
    intent = _intent(conn, config, book_id, paper_order_intent_id, now)
    _safety_checks(conn, book_id)
    client_order_id, payload_hash = derive_external_order_identity(intent)
    current = _current_event(conn, book_id, client_order_id)
    if current and current["new_state"] not in (STATE_PREVIEWED,):
        raise ExternalPaperError("ORDER_ALREADY_EXTERNAL", f"external order is already {current['new_state']}")
    account = _account_check(runtime, book_id)
    fingerprint = account["account_fingerprint"]
    _verify_fingerprint_history(conn, book_id, fingerprint)
    runtime_result = runtime.preview_limit_order(_payload(intent, client_order_id, payload_hash, fingerprint, now))
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


def _validate_order_response(order: dict, intent: dict, client_order_id: str, fingerprint: str) -> None:
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
    if intent["side"] == "BUY":
        reservation_inserted = cash_ledger.reserve_for_order(
            conn, intent["book_id"], intent["paper_order_intent_id"], Decimal(intent["notional_usd"]), now,
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
        raise
    try:
        order = runtime.submit_limit_order(_payload(intent, client_order_id, payload_hash, fingerprint, now))
        _validate_order_response(order, intent, client_order_id, fingerprint)
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
    fills = apply_external_fills(
        conn, intent=intent, client_order_id=client_order_id, payload_hash=payload_hash,
        fingerprint=fingerprint, runtime=runtime, now=now,
    )
    _release_terminal_buy_reservation(conn, intent, new_state, now)
    return {"status": new_state, "event": event, "order": order, "new_fills": fills}


def _release_terminal_buy_reservation(conn, intent: dict, state: str, now: datetime) -> None:
    if intent["side"] == "BUY" and state in TERMINAL_STATES:
        cash_ledger.release_reservation(
            conn, intent["book_id"], intent["paper_order_intent_id"], Decimal(intent["notional_usd"]),
            now, reason="external-terminal",
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
        order = runtime.get_order_by_client_order_id(book_id, client_order_id)
        if order is None:
            raise ExternalPaperError("ORDER_MISSING_AT_BROKER", "existing external order was not found at broker")
        return {"status": current["new_state"], "event": current, "order": order, "duplicate_submit": False}
    result = _submit_once(
        conn, intent=intent, client_order_id=client_order_id, payload_hash=payload_hash,
        fingerprint=fingerprint, operator=operator, reason=reason, runtime=runtime,
        config=config, now=now, attempt_number=0,
    )
    if result["status"] != STATE_UNKNOWN:
        result["reconciliation"] = reconcile_external_paper_order(
            conn, book_id=book_id, client_order_id=client_order_id,
            runtime=runtime, config=config, clock=lambda: now,
        )
    return result


def apply_external_fills(
    conn: sqlite3.Connection, *, intent: dict, client_order_id: str, payload_hash: str,
    fingerprint: str, runtime: ExternalPaperRuntime, now: datetime,
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
            conn.execute("BEGIN IMMEDIATE")
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
                else:
                    positions.apply_sell_fill(
                        conn, intent["book_id"], intent["symbol"], local_fill_id, quantity, price,
                        filled_at, commit=False,
                    )
                    cash_ledger.settle_sell(
                        conn, intent["book_id"], local_fill_id, quantity * price, Decimal("0"),
                        Decimal("0"), filled_at, commit=False,
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
        _release_terminal_buy_reservation(conn, intent, STATE_FILLED, now)
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
    current = _current_event(conn, book_id, client_order_id)
    if current is None or current["new_state"] != STATE_UNKNOWN:
        raise ExternalPaperError("RETRY_NOT_ALLOWED", "retry requires an ambiguous submission state")
    lookup = repo.load_latest_external_lookup(conn, book_id, client_order_id)
    if lookup is None or lookup["result"] != "NOT_FOUND" or not lookup["authoritative"]:
        raise ExternalPaperError("NOT_FOUND_NOT_CONFIRMED", "authoritative broker NOT_FOUND evidence is required")
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
    if result["status"] != STATE_UNKNOWN:
        result["reconciliation"] = reconcile_external_paper_order(
            conn, book_id=book_id, client_order_id=client_order_id,
            runtime=runtime, config=config, clock=lambda: now,
        )
    return result


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
        _validate_order_response(order, intent, client_order_id, current["account_fingerprint"])
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
    _release_terminal_buy_reservation(conn, intent, state, now)
    repo.update_order_status(conn, book_id, intent["paper_order_intent_id"], state)
    return {"status": state, "event": event, "order": order}


def show_external_paper_order(conn: sqlite3.Connection, *, book_id: str, client_order_id: str) -> dict:
    events = repo.list_external_order_events(conn, book_id=book_id, client_order_id=client_order_id)
    if not events:
        raise ExternalPaperError("ORDER_NOT_FOUND", "external paper order was not found")
    return {"current": events[-1], "events": events, "fills": repo.list_external_broker_fills(conn, book_id, client_order_id)}


def reconcile_external_paper_order(
    conn: sqlite3.Connection, *, book_id: str, runtime: ExternalPaperRuntime,
    config: PaperBooksConfiguration, client_order_id: str | None = None, clock=None,
) -> dict:
    now = _now(clock)
    _require_external_config(config, book_id)
    if client_order_id is None:
        row = conn.execute(
            "SELECT client_order_id FROM paper_external_order_events WHERE book_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (book_id,),
        ).fetchone()
        if row is None:
            return _persist_reconciliation(
                conn, book_id=book_id, intent=None, client_order_id=None, fingerprint=None,
                statuses=("ORDER_MISSING_LOCALLY",), details={}, now=now, config=config,
            )
        client_order_id = row["client_order_id"]
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
        "lookup_id": _digest("peol_", [client_order_id, lookup_result, now.isoformat()], 40),
        "book_id": book_id, "paper_order_intent_id": intent["paper_order_intent_id"],
        "client_order_id": client_order_id, "account_fingerprint": fingerprint,
        "result": lookup_result, "authoritative": int(order is None and "UNKNOWN" not in statuses),
        "runtime_request_id": request_id, "created_at": now.isoformat(),
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
        _validate_order_response(order, intent, client_order_id, fingerprint)
    except ExternalPaperError as exc:
        order_valid = False
        mapping = {
            "BROKER_ORDER_MISMATCH": "UNKNOWN", "MALFORMED_RUNTIME_RESPONSE": "UNKNOWN",
        }
        statuses.append(mapping.get(exc.code, "UNKNOWN"))
    try:
        broker_state = _state_from_order(order)
    except ExternalPaperError:
        broker_state = None
        statuses.append("UNKNOWN")
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
        apply_external_fills(
            conn, intent=intent, client_order_id=client_order_id, payload_hash=current["payload_hash"],
            fingerprint=fingerprint, runtime=runtime, now=now,
        )
        _release_terminal_buy_reservation(conn, intent, broker_state or current["new_state"], now)
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
    except Exception:
        statuses.append("UNKNOWN")
    if not statuses:
        statuses.append("MATCHED")
    return _persist_reconciliation(
        conn, book_id=book_id, intent=intent, client_order_id=client_order_id,
        fingerprint=fingerprint, statuses=tuple(dict.fromkeys(statuses)),
        details={"local_fill_quantity": str(local_fill_qty), "broker_filled_quantity": str(broker_filled)},
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
