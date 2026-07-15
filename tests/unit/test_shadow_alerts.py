"""Tests for shadow/alerts.py (docs/milestone-7.md Step 21, Step 27 section K)."""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_research.shadow.alerts import (
    ALERT_TYPE_BUDGET_EXCEEDED,
    ALERT_TYPE_CYCLE_FAILED,
    ALERT_TYPE_PAUSE_ACTIVATED,
    ALERT_TYPE_PROVIDER_UNAVAILABLE,
    SEVERITY_CRITICAL,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    AlertDeliveryResult,
    AlertValidationError,
    LogAlertSink,
    OperationalAlert,
    PersistenceOnlyAlertSink,
    compute_dedup_key,
    raise_alert,
    sanitize_context,
)
from trading_research.storage.database import connect
from trading_research.storage.shadow_alerts_repositories import (
    list_alert_deliveries,
    list_alerts,
    load_alert,
    resolve_alert,
)

BASE_TIME = datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc)


def _clock_at(t: datetime):
    return lambda: t


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "shadow_alerts_test.db")
        yield c
        c.close()


def _alert(**overrides) -> OperationalAlert:
    kwargs = dict(
        severity=SEVERITY_WARNING, alert_type=ALERT_TYPE_CYCLE_FAILED, message="cycle failed for symbol AAPL",
        context={"symbol": "AAPL"}, created_at=BASE_TIME,
    )
    kwargs.update(overrides)
    return OperationalAlert(**kwargs)


class _AlwaysFailSink:
    name = "always_fail"

    def __init__(self):
        self.calls = 0

    def send(self, alert: OperationalAlert) -> AlertDeliveryResult:
        self.calls += 1
        raise RuntimeError("simulated sink outage")


class _AlwaysSucceedSink:
    name = "always_succeed"

    def __init__(self):
        self.calls = 0

    def send(self, alert: OperationalAlert) -> AlertDeliveryResult:
        self.calls += 1
        return AlertDeliveryResult(sink_name=self.name, success=True, response_text="ok", attempt_number=1)


class _FailOnceThenSucceedSink:
    name = "flaky"

    def __init__(self):
        self.calls = 0

    def send(self, alert: OperationalAlert) -> AlertDeliveryResult:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient failure")
        return AlertDeliveryResult(sink_name=self.name, success=True, response_text="ok", attempt_number=self.calls)


# --- model validation ------------------------------------------------------------


def test_alert_requires_known_severity():
    with pytest.raises(AlertValidationError):
        OperationalAlert(severity="NOT_A_SEVERITY", alert_type=ALERT_TYPE_CYCLE_FAILED, message="x", created_at=BASE_TIME)


def test_alert_requires_known_type():
    with pytest.raises(AlertValidationError):
        OperationalAlert(severity=SEVERITY_WARNING, alert_type="NOT_A_TYPE", message="x", created_at=BASE_TIME)


def test_alert_requires_nonempty_message():
    with pytest.raises(AlertValidationError):
        OperationalAlert(severity=SEVERITY_WARNING, alert_type=ALERT_TYPE_CYCLE_FAILED, message="   ", created_at=BASE_TIME)


def test_alert_requires_timezone_aware_created_at():
    with pytest.raises(AlertValidationError):
        OperationalAlert(
            severity=SEVERITY_WARNING, alert_type=ALERT_TYPE_CYCLE_FAILED, message="x",
            created_at=datetime(2026, 7, 13, 13, 30),
        )


def test_message_is_bounded():
    long_message = "x" * 5000
    alert = _alert(message=long_message)
    assert len(alert.message) <= 1000 + len("...[TRUNCATED]")


# --- sanitized payload (secret-shaped keys stripped) -----------------------------


def test_sanitize_context_strips_secret_shaped_keys():
    cleaned = sanitize_context({"api_key": "sk-ant-abcdef1234567890", "symbol": "AAPL"})
    assert "api_key" not in cleaned
    assert cleaned["symbol"] == "AAPL"


def test_sanitize_context_strips_authorization_key():
    cleaned = sanitize_context({"Authorization": "Bearer abc123", "reason": "provider timeout"})
    assert "Authorization" not in cleaned
    assert cleaned["reason"] == "provider timeout"


def test_sanitize_context_strips_secret_looking_value_even_under_safe_key():
    cleaned = sanitize_context({"note": "token=sk-ant-abcdef1234567890"})
    assert "note" not in cleaned


def test_alert_context_is_sanitized_on_construction():
    alert = OperationalAlert(
        severity=SEVERITY_WARNING, alert_type=ALERT_TYPE_PROVIDER_UNAVAILABLE, message="provider down",
        context={"api_key": "should-not-survive", "provider": "sec_edgar"}, created_at=BASE_TIME,
    )
    assert "api_key" not in alert.context
    assert alert.context["provider"] == "sec_edgar"


def test_sanitize_context_bounds_value_length():
    cleaned = sanitize_context({"blob": "y" * 10_000})
    assert len(cleaned["blob"]) <= 500 + len("...[TRUNCATED]")


def test_sanitize_context_bounds_key_count():
    huge = {f"key_{i}": i for i in range(100)}
    cleaned = sanitize_context(huge)
    assert len(cleaned) <= 25


# --- dedup key determinism --------------------------------------------------------


def test_dedup_key_is_deterministic_for_same_type_and_context():
    k1 = compute_dedup_key(ALERT_TYPE_CYCLE_FAILED, {"symbol": "AAPL"})
    k2 = compute_dedup_key(ALERT_TYPE_CYCLE_FAILED, {"symbol": "AAPL"})
    assert k1 == k2


def test_dedup_key_differs_for_different_context():
    k1 = compute_dedup_key(ALERT_TYPE_CYCLE_FAILED, {"symbol": "AAPL"})
    k2 = compute_dedup_key(ALERT_TYPE_CYCLE_FAILED, {"symbol": "MSFT"})
    assert k1 != k2


# --- persistence before delivery --------------------------------------------------


def test_alert_is_persisted(conn):
    alert = _alert()
    raise_alert(conn, alert, (PersistenceOnlyAlertSink(),), clock=_clock_at(BASE_TIME))
    row = load_alert(conn, alert.alert_id)
    assert row is not None
    assert row["severity"] == SEVERITY_WARNING
    assert row["alert_type"] == ALERT_TYPE_CYCLE_FAILED


def test_alert_persisted_even_when_every_sink_raises(conn):
    """Persistence happens BEFORE delivery is attempted — proven by
    persisting successfully even though every configured sink raises."""
    alert = _alert(severity=SEVERITY_CRITICAL)
    sink = _AlwaysFailSink()
    raise_alert(conn, alert, (sink,), clock=_clock_at(BASE_TIME))
    row = load_alert(conn, alert.alert_id)
    assert row is not None
    assert sink.calls >= 1


# --- delivery ----------------------------------------------------------------------


def test_delivery_recorded_for_persistence_only_sink(conn):
    alert = _alert()
    raise_alert(conn, alert, (PersistenceOnlyAlertSink(),), clock=_clock_at(BASE_TIME))
    deliveries = list_alert_deliveries(conn, alert.alert_id)
    assert len(deliveries) == 1
    assert deliveries[0]["success"] == 1
    assert deliveries[0]["sink_name"] == "persistence_only"


def test_delivery_recorded_for_log_sink(conn):
    alert = _alert()
    raise_alert(conn, alert, (LogAlertSink(),), clock=_clock_at(BASE_TIME))
    deliveries = list_alert_deliveries(conn, alert.alert_id)
    assert len(deliveries) == 1
    assert deliveries[0]["success"] == 1
    assert deliveries[0]["sink_name"] == "log"


def test_delivery_succeeds_across_multiple_sinks(conn):
    alert = _alert()
    sink1, sink2 = _AlwaysSucceedSink(), _AlwaysSucceedSink()
    raise_alert(conn, alert, (sink1, sink2), clock=_clock_at(BASE_TIME))
    deliveries = list_alert_deliveries(conn, alert.alert_id)
    sink_names = {d["sink_name"] for d in deliveries}
    assert sink_names == {"always_succeed"}
    assert sink1.calls == 1
    assert sink2.calls == 1


# --- bounded retry -------------------------------------------------------------------


def test_bounded_retry_succeeds_on_second_attempt(conn):
    alert = _alert()
    sink = _FailOnceThenSucceedSink()
    raise_alert(conn, alert, (sink,), clock=_clock_at(BASE_TIME))
    assert sink.calls == 2
    deliveries = list_alert_deliveries(conn, alert.alert_id)
    assert len(deliveries) == 2
    assert deliveries[0]["success"] == 0
    assert deliveries[1]["success"] == 1


def test_retry_is_bounded_not_unbounded(conn):
    alert = _alert()
    sink = _AlwaysFailSink()
    raise_alert(conn, alert, (sink,), clock=_clock_at(BASE_TIME))
    # default max_delivery_attempts = 2 (1 initial + 1 retry) — never unbounded.
    assert sink.calls == 2
    deliveries = list_alert_deliveries(conn, alert.alert_id)
    assert len(deliveries) == 2
    assert all(d["success"] == 0 for d in deliveries)


# --- deduplication -------------------------------------------------------------------


def test_duplicate_alert_within_window_is_suppressed_not_redelivered(conn):
    sink = _AlwaysSucceedSink()
    alert1 = _alert(created_at=BASE_TIME)
    alert2 = _alert(created_at=BASE_TIME + timedelta(seconds=30))  # same type+context -> same dedup_key
    raise_alert(conn, alert1, (sink,), clock=_clock_at(BASE_TIME), dedup_window_seconds=900)
    raise_alert(conn, alert2, (sink,), clock=_clock_at(BASE_TIME + timedelta(seconds=30)), dedup_window_seconds=900)
    assert sink.calls == 1  # second alert's delivery was suppressed
    all_alerts = list_alerts(conn)
    assert len(all_alerts) == 1  # no duplicate row was persisted


def test_suppressed_duplicate_still_recorded_via_suppressed_count(conn):
    sink = _AlwaysSucceedSink()
    alert1 = _alert(created_at=BASE_TIME)
    alert2 = _alert(created_at=BASE_TIME + timedelta(seconds=30))
    raise_alert(conn, alert1, (sink,), clock=_clock_at(BASE_TIME), dedup_window_seconds=900)
    raise_alert(conn, alert2, (sink,), clock=_clock_at(BASE_TIME + timedelta(seconds=30)), dedup_window_seconds=900)
    row = load_alert(conn, alert1.alert_id)
    assert row["suppressed_count"] == 1  # not silently dropped — incremented on the original row


def test_duplicate_outside_window_is_not_suppressed(conn):
    sink = _AlwaysSucceedSink()
    alert1 = _alert(created_at=BASE_TIME)
    alert2 = _alert(created_at=BASE_TIME + timedelta(seconds=1000))  # beyond 900s window
    raise_alert(conn, alert1, (sink,), clock=_clock_at(BASE_TIME), dedup_window_seconds=900)
    raise_alert(conn, alert2, (sink,), clock=_clock_at(BASE_TIME + timedelta(seconds=1000)), dedup_window_seconds=900)
    assert sink.calls == 2
    assert len(list_alerts(conn)) == 2


def test_different_dedup_keys_are_both_delivered(conn):
    sink = _AlwaysSucceedSink()
    alert1 = _alert(context={"symbol": "AAPL"}, created_at=BASE_TIME)
    alert2 = _alert(context={"symbol": "MSFT"}, created_at=BASE_TIME + timedelta(seconds=5))
    raise_alert(conn, alert1, (sink,), clock=_clock_at(BASE_TIME))
    raise_alert(conn, alert2, (sink,), clock=_clock_at(BASE_TIME + timedelta(seconds=5)))
    assert sink.calls == 2
    assert len(list_alerts(conn)) == 2


# --- failure visibility ----------------------------------------------------------------


def test_critical_alert_failing_every_sink_is_still_queryable(conn):
    alert = _alert(severity=SEVERITY_CRITICAL, alert_type=ALERT_TYPE_PROVIDER_UNAVAILABLE, message="SEC EDGAR unreachable")
    sink1, sink2 = _AlwaysFailSink(), _AlwaysFailSink()
    result = raise_alert(conn, alert, (sink1, sink2), clock=_clock_at(BASE_TIME))
    assert result.success is False

    # The underlying alert is never erased/lost — queryable by id and by listing.
    row = load_alert(conn, alert.alert_id)
    assert row is not None
    assert row["severity"] == SEVERITY_CRITICAL

    all_critical = list_alerts(conn, severity=SEVERITY_CRITICAL)
    assert len(all_critical) == 1
    assert all_critical[0]["alert_id"] == alert.alert_id

    deliveries = list_alert_deliveries(conn, alert.alert_id)
    assert len(deliveries) == 4  # 2 attempts x 2 sinks
    assert all(d["success"] == 0 for d in deliveries)


# --- severity -----------------------------------------------------------------------


@pytest.mark.parametrize("severity", [SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_ERROR, SEVERITY_CRITICAL])
def test_all_severities_are_persistable(conn, severity):
    alert = _alert(severity=severity, created_at=BASE_TIME)
    raise_alert(conn, alert, (PersistenceOnlyAlertSink(),), clock=_clock_at(BASE_TIME))
    row = load_alert(conn, alert.alert_id)
    assert row["severity"] == severity


# --- pause-triggering alert ------------------------------------------------------------


def test_pause_activated_alert(conn):
    alert = OperationalAlert(
        severity=SEVERITY_ERROR, alert_type=ALERT_TYPE_PAUSE_ACTIVATED,
        message="shadow operations paused: provider_failure_rate exceeded threshold",
        context={"target_state": "PAUSED_PROVIDER_HEALTH", "source": "AUTOMATIC_HEALTH_RULE"}, created_at=BASE_TIME,
    )
    raise_alert(conn, alert, (PersistenceOnlyAlertSink(),), clock=_clock_at(BASE_TIME))
    rows = list_alerts(conn, alert_type=ALERT_TYPE_PAUSE_ACTIVATED)
    assert len(rows) == 1


# --- budget alert -----------------------------------------------------------------------


def test_budget_exceeded_alert(conn):
    alert = OperationalAlert(
        severity=SEVERITY_ERROR, alert_type=ALERT_TYPE_BUDGET_EXCEEDED,
        message="daily cost cap would be exceeded", context={"cap_name": "daily_cost"}, created_at=BASE_TIME,
    )
    raise_alert(conn, alert, (PersistenceOnlyAlertSink(),), clock=_clock_at(BASE_TIME))
    rows = list_alerts(conn, alert_type=ALERT_TYPE_BUDGET_EXCEEDED)
    assert len(rows) == 1
    assert rows[0]["severity"] == SEVERITY_ERROR


# --- provider alert ---------------------------------------------------------------------


def test_provider_unavailable_alert(conn):
    alert = OperationalAlert(
        severity=SEVERITY_CRITICAL, alert_type=ALERT_TYPE_PROVIDER_UNAVAILABLE,
        message="Alpaca market data unreachable", context={"provider": "alpaca"}, created_at=BASE_TIME,
    )
    raise_alert(conn, alert, (LogAlertSink(),), clock=_clock_at(BASE_TIME))
    rows = list_alerts(conn, alert_type=ALERT_TYPE_PROVIDER_UNAVAILABLE)
    assert len(rows) == 1


# --- no sinks configured -------------------------------------------------------------------


def test_raise_alert_with_no_sinks_still_persists(conn):
    alert = _alert()
    result = raise_alert(conn, alert, (), clock=_clock_at(BASE_TIME))
    assert result.success is True
    row = load_alert(conn, alert.alert_id)
    assert row is not None
    assert list_alert_deliveries(conn, alert.alert_id) == []


# --- alert resolution (Milestone 9.1) ------------------------------------------------------


def test_new_alert_reads_back_as_unresolved(conn):
    alert = _alert(severity=SEVERITY_CRITICAL)
    raise_alert(conn, alert, (), clock=_clock_at(BASE_TIME))
    row = load_alert(conn, alert.alert_id)
    assert row["resolved_at"] is None
    unresolved = list_alerts(conn, severity=SEVERITY_CRITICAL, unresolved_only=True)
    assert any(r["alert_id"] == alert.alert_id for r in unresolved)


def test_resolve_alert_marks_it_resolved_and_excludes_from_unresolved_only(conn):
    alert = _alert(severity=SEVERITY_CRITICAL)
    raise_alert(conn, alert, (), clock=_clock_at(BASE_TIME))

    resolved = resolve_alert(
        conn, alert.alert_id, resolved_by="alice", reason="investigated — false positive",
        resolved_at=(BASE_TIME + timedelta(minutes=5)).isoformat(),
    )
    assert resolved is True

    row = load_alert(conn, alert.alert_id)
    assert row["resolved_at"] is not None
    assert row["resolved_by"] == "alice"

    unresolved = list_alerts(conn, severity=SEVERITY_CRITICAL, unresolved_only=True)
    assert not any(r["alert_id"] == alert.alert_id for r in unresolved)
    all_critical = list_alerts(conn, severity=SEVERITY_CRITICAL)
    assert any(r["alert_id"] == alert.alert_id for r in all_critical)


def test_resolve_alert_is_idempotent_never_overwrites_first_resolution(conn):
    alert = _alert(severity=SEVERITY_CRITICAL)
    raise_alert(conn, alert, (), clock=_clock_at(BASE_TIME))

    first = resolve_alert(
        conn, alert.alert_id, resolved_by="alice", reason="first",
        resolved_at=(BASE_TIME + timedelta(minutes=5)).isoformat(),
    )
    second = resolve_alert(
        conn, alert.alert_id, resolved_by="bob", reason="second",
        resolved_at=(BASE_TIME + timedelta(minutes=10)).isoformat(),
    )
    assert first is True
    assert second is False
    row = load_alert(conn, alert.alert_id)
    assert row["resolved_by"] == "alice"
