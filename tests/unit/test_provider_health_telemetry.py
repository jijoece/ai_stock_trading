"""Milestone 11.3.1 Item 8 Parts A/B: authoritative per-cycle provider
request telemetry and bounded severe-error classification, wired from real
persisted `evidence_provider_requests` fields.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_research.evidence_providers.health import (
    REQUIRED_CATEGORY_STATUS_FAIL,
    REQUIRED_CATEGORY_STATUS_MISSING,
    REQUIRED_CATEGORY_STATUS_PASS,
    SEVERE_AUTHENTICATION_FAILED,
    SEVERE_DNS_OR_CONNECTION_FAILURE,
    SEVERE_PROTOCOL_OR_SCHEMA_BREAK,
    SEVERE_PROVIDER_CONFIGURATION_INVALID,
    SEVERE_REPEATED_RATE_LIMIT_EXHAUSTION,
    ProviderCoveragePolicy,
    classify_severe_error,
    compute_cycle_provider_telemetry,
    evaluate_required_category_health,
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


def test_unknown_no_response_error_is_not_assumed_severe():
    row = _row(success=False, error_code="ProviderRequestError", http_status=None)
    assert classify_severe_error(row) is None


def test_connection_error_with_http_status_is_not_dns_failure():
    # A ProviderRequestError with a real HTTP status is an ordinary request
    # failure, not a connection-level break.
    row = _row(success=False, error_code="ProviderRequestError", http_status=500)
    assert classify_severe_error(row) != SEVERE_DNS_OR_CONNECTION_FAILURE


def test_repeated_rate_limit_exhaustion_remains_hysteresis_input():
    row = _row(success=False, rate_limited=1, retry_count=3)
    assert classify_severe_error(row) is None


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


# --- Milestone 12.1 Item 6: required categories evaluated independently -----

_TWO_CATEGORY_POLICY = ProviderCoveragePolicy(
    required_categories=(
        ("corporate_filings", ("sec-edgar",)),
        ("market_data", ("alpaca-data",)),
    ),
)


def test_required_provider_dilution_sec_healthy_alpaca_failed_is_not_healthy():
    """Required test #1: SEC 9 successes plus Alpaca 1 failure must not read
    as healthy just because the aggregate is 90%."""
    rows = [_row(provider="sec-edgar", success=True) for _ in range(9)] + [
        _row(provider="alpaca-data", success=False),
    ]
    telem = compute_cycle_provider_telemetry(rows, coverage_policy=_TWO_CATEGORY_POLICY)
    assert telem.aggregate_success_rate == 0.9
    assert telem.unhealthy_required_categories == ("market_data",)
    market_data = next(h for h in telem.required_category_health if h.category == "market_data")
    assert market_data.status == REQUIRED_CATEGORY_STATUS_FAIL
    corporate_filings = next(h for h in telem.required_category_health if h.category == "corporate_filings")
    assert corporate_filings.status == REQUIRED_CATEGORY_STATUS_PASS


def test_required_provider_dilution_alpaca_healthy_sec_absent_is_not_healthy():
    """Required test #2."""
    rows = [_row(provider="alpaca-data", success=True) for _ in range(5)]
    telem = compute_cycle_provider_telemetry(rows, coverage_policy=_TWO_CATEGORY_POLICY)
    assert telem.unhealthy_required_categories == ("corporate_filings",)
    corporate_filings = next(h for h in telem.required_category_health if h.category == "corporate_filings")
    assert corporate_filings.status == REQUIRED_CATEGORY_STATUS_MISSING


def test_each_required_provider_healthy_yields_healthy_coverage():
    """Required test #3."""
    rows = [_row(provider="sec-edgar", success=True), _row(provider="alpaca-data", success=True)]
    telem = compute_cycle_provider_telemetry(rows, coverage_policy=_TWO_CATEGORY_POLICY)
    assert telem.unhealthy_required_categories == ()


def test_optional_provider_failure_does_not_fail_required_coverage():
    """Required test #4."""
    policy = ProviderCoveragePolicy(
        required_categories=(("market_data", ("alpaca-data",)),), optional_providers=("alpaca-news",),
    )
    rows = [_row(provider="alpaca-data", success=True), _row(provider="alpaca-news", success=False)]
    telem = compute_cycle_provider_telemetry(rows, coverage_policy=policy)
    assert telem.unhealthy_required_categories == ()


def test_provider_aliases_normalize_correctly_for_required_category_health():
    """Required test #5."""
    policy = ProviderCoveragePolicy(required_categories=(("market_data", ("alpaca-data",)),))
    rows = [_row(provider="AlpacaData", success=True)]
    telem = compute_cycle_provider_telemetry(rows, coverage_policy=policy)
    assert telem.unhealthy_required_categories == ()


def test_category_specific_sample_floor():
    """Required test #6: a category with a higher configured
    minimum_requests is INSUFFICIENT_DATA (not FAIL/healthy) below it."""
    policy = ProviderCoveragePolicy(
        required_categories=(("market_data", ("alpaca-data",)),),
        category_minimum_requests={"market_data": 3},
    )
    rows = [_row(provider="alpaca-data", success=True)]
    results = evaluate_required_category_health(rows, policy)
    assert results[0].status == "INSUFFICIENT_DATA"
    # INSUFFICIENT_DATA is not counted as unhealthy for pause purposes.
    telem = compute_cycle_provider_telemetry(rows, coverage_policy=policy)
    assert telem.unhealthy_required_categories == ()


def test_per_provider_reasons_are_persisted():
    """Required test #7."""
    results = evaluate_required_category_health(
        [_row(provider="alpaca-data", success=False)],
        ProviderCoveragePolicy(required_categories=(("market_data", ("alpaca-data",)),)),
    )
    assert results[0].reasons
    assert "market_data" in results[0].reasons[0]


def test_aggregate_rate_remains_informational_only():
    """Required test #8: aggregate rate is still computed/available, but no
    longer the sole signal driving the FAIL verdict."""
    rows = [_row(provider="sec-edgar", success=True) for _ in range(9)] + [
        _row(provider="alpaca-data", success=False),
    ]
    telem = compute_cycle_provider_telemetry(rows, coverage_policy=_TWO_CATEGORY_POLICY)
    assert telem.aggregate_success_rate == 0.9  # still reported
    assert telem.unhealthy_required_categories  # but does not determine health alone


def test_failing_category_determines_the_health_dimension_result():
    """Required test #9."""
    from trading_research.shadow.health import (
        CHECK_STATUS_FAIL,
        CycleHealthInputs,
        HealthPolicyConfig,
        evaluate_cycle_health,
        provider_health_check,
    )

    inputs = CycleHealthInputs(
        provider_success_rate=0.9, evidence_completeness_rate=None, claude_role_success_rate=None,
        retry_rate=None, retry_exhaustion_rate=None, unsupported_claim_rate=None, output_truncation_rate=None,
        latency_seconds=None, input_tokens=None, output_tokens=None, cost_usd=None, pricing_configured=True,
        paper_reconciliation_mismatch=False, duplicate_prevention_violation=False, cycle_duration_seconds=None,
        provider_unhealthy_required_categories=("market_data",),
    )
    config = HealthPolicyConfig(
        policy_version="test", pause_on_provider_failure_rate=0.5, pause_on_retry_exhaustion_rate=0.5,
        pause_on_unsupported_claim_rate=0.5, pause_on_reconciliation_mismatch=True, pause_on_budget_breach=True,
    )
    result = evaluate_cycle_health(inputs, config)
    assert provider_health_check(result).status == CHECK_STATUS_FAIL
    assert "market_data" in provider_health_check(result).reason


# --- Milestone 12.1 Item 8: typed transport diagnostic rates ----------------


def test_timeout_rate_counts_only_timeout_category():
    rows = [
        _row(success=False, transport_failure_category="TIMEOUT"),
        _row(success=False, transport_failure_category="HTTP_SERVER_ERROR"),
        _row(success=True),
    ]
    from trading_research.evidence_providers.health import compute_provider_health

    summary = compute_provider_health(rows, "alpaca-data")
    assert summary.timeout_rate == pytest.approx(1 / 3)


def test_http_500_does_not_count_as_timeout():
    rows = [_row(success=False, transport_failure_category="HTTP_SERVER_ERROR") for _ in range(3)]
    from trading_research.evidence_providers.health import compute_provider_health

    summary = compute_provider_health(rows, "alpaca-data")
    assert summary.timeout_rate == 0.0
    assert summary.http_server_error_rate == 1.0


def test_dns_failure_does_not_count_as_timeout():
    rows = [_row(success=False, transport_failure_category="DNS_FAILURE") for _ in range(3)]
    from trading_research.evidence_providers.health import compute_provider_health

    summary = compute_provider_health(rows, "alpaca-data")
    assert summary.timeout_rate == 0.0
    assert summary.dns_failure_rate == 1.0


def test_429_counts_as_rate_limit():
    rows = [_row(success=False, transport_failure_category="RATE_LIMITED") for _ in range(3)]
    from trading_research.evidence_providers.health import compute_provider_health

    summary = compute_provider_health(rows, "alpaca-data")
    assert summary.rate_limit_rate == 1.0


def test_authentication_is_distinct_from_other_categories():
    rows = [
        _row(success=False, transport_failure_category="AUTHENTICATION_FAILURE"),
        _row(success=False, transport_failure_category="TLS_FAILURE"),
        _row(success=True),
    ]
    from trading_research.evidence_providers.health import compute_provider_health

    summary = compute_provider_health(rows, "alpaca-data")
    assert summary.authentication_failure_rate == pytest.approx(1 / 3)
    assert summary.tls_failure_rate == pytest.approx(1 / 3)
    assert summary.timeout_rate == 0.0


def test_successful_requests_count_in_the_denominator():
    rows = [_row(success=True) for _ in range(2)] + [_row(success=False, transport_failure_category="TIMEOUT")]
    from trading_research.evidence_providers.health import compute_provider_health

    summary = compute_provider_health(rows, "alpaca-data")
    assert summary.timeout_rate == pytest.approx(1 / 3)
    assert summary.success_rate == pytest.approx(2 / 3)


def test_missing_legacy_category_is_represented_explicitly_not_fabricated():
    """A legacy pre-migration row with `transport_failure_category='NONE'`
    (or absent entirely) is a real failure but contributes to no typed rate
    — never silently folded into `unknown_transport_error_rate`."""
    rows = [_row(success=False, transport_failure_category="NONE") for _ in range(3)]
    from trading_research.evidence_providers.health import compute_provider_health

    summary = compute_provider_health(rows, "alpaca-data")
    assert summary.timeout_rate == 0.0
    assert summary.unknown_transport_error_rate == 0.0
    assert summary.success_rate == 0.0  # the failure itself is still visible


def test_typed_rates_are_deterministic_and_bounded_between_zero_and_one():
    rows = (
        [_row(success=True) for _ in range(4)]
        + [_row(success=False, transport_failure_category="CONNECTION_RESET") for _ in range(2)]
        + [_row(success=False, transport_failure_category="PROTOCOL_ERROR")]
    )
    from trading_research.evidence_providers.health import compute_provider_health

    summary = compute_provider_health(rows, "alpaca-data")
    for rate in (
        summary.timeout_rate, summary.connection_reset_rate, summary.protocol_error_rate,
        summary.dns_failure_rate, summary.rate_limit_rate,
    ):
        assert rate is not None
        assert 0.0 <= rate <= 1.0
    assert summary.connection_reset_rate == pytest.approx(2 / 7)
    assert summary.protocol_error_rate == pytest.approx(1 / 7)


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
            correlation_mode="RESEARCH_CYCLE", research_cycle_id="cycle-test",
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


def test_build_health_inputs_zero_requests_stays_insufficient_without_symbol_fallback():
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
    assert inputs.provider_request_count == 0
    assert inputs.provider_success_rate is None
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
        correlation_mode="RESEARCH_CYCLE", research_cycle_id="cycle-test",
        transport_failure_category="AUTHENTICATION_FAILURE",
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
