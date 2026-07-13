"""Category K: usage and cost tests (docs/milestone-5.md Step 20.K)."""
from __future__ import annotations

from decimal import Decimal

from trading_research.research.models import COST_CALCULATED, COST_NOT_APPLICABLE, COST_PRICING_NOT_CONFIGURED, COST_USAGE_NOT_RETURNED
from trading_research.research.usage import PricingEntry, build_usage_record, load_pricing_config, select_pricing


def test_input_output_tokens_persisted():
    usage = build_usage_record(
        provider="anthropic", model_name="claude-sonnet-5", role="fundamental", input_tokens=1200,
        output_tokens=300, cache_read_tokens=None, cache_write_tokens=None, latency_ms=850,
        provider_request_id="req-1", retry_count=0, success=True,
    )
    assert usage.input_tokens == 1200
    assert usage.output_tokens == 300
    assert usage.cost_status == COST_PRICING_NOT_CONFIGURED  # no pricing entries passed


def test_missing_usage_not_invented():
    usage = build_usage_record(
        provider="anthropic", model_name="claude-sonnet-5", role="fundamental", input_tokens=None,
        output_tokens=None, cache_read_tokens=None, cache_write_tokens=None, latency_ms=500,
        provider_request_id="req-2", retry_count=0, success=True,
    )
    assert usage.input_tokens is None
    assert usage.cost_status == COST_USAGE_NOT_RETURNED
    assert usage.estimated_cost is None


def test_configured_pricing_calculates_cost():
    pricing = (
        PricingEntry(
            provider="anthropic", model="claude-sonnet-5", effective_date="2026-01-01", currency="USD",
            input_price_per_million=Decimal("3.00"), output_price_per_million=Decimal("15.00"),
            pricing_version="anthropic-2026-01-01",
        ),
    )
    usage = build_usage_record(
        provider="anthropic", model_name="claude-sonnet-5", role="fundamental", input_tokens=1_000_000,
        output_tokens=1_000_000, cache_read_tokens=None, cache_write_tokens=None, latency_ms=1000,
        provider_request_id="req-3", retry_count=0, success=True, pricing_entries=pricing, as_of_date="2026-07-01",
    )
    assert usage.cost_status == COST_CALCULATED
    assert usage.estimated_cost == Decimal("18.00")


def test_pricing_unavailable_for_unconfigured_model():
    usage = build_usage_record(
        provider="anthropic", model_name="claude-opus-99", role="fundamental", input_tokens=100,
        output_tokens=50, cache_read_tokens=None, cache_write_tokens=None, latency_ms=100,
        provider_request_id="req-4", retry_count=0, success=True, pricing_entries=(),
    )
    assert usage.cost_status == COST_PRICING_NOT_CONFIGURED
    assert usage.estimated_cost is None


def test_pricing_selects_most_recent_effective_dated_entry():
    pricing = (
        PricingEntry(provider="anthropic", model="m", effective_date="2026-01-01", currency="USD",
                      input_price_per_million=Decimal("1"), output_price_per_million=Decimal("2"), pricing_version="v1"),
        PricingEntry(provider="anthropic", model="m", effective_date="2026-06-01", currency="USD",
                      input_price_per_million=Decimal("2"), output_price_per_million=Decimal("4"), pricing_version="v2"),
    )
    selected = select_pricing(pricing, "anthropic", "m", "2026-07-01")
    assert selected.pricing_version == "v2"
    selected_early = select_pricing(pricing, "anthropic", "m", "2026-02-01")
    assert selected_early.pricing_version == "v1"


def test_failed_attempt_cost_status_not_applicable():
    usage = build_usage_record(
        provider="anthropic", model_name="m", role="fundamental", input_tokens=None, output_tokens=None,
        cache_read_tokens=None, cache_write_tokens=None, latency_ms=None, provider_request_id=None,
        retry_count=1, success=False,
    )
    assert usage.cost_status == COST_NOT_APPLICABLE
    assert usage.success is False


def test_deterministic_provider_cost_status_not_applicable():
    usage = build_usage_record(
        provider="deterministic", model_name="deterministic-v1", role="fundamental", input_tokens=None,
        output_tokens=None, cache_read_tokens=None, cache_write_tokens=None, latency_ms=1, provider_request_id=None,
        retry_count=0, success=True,
    )
    assert usage.cost_status == COST_NOT_APPLICABLE


def test_no_secret_fields_on_usage_record():
    usage = build_usage_record(
        provider="anthropic", model_name="m", role="fundamental", input_tokens=10, output_tokens=5,
        cache_read_tokens=None, cache_write_tokens=None, latency_ms=1, provider_request_id="req-5",
        retry_count=0, success=True,
    )
    field_names = set(usage.__dataclass_fields__.keys())
    assert not any("key" in f.lower() or "secret" in f.lower() or "token" == f.lower() for f in field_names)


def test_empty_pricing_config_file_returns_no_entries(tmp_path):
    pricing_path = tmp_path / "research_pricing.yaml"
    pricing_path.write_text("version: 1\npricing: []\n")
    assert load_pricing_config(pricing_path) == ()


def test_missing_pricing_config_file_returns_no_entries_not_error(tmp_path):
    assert load_pricing_config(tmp_path / "does_not_exist.yaml") == ()
