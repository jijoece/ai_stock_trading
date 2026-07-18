"""Provider-neutral operational alert model and sinks (docs/milestone-7.md
Step 21, ADR 0005 Decision 8). `OperationalAlert` is persisted to
`shadow_alerts` BEFORE any `AlertSink.send()` is attempted — a delivery
failure never erases the underlying operational event, and a `CRITICAL`
alert that fails delivery on every configured sink remains queryable via
`shadow_alerts`/`shadow_alert_deliveries`.

Two concrete sinks ship in this milestone: `PersistenceOnlyAlertSink` (a
no-op beyond what `raise_alert` already persisted) and `LogAlertSink`
(structured log line via `logging_config.get_logger`). No webhook/email sink
is added — the repository has no existing outbound-HTTP-notification
dependency, and ADR 0005 Decision 8 explicitly defers that without a
concrete, already-authorized target.

This module has no import of anything under `evidence_providers` or
`research` (Claude/provider code) — an alert's `context` is always a plain,
pre-sanitized dict the caller already reduced any provider/Claude output to
before it reaches here, mirroring `shadow/pause.py`'s same structural
boundary.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Protocol

from ..logging_config import get_logger
from ..storage.shadow_alerts_repositories import (
    find_recent_alerts_by_dedup_key,
    increment_alert_suppressed_count,
    insert_alert,
    insert_alert_delivery,
)

Clock = Callable[[], datetime]

SEVERITY_INFO = "INFO"
SEVERITY_WARNING = "WARNING"
SEVERITY_ERROR = "ERROR"
SEVERITY_CRITICAL = "CRITICAL"

SEVERITIES = (SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_ERROR, SEVERITY_CRITICAL)

ALERT_TYPE_CYCLE_FAILED = "CYCLE_FAILED"
ALERT_TYPE_CYCLE_PARTIALLY_COMPLETE = "CYCLE_PARTIALLY_COMPLETE"
ALERT_TYPE_LEASE_CONFLICT = "LEASE_CONFLICT"
ALERT_TYPE_LEASE_STALE_RECOVERED = "LEASE_STALE_RECOVERED"
ALERT_TYPE_BUDGET_NEAR_LIMIT = "BUDGET_NEAR_LIMIT"
ALERT_TYPE_BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
ALERT_TYPE_PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
ALERT_TYPE_PROVIDER_FAILURE_RATE_HIGH = "PROVIDER_FAILURE_RATE_HIGH"
ALERT_TYPE_EVIDENCE_COMPLETENESS_LOW = "EVIDENCE_COMPLETENESS_LOW"
ALERT_TYPE_RESEARCH_RETRY_EXHAUSTION_HIGH = "RESEARCH_RETRY_EXHAUSTION_HIGH"
ALERT_TYPE_UNSUPPORTED_CLAIM_RATE_HIGH = "UNSUPPORTED_CLAIM_RATE_HIGH"
ALERT_TYPE_OUTPUT_TRUNCATION = "OUTPUT_TRUNCATION"
ALERT_TYPE_CLAUDE_COST_SPIKE = "CLAUDE_COST_SPIKE"
ALERT_TYPE_RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
ALERT_TYPE_SCHEDULER_MISSED_RUN = "SCHEDULER_MISSED_RUN"
ALERT_TYPE_PAUSE_ACTIVATED = "PAUSE_ACTIVATED"
ALERT_TYPE_KILL_SWITCH_ACTIVATED = "KILL_SWITCH_ACTIVATED"

ALERT_TYPES = (
    ALERT_TYPE_CYCLE_FAILED,
    ALERT_TYPE_CYCLE_PARTIALLY_COMPLETE,
    ALERT_TYPE_LEASE_CONFLICT,
    ALERT_TYPE_LEASE_STALE_RECOVERED,
    ALERT_TYPE_BUDGET_NEAR_LIMIT,
    ALERT_TYPE_BUDGET_EXCEEDED,
    ALERT_TYPE_PROVIDER_UNAVAILABLE,
    ALERT_TYPE_PROVIDER_FAILURE_RATE_HIGH,
    ALERT_TYPE_EVIDENCE_COMPLETENESS_LOW,
    ALERT_TYPE_RESEARCH_RETRY_EXHAUSTION_HIGH,
    ALERT_TYPE_UNSUPPORTED_CLAIM_RATE_HIGH,
    ALERT_TYPE_OUTPUT_TRUNCATION,
    ALERT_TYPE_CLAUDE_COST_SPIKE,
    ALERT_TYPE_RECONCILIATION_MISMATCH,
    ALERT_TYPE_SCHEDULER_MISSED_RUN,
    ALERT_TYPE_PAUSE_ACTIVATED,
    ALERT_TYPE_KILL_SWITCH_ACTIVATED,
)

# Keys matched case-insensitively, as either an exact key match or a
# substring of the key — "api_key", "Authorization", "auth_token", etc are
# all rejected. Mirrors `research/failure_taxonomy.py`'s allowlist-not-
# denylist posture, but context here is caller-supplied free-form
# diagnostic data (unlike the taxonomy's fixed metadata schema) so a
# denylist on *keys* plus a bound on *value length* is the workable
# equivalent.
_SECRET_LIKE_KEY_SUBSTRINGS = (
    "api_key", "apikey", "api-key", "secret", "password", "authorization", "auth_token",
    "bearer", "token", "credential", "private_key", "access_key",
)

MAX_CONTEXT_VALUE_CHARS = 500
MAX_CONTEXT_KEYS = 25
MAX_MESSAGE_CHARS = 1000
MAX_DELIVERY_RESPONSE_CHARS = 500

DEFAULT_DEDUP_WINDOW_SECONDS = 900  # 15 minutes
DEFAULT_MAX_DELIVERY_ATTEMPTS = 2  # 1 initial attempt + 1 bounded retry


class AlertValidationError(ValueError):
    pass


def _is_secret_like_key(key: str) -> bool:
    lowered = key.lower()
    return any(substr in lowered for substr in _SECRET_LIKE_KEY_SUBSTRINGS)


def sanitize_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Strips secret-shaped keys and bounds size/length. Never raises on a
    secret-shaped key — silently stripping (rather than rejecting the whole
    alert) keeps an operational alert usable even if a caller accidentally
    included a suspicious-looking key, while still guaranteeing the secret
    substring itself never reaches persistence or a log line."""
    cleaned: dict[str, Any] = {}
    for key, value in context.items():
        if len(cleaned) >= MAX_CONTEXT_KEYS:
            break
        if _is_secret_like_key(key):
            continue
        if isinstance(value, (dict, list, tuple)):
            value = json.dumps(value, default=str)
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            value = str(value)
        if isinstance(value, str):
            lowered = value.lower()
            if any(substr in lowered for substr in _SECRET_LIKE_KEY_SUBSTRINGS):
                continue
            if len(value) > MAX_CONTEXT_VALUE_CHARS:
                value = value[:MAX_CONTEXT_VALUE_CHARS] + "...[TRUNCATED]"
        cleaned[key] = value
    return cleaned


def compute_dedup_key(alert_type: str, context: Mapping[str, Any]) -> str:
    """Deterministic: the same alert_type + normalized context subset always
    produces the same dedup_key, so `raise_alert` can detect "the same
    underlying condition fired again" independent of message wording or
    created_at. Only a stable, sorted-key subset of context participates —
    high-cardinality/volatile fields (e.g. a timestamp accidentally placed in
    context) would otherwise defeat deduplication entirely."""
    normalized = {str(k): str(v) for k, v in sorted(context.items())}
    payload = json.dumps({"alert_type": alert_type, "context": normalized}, sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"dedup-{digest[:32]}"


@dataclass(frozen=True)
class OperationalAlert:
    severity: str
    alert_type: str
    message: str
    context: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now())
    alert_id: str = field(default_factory=lambda: f"alert-{uuid.uuid4().hex}")
    dedup_key: str = field(init=False)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise AlertValidationError(f"severity {self.severity!r} is not one of {SEVERITIES} — fails closed")
        if self.alert_type not in ALERT_TYPES:
            raise AlertValidationError(f"alert_type {self.alert_type!r} is not one of {ALERT_TYPES} — fails closed")
        if not self.message or not self.message.strip():
            raise AlertValidationError("message must be non-empty")
        if self.created_at.tzinfo is None:
            raise AlertValidationError("created_at must be timezone-aware")
        cleaned_context = sanitize_context(self.context)
        object.__setattr__(self, "context", cleaned_context)
        bounded_message = self.message.strip()
        if len(bounded_message) > MAX_MESSAGE_CHARS:
            bounded_message = bounded_message[:MAX_MESSAGE_CHARS] + "...[TRUNCATED]"
        object.__setattr__(self, "message", bounded_message)
        object.__setattr__(self, "dedup_key", compute_dedup_key(self.alert_type, cleaned_context))


@dataclass(frozen=True)
class AlertDeliveryResult:
    sink_name: str
    success: bool
    response_text: str | None
    attempt_number: int


class AlertSink(Protocol):
    # A read-only property (not a plain mutable attribute) so a frozen
    # dataclass implementation's `name` field — read-only by construction —
    # satisfies this Protocol under Pyright's variance rules (Milestone 12.1
    # Item 10 safety-critical type check: this was a real, if narrow, type
    # error at the `_default_alert_sinks` call site in `shadow/scheduler.py`).
    @property
    def name(self) -> str: ...

    def send(self, alert: OperationalAlert) -> AlertDeliveryResult: ...


@dataclass(frozen=True)
class PersistenceOnlyAlertSink:
    """Satisfies `AlertSink` trivially — persistence already happened in
    `raise_alert` before any sink is invoked, so this sink's `send` is
    intentionally a no-op that always reports success."""

    name: str = "persistence_only"

    def send(self, alert: OperationalAlert) -> AlertDeliveryResult:
        return AlertDeliveryResult(sink_name=self.name, success=True, response_text="persisted", attempt_number=1)


@dataclass(frozen=True)
class LogAlertSink:
    """Structured log line via `logging_config.get_logger` — bounded and
    subject to the same redaction (`logging_config.redact`) as every other
    log line in this repository."""

    name: str = "log"
    logger_name: str = "shadow.alerts"

    def send(self, alert: OperationalAlert) -> AlertDeliveryResult:
        logger = get_logger(self.logger_name)
        bounded_context = json.dumps(dict(alert.context), default=str)
        if len(bounded_context) > MAX_DELIVERY_RESPONSE_CHARS:
            bounded_context = bounded_context[:MAX_DELIVERY_RESPONSE_CHARS] + "...[TRUNCATED]"
        log_line = f"[{alert.severity}] {alert.alert_type}: {alert.message} context={bounded_context}"
        level_by_severity = {
            SEVERITY_INFO: logger.info, SEVERITY_WARNING: logger.warning,
            SEVERITY_ERROR: logger.error, SEVERITY_CRITICAL: logger.critical,
        }
        level_by_severity[alert.severity](log_line)
        return AlertDeliveryResult(sink_name=self.name, success=True, response_text=log_line[:MAX_DELIVERY_RESPONSE_CHARS], attempt_number=1)


def _bound_response(text: str | None) -> str | None:
    if text is None:
        return None
    return text if len(text) <= MAX_DELIVERY_RESPONSE_CHARS else text[:MAX_DELIVERY_RESPONSE_CHARS] + "...[TRUNCATED]"


def _deliver_with_bounded_retry(
    sink: AlertSink, alert: OperationalAlert, *, max_attempts: int,
) -> list[AlertDeliveryResult]:
    """Attempts delivery up to `max_attempts` times (default 2: one initial
    attempt plus one bounded retry — never unbounded). Every attempt is
    returned so the caller can persist each one to `shadow_alert_deliveries`,
    regardless of whether the final attempt succeeded."""
    attempts: list[AlertDeliveryResult] = []
    for attempt_number in range(1, max_attempts + 1):
        try:
            result = sink.send(alert)
            result = AlertDeliveryResult(
                sink_name=result.sink_name, success=result.success,
                response_text=_bound_response(result.response_text), attempt_number=attempt_number,
            )
        except Exception as exc:  # sink raising must never lose the underlying alert (ADR 0005 Decision 8)
            result = AlertDeliveryResult(
                sink_name=getattr(sink, "name", sink.__class__.__name__), success=False,
                response_text=_bound_response(f"sink raised: {exc}"), attempt_number=attempt_number,
            )
        attempts.append(result)
        if result.success:
            break
    return attempts


def raise_alert(
    conn, alert: OperationalAlert, sinks: tuple[AlertSink, ...], clock: Clock,
    dedup_window_seconds: int = DEFAULT_DEDUP_WINDOW_SECONDS,
    max_delivery_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS,
) -> AlertDeliveryResult:
    """Persists `alert` to `shadow_alerts` BEFORE any sink is invoked — this
    ordering holds even if every sink subsequently raises (docs/milestone-7.md
    Step 21: "alerts persisted before delivery"). Checks `dedup_key` against
    recent alerts within `dedup_window_seconds`: a duplicate is NOT
    re-persisted as a new row and NOT delivered again, but the suppression is
    itself recorded (`suppressed_count` incremented on the original row) so
    it is never a silent drop. Returns the delivery result for the
    highest-severity sink outcome (first sink's final attempt) so a caller
    can decide whether to escalate further — full detail is always available
    via `shadow_alert_deliveries`.
    """
    now = clock()
    since = now - timedelta(seconds=dedup_window_seconds)
    recent = find_recent_alerts_by_dedup_key(conn, alert.dedup_key, since_iso=since.isoformat())

    if recent:
        original = recent[0]
        increment_alert_suppressed_count(conn, original["alert_id"])
        return AlertDeliveryResult(
            sink_name="deduplicated", success=True,
            response_text=f"suppressed duplicate of alert_id={original['alert_id']}", attempt_number=0,
        )

    insert_alert(
        conn,
        {
            "alert_id": alert.alert_id, "dedup_key": alert.dedup_key, "severity": alert.severity,
            "alert_type": alert.alert_type, "message": alert.message,
            "context_json": json.dumps(dict(alert.context), default=str), "suppressed_count": 0,
            "created_at": alert.created_at.isoformat(),
        },
    )

    if not sinks:
        return AlertDeliveryResult(sink_name="none", success=True, response_text="no sinks configured", attempt_number=0)

    final_result: AlertDeliveryResult | None = None
    for sink in sinks:
        attempts = _deliver_with_bounded_retry(sink, alert, max_attempts=max_delivery_attempts)
        for attempt in attempts:
            insert_alert_delivery(
                conn,
                {
                    "delivery_id": f"delivery-{uuid.uuid4().hex}", "alert_id": alert.alert_id,
                    "sink_name": attempt.sink_name, "attempt_number": attempt.attempt_number,
                    "success": int(attempt.success), "response_text": attempt.response_text,
                    "created_at": clock().isoformat(),
                },
            )
        if final_result is None:
            final_result = attempts[-1]
        elif attempts[-1].success and not final_result.success:
            final_result = attempts[-1]

    assert final_result is not None
    return final_result
