"""Milestone 11.3.1 Item 8 Parts A/B: authoritative per-cycle provider
request telemetry and bounded severe-error classification, wired from real
persisted `evidence_provider_requests` fields.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trading_research.evidence_providers.health import (
    SEVERE_AUTHENTICATION_FAILED,
    SEVERE_DNS_OR_CONNECTION_FAILURE,
    SEVERE_PROTOCOL_OR_SCHEMA_BREAK,
    SEVERE_PROVIDER_CONFIGURATION_INVALID,
    SEVERE_REPEATED_RATE_LIMIT_EXHAUSTION,
    classify_severe_error,
    compute_cycle_provider_telemetry,
)


def _row(**overrides):
    base = {
        "provider": "alpaca-data", "success": True, "http_status": 200, "error_code": None,
        "rate_limited": 0, "retry_count": 0, "cache_status": "MISS", "latency_ms": 100,
    }
    base.update(overrides)
    return base


# --- severe error classification ---------------------------------------------


def test_successful_request_is_never_severe():
    assert classify_severe_error(_row(success=True)) is None


def test_ordinary_failure_without_classification_signal_is_not_severe():
    assert classify_severe_error(_row(success=False, http_status=404, error_code=None)) is None


def test_401_is_authentication_failed():
    assert classify_severe_error(_row(success=False, http_status=401)) == SEVERE_AUTHENTICATION_FAILED


def test_403_is_authentication_failed():
    assert classify_severe_error(_row(success=False, http_status=403)) == SEVERE_AUTHENTICATION_FAILED


def test_provider_configuration_error_is_configuration_invalid():
    row = _row(success=False, error_code="ProviderConfigurationError", http_status=None)
    assert classify_severe_error(row) == SEVERE_PROVIDER_CONFIGURATION_INVALID


def test_malformed_response_is_protocol_or_schema_break():
    row = _row(success=False, error_code="MalformedProviderResponseError")
    assert classify_severe_error(row) == SEVERE_PROTOCOL_OR_SCHEMA_BREAK


def test_connection_error_with_no_http_status_is_dns_or_connection_failure():
    row = _row(success=False, error_code="ProviderRequestError", http_status=None)
    assert classify_severe_error(row) == SEVERE_DNS_OR_CONNECTION_FAILURE


def test_connection_error_with_http_status_is_not_dns_failure():
    # A ProviderRequestError with a real HTTP status is an ordinary request
    # failure, not a connection-level break.
    row = _row(success=False, error_code="ProviderRequestError", http_status=500)
    assert classify_severe_error(row) != SEVERE_DNS_OR_CONNECTION_FAILURE


def test_repeated_rate_limit_exhaustion():
    row = _row(success=False, rate_limited=1, retry_count=3)
    assert classify_severe_error(row) == SEVERE_REPEATED_RATE_LIMIT_EXHAUSTION


def test_single_rate_limit_hit_is_not_severe():
    row = _row(success=False, rate_limited=1, retry_count=0)
    assert classify_severe_error(row) is None


# --- compute_cycle_provider_telemetry -----------------------------------------


def test_one_symbol_many_requests_uses_actual_request_count():
    rows = [_row(symbol="AAPL"), _row(symbol="AAPL"), _row(symbol="AAPL", success=False)]
    telem = compute_cycle_provider_telemetry(rows)
    assert telem.total_requests == 3
    assert telem.successful_requests == 2
    assert telem.aggregate_success_rate == 2 / 3


def test_zero_requests_is_insufficient_not_healthy():
    telem = compute_cycle_provider_telemetry([])
    assert telem.total_requests == 0
    assert telem.aggregate_success_rate is None


def test_required_provider_missing_is_not_treated_as_success():
    rows = [_row(provider="alpaca-data", success=True)]
    telem = compute_cycle_provider_telemetry(rows, required_providers=("alpaca-data", "sec-edgar"))
    assert telem.required_providers_missing == ("sec-edgar",)


def test_one_provider_outage_not_hidden_by_another_providers_success():
    rows = [
        _row(provider="alpaca-data", success=True), _row(provider="alpaca-data", success=True),
        _row(provider="alpaca-data", success=True),
        _row(provider="sec-edgar", success=False), _row(provider="sec-edgar", success=False),
        _row(provider="sec-edgar", success=False),
    ]
    telem = compute_cycle_provider_telemetry(rows, required_providers=("alpaca-data", "sec-edgar"))
    assert telem.aggregate_success_rate == 0.5
    assert telem.per_provider["sec-edgar"].success_rate == 0.0
    assert telem.per_provider["alpaca-data"].success_rate == 1.0


def test_severe_error_present_in_telemetry():
    rows = [_row(success=False, http_status=401)]
    telem = compute_cycle_provider_telemetry(rows)
    assert telem.severe_error is True
    assert telem.severe_error_categories == (SEVERE_AUTHENTICATION_FAILED,)


# --- wiring: _build_health_inputs_from_cycle_result uses real telemetry ------


def test_build_health_inputs_uses_real_provider_request_count_over_symbol_proxy():
    """The core Item 8 Part A fix: one symbol producing many provider
    requests must use the real request count, not len(bounded_symbols)."""
    from trading_research.evidence_providers.persistence import ProviderRequestRecord, save_provider_request
    from trading_research.research.scheduled_cycle import ResearchCycleResult
    from trading_research.shadow.scheduler import _build_health_inputs_from_cycle_result
    from trading_research.storage.database import connect

    conn = connect(":memory:")
    as_of = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    window_start = as_of
    window_end = as_of + timedelta(minutes=5)

    # A single symbol ("AAPL") makes 5 real provider requests this cycle --
    # 4 succeed, 1 fails. len(bounded_symbols) would be 1.
    for i in range(5):
        save_provider_request(conn, ProviderRequestRecord(
            provider="alpaca-data", operation="bars", symbol="AAPL",
            requested_as_of=as_of, retrieved_at=window_start + timedelta(seconds=i),
            provider_response_timestamp=None, http_status=200, content_hash=None,
            normalized_record_hash=None, cache_status="MISS", rate_limited=False, retry_count=0,
            latency_ms=50, success=(i != 4), error_code=None if i != 4 else "ProviderRequestError",
            retryable=False, licensing_classification="ACCOUNT_LINKED",
        ))

    from trading_research.research.scheduled_cycle import SymbolCycleResult

    cycle_result = ResearchCycleResult(
        cycle_id="cycle-test", universe_id="test-universe", as_of=as_of, status="COMPLETED",
        symbol_results=(SymbolCycleResult(symbol="AAPL", status="COMPLETED", evidence_outcome="COMPLETE"),),
        reused_existing_cycle=False,
    )
    inputs = _build_health_inputs_from_cycle_result(
        conn, cycle_result, symbols_attempted=1, cycle_duration_seconds=1.0,
        bounded_symbols=("AAPL",), window_start=window_start, window_end=window_end,
    )
    assert inputs.provider_request_count == 5  # real count, not len(bounded_symbols) == 1
    assert inputs.provider_success_rate == 4 / 5
    conn.close()


def test_build_health_inputs_falls_back_to_symbol_proxy_when_no_real_requests_in_window():
    from trading_research.research.scheduled_cycle import ResearchCycleResult, SymbolCycleResult
    from trading_research.shadow.scheduler import _build_health_inputs_from_cycle_result
    from trading_research.storage.database import connect

    conn = connect(":memory:")
    as_of = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    cycle_result = ResearchCycleResult(
        cycle_id="cycle-test", universe_id="test-universe", as_of=as_of, status="COMPLETED",
        symbol_results=(SymbolCycleResult(symbol="AAPL", status="COMPLETED", evidence_outcome="COMPLETE"),),
        reused_existing_cycle=False,
    )
    inputs = _build_health_inputs_from_cycle_result(
        conn, cycle_result, symbols_attempted=1, cycle_duration_seconds=1.0,
        bounded_symbols=("AAPL",), window_start=as_of, window_end=as_of + timedelta(minutes=5),
    )
    assert inputs.provider_request_count == 1  # falls back to the symbol-level proxy
    assert inputs.provider_success_rate == 1.0
    conn.close()


def test_build_health_inputs_severe_provider_error_is_wired_through():
    from trading_research.evidence_providers.persistence import ProviderRequestRecord, save_provider_request
    from trading_research.research.scheduled_cycle import ResearchCycleResult, SymbolCycleResult
    from trading_research.shadow.scheduler import _build_health_inputs_from_cycle_result
    from trading_research.storage.database import connect

    conn = connect(":memory:")
    as_of = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    save_provider_request(conn, ProviderRequestRecord(
        provider="alpaca-data", operation="bars", symbol="AAPL", requested_as_of=as_of, retrieved_at=as_of,
        provider_response_timestamp=None, http_status=401, content_hash=None, normalized_record_hash=None,
        cache_status="MISS", rate_limited=False, retry_count=0, latency_ms=50, success=False,
        error_code="ProviderRequestError", retryable=False, licensing_classification="ACCOUNT_LINKED",
    ))
    cycle_result = ResearchCycleResult(
        cycle_id="cycle-test", universe_id="test-universe", as_of=as_of, status="COMPLETED",
        symbol_results=(SymbolCycleResult(symbol="AAPL", status="COMPLETED", evidence_outcome="COMPLETE"),),
        reused_existing_cycle=False,
    )
    inputs = _build_health_inputs_from_cycle_result(
        conn, cycle_result, symbols_attempted=1, cycle_duration_seconds=1.0,
        bounded_symbols=("AAPL",), window_start=as_of, window_end=as_of + timedelta(minutes=5),
    )
    assert inputs.provider_severe_error is True
