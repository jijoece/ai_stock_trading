from __future__ import annotations

import json
import socket
import sqlite3
import ssl
from datetime import datetime, timezone

import httpx
import pytest

from trading_research.evidence_providers.health import (
    ProviderCoveragePolicy,
    compute_cycle_provider_telemetry,
    normalize_provider_name,
)
from trading_research.evidence_providers.http_client import (
    TRANSPORT_AUTHENTICATION_FAILURE,
    TRANSPORT_CONNECTION_REFUSED,
    TRANSPORT_DNS_FAILURE,
    TRANSPORT_HTTP_SERVER_ERROR,
    TRANSPORT_PROTOCOL_ERROR,
    TRANSPORT_RATE_LIMITED,
    TRANSPORT_TIMEOUT,
    TRANSPORT_TLS_FAILURE,
    HttpJsonClient,
    classify_httpx_transport_exception,
)
from trading_research.evidence_providers.persistence import (
    CORRELATION_MANUAL,
    CORRELATION_RESEARCH_CYCLE,
    CORRELATION_SCHEDULED,
    LICENSE_ACCOUNT_LINKED,
    ProviderRequestRecord,
    list_provider_requests_for_cycle,
    save_provider_request,
)
from trading_research.evidence_providers.rate_limits import MinIntervalRateLimiter
from trading_research.shadow import health as health_mod
from trading_research.shadow import health_hysteresis as hysteresis_mod
from trading_research.storage import shadow_alerts_repositories as alert_repo
from trading_research.storage.database import connect

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


def _record(*, cycle_id: str | None, mode: str, provider: str = "alpaca-data", success: bool = True):
    return ProviderRequestRecord(
        provider=provider, operation="test", symbol="AAPL", requested_as_of=NOW, retrieved_at=NOW,
        provider_response_timestamp=None, http_status=200 if success else 500, content_hash=None,
        normalized_record_hash=None, cache_status="MISS", rate_limited=False, retry_count=0,
        latency_ms=1, success=success, error_code=None if success else "ProviderRequestError",
        retryable=None, licensing_classification=LICENSE_ACCOUNT_LINKED,
        correlation_mode=mode, research_cycle_id=cycle_id,
        scheduler_run_id="scheduler-1" if mode == CORRELATION_SCHEDULED else None,
        transport_failure_category="NONE" if success else TRANSPORT_HTTP_SERVER_ERROR,
    )


def test_cycle_correlation_is_exact_and_legacy_manual_rows_never_leak():
    conn = connect(":memory:")
    save_provider_request(conn, _record(cycle_id="cycle-a", mode=CORRELATION_RESEARCH_CYCLE))
    save_provider_request(conn, _record(cycle_id="cycle-b", mode=CORRELATION_RESEARCH_CYCLE))
    save_provider_request(conn, _record(cycle_id=None, mode=CORRELATION_MANUAL))
    assert [row["research_cycle_id"] for row in list_provider_requests_for_cycle(conn, "cycle-a")] == ["cycle-a"]
    assert [row["research_cycle_id"] for row in list_provider_requests_for_cycle(conn, "cycle-b")] == ["cycle-b"]
    conn.close()


def test_scheduled_request_without_both_cycle_and_scheduler_identity_fails_closed():
    conn = connect(":memory:")
    with pytest.raises(ValueError, match="require research_cycle_id and scheduler_run_id"):
        save_provider_request(conn, _record(cycle_id=None, mode=CORRELATION_SCHEDULED))
    conn.close()


def test_pr15_request_schema_migrates_without_attributing_legacy_rows(tmp_path):
    path = tmp_path / "pr15.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE evidence_provider_requests (request_id TEXT PRIMARY KEY, provider TEXT NOT NULL, "
        "operation TEXT NOT NULL, symbol TEXT NOT NULL, requested_as_of TEXT NOT NULL, retrieved_at TEXT NOT NULL, "
        "provider_response_timestamp TEXT, http_status INTEGER, content_hash TEXT, normalized_record_hash TEXT, "
        "cache_status TEXT NOT NULL, rate_limited INTEGER NOT NULL, retry_count INTEGER NOT NULL, latency_ms INTEGER, "
        "success INTEGER NOT NULL, error_code TEXT, retryable INTEGER, licensing_classification TEXT NOT NULL, "
        "raw_payload_stored INTEGER NOT NULL, raw_payload_json TEXT, created_at TEXT NOT NULL)"
    )
    legacy.execute(
        "INSERT INTO evidence_provider_requests VALUES "
        "('legacy-1','alpaca-data','bars','AAPL',?,?,?,?,NULL,NULL,'MISS',0,0,1,1,NULL,NULL,"
        "'ACCOUNT_LINKED',0,NULL,?)",
        (NOW.isoformat(), NOW.isoformat(), None, 200, NOW.isoformat()),
    )
    legacy.commit()
    legacy.close()
    migrated = connect(path)
    row = migrated.execute("SELECT * FROM evidence_provider_requests WHERE request_id='legacy-1'").fetchone()
    assert row["correlation_mode"] == "LEGACY_MANUAL"
    assert row["research_cycle_id"] is None
    assert row["transport_failure_category"] == "NONE"
    assert list_provider_requests_for_cycle(migrated, "new-cycle") == []
    migrated.close()


def _row(provider: str, success: bool = True) -> dict:
    return {
        "provider": provider, "success": success, "http_status": 200 if success else 500,
        "error_code": None, "rate_limited": 0, "retry_count": 0, "cache_status": "MISS",
        "latency_ms": 1, "transport_failure_category": "NONE" if success else TRANSPORT_HTTP_SERVER_ERROR,
    }


def test_required_provider_coverage_fails_closed_without_optional_contamination():
    policy = ProviderCoveragePolicy(
        required_categories=(("market_data", ("alpaca-data",)), ("corporate_filings", ("sec-edgar",))),
        optional_providers=("alpaca-news",), configuration_hash="config-a",
    )
    healthy = compute_cycle_provider_telemetry(
        [_row("alpaca"), _row("alpaca-data"), _row("sec"), _row("sec-edgar")], coverage_policy=policy,
    )
    assert healthy.required_providers_missing == ()
    missing = compute_cycle_provider_telemetry(
        [_row("alpaca-data") for _ in range(5)] + [_row("alpaca-news") for _ in range(5)],
        coverage_policy=policy,
    )
    assert missing.required_providers_missing == ("sec-edgar",)
    assert missing.missing_required_categories == ("corporate_filings",)
    assert normalize_provider_name("SEC_EDGAR_API") == "sec-edgar"


def test_provider_policy_hash_changes_at_configuration_boundary():
    base = dict(required_categories=(("market_data", ("alpaca-data",)),), telemetry_expected=True)
    assert ProviderCoveragePolicy(**base, configuration_hash="a").policy_hash != ProviderCoveragePolicy(
        **base, configuration_hash="b"
    ).policy_hash


def _typed_transport(category_exception: BaseException, expected: str) -> None:
    request = httpx.Request("GET", "https://example.invalid")
    try:
        raise category_exception
    except BaseException as cause:
        try:
            raise httpx.ConnectError("redacted", request=request) from cause
        except httpx.ConnectError as exc:
            assert classify_httpx_transport_exception(exc) == expected


def test_typed_dns_tls_and_refusal_categories_are_distinct():
    _typed_transport(socket.gaierror(-2, "dns"), TRANSPORT_DNS_FAILURE)
    _typed_transport(ssl.SSLError("tls"), TRANSPORT_TLS_FAILURE)
    _typed_transport(ConnectionRefusedError(), TRANSPORT_CONNECTION_REFUSED)


@pytest.mark.parametrize(
    ("status", "category"),
    [(401, TRANSPORT_AUTHENTICATION_FAILURE), (403, TRANSPORT_AUTHENTICATION_FAILURE),
     (429, TRANSPORT_RATE_LIMITED), (500, TRANSPORT_HTTP_SERVER_ERROR)],
)
def test_http_status_transport_category_is_persistable(status: int, category: str):
    observed: list[dict] = []
    client = HttpJsonClient(
        base_headers={}, rate_limiter=MinIntervalRateLimiter(0), max_attempts=1,
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"error": "x"})),
        on_response=observed.append,
    )
    with pytest.raises(Exception):
        client.get_json("https://example.invalid", operation="test", symbol="AAPL")
    assert observed[-1]["transport_failure_category"] == category
    assert set(observed[-1]).isdisjoint({"message", "exception", "raw_exception"})


def test_timeout_is_typed_and_not_structurally_severe():
    observed: list[dict] = []

    def timeout(request):
        raise httpx.ReadTimeout("secret raw timeout text", request=request)

    client = HttpJsonClient(
        base_headers={}, rate_limiter=MinIntervalRateLimiter(0), max_attempts=1,
        transport=httpx.MockTransport(timeout), on_response=observed.append,
    )
    with pytest.raises(Exception):
        client.get_json("https://example.invalid", operation="test", symbol="AAPL")
    assert observed[-1]["transport_failure_category"] == TRANSPORT_TIMEOUT
    assert "secret raw timeout text" not in json.dumps(observed[-1])


def _health_inputs(**overrides) -> health_mod.CycleHealthInputs:
    values = dict(
        provider_success_rate=0.0, evidence_completeness_rate=None, claude_role_success_rate=None,
        retry_rate=None, retry_exhaustion_rate=None, unsupported_claim_rate=None,
        output_truncation_rate=None, latency_seconds=None, input_tokens=None, output_tokens=None,
        cost_usd=None, pricing_configured=True, paper_reconciliation_mismatch=False,
        duplicate_prevention_violation=False, cycle_duration_seconds=1.0, provider_request_count=5,
    )
    values.update(overrides)
    return health_mod.CycleHealthInputs(**values)


def _health_config() -> health_mod.HealthPolicyConfig:
    return health_mod.HealthPolicyConfig(
        policy_version="test", pause_on_provider_failure_rate=0.2,
        pause_on_retry_exhaustion_rate=0.2, pause_on_unsupported_claim_rate=0.2,
        pause_on_reconciliation_mismatch=True, pause_on_budget_breach=True,
        minimum_requests_for_failure_rate=5,
    )


def test_hysteresis_controls_effective_pause_and_history_is_complete():
    conn = connect(":memory:")
    health_result = health_mod.evaluate_cycle_health(_health_inputs(), _health_config())
    policy = hysteresis_mod.PersistentHealthPolicyConfig()
    evidence = hysteresis_mod.HysteresisEvaluationEvidence(
        sample_size=5, minimum_sample_size=5, aggregate_success_rate=0.0,
        required_categories=("market_data",), required_providers=("alpaca-data",),
        observed_providers=("alpaca-data",), per_provider_metrics={"alpaca-data": {"success_rate": 0.0}},
        provider_policy_hash="coverage-a",
    )
    decisions = []
    for index in range(1, 4):
        hysteresis = hysteresis_mod.evaluate_and_persist_hysteresis(
            conn, cycle_id=f"cycle-{index}", cycle_status=health_result.status, qualified=True,
            config=policy, clock=lambda: NOW, evidence=evidence,
        )
        decisions.append(health_mod.combine_effective_health_decision(health_result, hysteresis.decision))
    assert [decision.effective_status for decision in decisions] == [
        health_mod.STATUS_DEGRADED, health_mod.STATUS_PAUSE_RECOMMENDED, health_mod.STATUS_PAUSE_REQUIRED,
    ]
    rows = conn.execute("SELECT * FROM shadow_health_hysteresis_evaluations ORDER BY cycle_id").fetchall()
    assert len(rows) == 3
    assert json.loads(rows[-1]["per_provider_metrics_json"])["alpaca-data"]["success_rate"] == 0.0
    assert rows[-1]["consecutive_failures_before"] == 2
    replay = hysteresis_mod.evaluate_and_persist_hysteresis(
        conn, cycle_id="cycle-3", cycle_status=health_result.status, qualified=True,
        config=policy, clock=lambda: NOW, evidence=evidence,
    )
    assert replay.idempotent_replay is True
    assert len(conn.execute("SELECT * FROM shadow_health_hysteresis_evaluations").fetchall()) == 3
    assert alert_repo.load_latest_health_hysteresis_evaluation(conn)["effective_status"] == "PAUSE_REQUIRED"
    conn.close()


def test_insufficient_sample_moves_neither_streak_and_auth_bypasses_immediately():
    conn = connect(":memory:")
    insufficient = health_mod.evaluate_cycle_health(
        _health_inputs(provider_request_count=4), _health_config(),
    )
    first = hysteresis_mod.evaluate_and_persist_hysteresis(
        conn, cycle_id="insufficient", cycle_status=insufficient.status,
        qualified=health_mod.provider_health_is_qualified(insufficient),
        config=hysteresis_mod.PersistentHealthPolicyConfig(), clock=lambda: NOW,
    )
    assert (first.consecutive_failures, first.consecutive_recoveries) == (0, 0)

    authentication = health_mod.evaluate_cycle_health(
        _health_inputs(
            provider_request_count=1, provider_severe_error=True,
            provider_severe_error_categories=(TRANSPORT_AUTHENTICATION_FAILURE,),
        ),
        _health_config(),
    )
    auth_hysteresis = hysteresis_mod.evaluate_and_persist_hysteresis(
        conn, cycle_id="auth", cycle_status=authentication.status, qualified=True, severe_error=True,
        immediate_pause=True, config=hysteresis_mod.PersistentHealthPolicyConfig(), clock=lambda: NOW,
    )
    effective = health_mod.combine_effective_health_decision(
        authentication, auth_hysteresis.decision,
        provider_severe_categories=(TRANSPORT_AUTHENTICATION_FAILURE,),
    )
    assert effective.immediate_pause is True
    assert effective.effective_status == health_mod.STATUS_PAUSE_REQUIRED
    conn.close()


def test_hysteresis_state_and_history_roll_back_together_on_either_insert_failure():
    conn = connect(":memory:")
    conn.execute(
        "CREATE TRIGGER reject_health_history BEFORE INSERT ON shadow_health_hysteresis_evaluations "
        "BEGIN SELECT RAISE(ABORT, 'injected history failure'); END"
    )
    with pytest.raises(Exception, match="injected history failure"):
        hysteresis_mod.evaluate_and_persist_hysteresis(
            conn, cycle_id="history-fails", cycle_status=health_mod.STATUS_DEGRADED, qualified=True,
            config=hysteresis_mod.PersistentHealthPolicyConfig(), clock=lambda: NOW,
        )
    assert alert_repo.load_health_hysteresis_state(conn, "default") is None
    assert conn.execute("SELECT COUNT(*) FROM shadow_health_hysteresis_evaluations").fetchone()[0] == 0
    conn.execute("DROP TRIGGER reject_health_history")
    conn.execute(
        "CREATE TRIGGER reject_health_state BEFORE INSERT ON shadow_health_hysteresis_state "
        "BEGIN SELECT RAISE(ABORT, 'injected state failure'); END"
    )
    with pytest.raises(Exception, match="injected state failure"):
        hysteresis_mod.evaluate_and_persist_hysteresis(
            conn, cycle_id="state-fails", cycle_status=health_mod.STATUS_DEGRADED, qualified=True,
            config=hysteresis_mod.PersistentHealthPolicyConfig(), clock=lambda: NOW,
        )
    assert conn.execute("SELECT COUNT(*) FROM shadow_health_hysteresis_evaluations").fetchone()[0] == 0
    conn.close()
