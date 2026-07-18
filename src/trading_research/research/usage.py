"""Token usage, latency, and cost tracking (docs/milestone-5.md Step 17).

Pricing is optional, effective-dated configuration — never a hardcoded
"timeless" constant (Step 3). When no pricing entry applies, cost stays
unset and `cost_status` explains why; usage counts are never invented when a
provider doesn't return them.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

from ..config import REPO_ROOT
from .models import (
    COST_BASIS_DIRECT_API_ESTIMATE,
    COST_BASIS_NOT_APPLICABLE,
    COST_BASIS_USAGE_NOT_RETURNED,
    COST_CALCULATED,
    COST_NOT_APPLICABLE,
    COST_PRICING_NOT_CONFIGURED,
    COST_USAGE_NOT_RETURNED,
    TOKEN_ACCOUNTING_NOT_APPLICABLE,
    UsageRecord,
)

DEFAULT_PRICING_PATH = REPO_ROOT / "config" / "research_pricing.yaml"


@dataclass(frozen=True)
class PricingEntry:
    provider: str
    model: str
    effective_date: str
    currency: str
    input_price_per_million: Decimal
    output_price_per_million: Decimal
    pricing_version: str


def load_pricing_config(path: Path | None = None) -> tuple[PricingEntry, ...]:
    """Returns an empty tuple (not an error) when no pricing file exists —
    absence of pricing configuration must never break offline tests."""
    config_path = path or DEFAULT_PRICING_PATH
    if not config_path.exists():
        return ()
    raw = yaml.safe_load(config_path.read_text()) or {}
    entries = []
    for row in raw.get("pricing", []):
        entries.append(
            PricingEntry(
                provider=row["provider"],
                model=row["model"],
                effective_date=str(row["effective_date"]),
                currency=row.get("currency", "USD"),
                input_price_per_million=Decimal(str(row["input_price_per_million"])),
                output_price_per_million=Decimal(str(row["output_price_per_million"])),
                pricing_version=row["pricing_version"],
            )
        )
    return tuple(entries)


def select_pricing(
    entries: tuple[PricingEntry, ...], provider: str, model: str, as_of_date: str
) -> PricingEntry | None:
    """Most recent entry for (provider, model) with effective_date <= as_of_date."""
    candidates = [
        e for e in entries if e.provider == provider and e.model == model and e.effective_date <= as_of_date
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda e: e.effective_date)


def build_usage_record(
    *,
    provider: str,
    model_name: str,
    role: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
    latency_ms: int | None,
    provider_request_id: str | None,
    retry_count: int,
    success: bool,
    pricing_entries: tuple[PricingEntry, ...] = (),
    as_of_date: str | None = None,
    cost_estimate_basis: str | None = None,
    configured_model_alias: str | None = None,
    resolved_model_name: str | None = None,
    claude_code_version: str | None = None,
    provider_cli_version: str | None = None,
    provider_adapter_version: str | None = None,
    reasoning_output_tokens: int | None = None,
    token_accounting_policy: str = TOKEN_ACCOUNTING_NOT_APPLICABLE,
    pricing_model: str | None = None,
) -> UsageRecord:
    provenance = {
        "configured_model_alias": configured_model_alias,
        "resolved_model_name": resolved_model_name,
        "claude_code_version": claude_code_version,
        "provider_cli_version": provider_cli_version,
        "provider_adapter_version": provider_adapter_version,
    }
    if not success:
        failure_basis = cost_estimate_basis or COST_BASIS_NOT_APPLICABLE
        return UsageRecord(
            provider=provider, model_name=model_name, role=role, input_tokens=input_tokens,
            output_tokens=output_tokens, cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens, latency_ms=latency_ms,
            provider_request_id=provider_request_id, retry_count=retry_count, success=False,
            pricing_version=None, estimated_cost=None,
            cost_status=(COST_USAGE_NOT_RETURNED if failure_basis == COST_BASIS_USAGE_NOT_RETURNED else COST_NOT_APPLICABLE),
            cost_estimate_basis=failure_basis, **provenance,
            reasoning_output_tokens=reasoning_output_tokens, token_accounting_policy=token_accounting_policy,
        )

    if provider == "deterministic":
        return UsageRecord(
            provider=provider, model_name=model_name, role=role, input_tokens=input_tokens,
            output_tokens=output_tokens, cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens, latency_ms=latency_ms,
            provider_request_id=provider_request_id, retry_count=retry_count, success=True,
            pricing_version=None, estimated_cost=None, cost_status=COST_NOT_APPLICABLE,
            cost_estimate_basis=COST_BASIS_NOT_APPLICABLE, **provenance,
            reasoning_output_tokens=reasoning_output_tokens, token_accounting_policy=token_accounting_policy,
        )

    if input_tokens is None or output_tokens is None:
        return UsageRecord(
            provider=provider, model_name=model_name, role=role, input_tokens=input_tokens,
            output_tokens=output_tokens, cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens, latency_ms=latency_ms,
            provider_request_id=provider_request_id, retry_count=retry_count, success=True,
            pricing_version=None, estimated_cost=None, cost_status=COST_USAGE_NOT_RETURNED,
            cost_estimate_basis=COST_BASIS_USAGE_NOT_RETURNED, **provenance,
            reasoning_output_tokens=reasoning_output_tokens, token_accounting_policy=token_accounting_policy,
        )

    pricing = select_pricing(pricing_entries, provider, pricing_model or model_name, as_of_date or "9999-12-31")
    if pricing is None:
        return UsageRecord(
            provider=provider, model_name=model_name, role=role, input_tokens=input_tokens,
            output_tokens=output_tokens, cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens, latency_ms=latency_ms,
            provider_request_id=provider_request_id, retry_count=retry_count, success=True,
            pricing_version=None, estimated_cost=None, cost_status=COST_PRICING_NOT_CONFIGURED,
            cost_estimate_basis=cost_estimate_basis or COST_BASIS_DIRECT_API_ESTIMATE, **provenance,
            reasoning_output_tokens=reasoning_output_tokens, token_accounting_policy=token_accounting_policy,
        )

    cost = (Decimal(input_tokens) / Decimal(1_000_000)) * pricing.input_price_per_million + (
        Decimal(output_tokens) / Decimal(1_000_000)
    ) * pricing.output_price_per_million
    return UsageRecord(
        provider=provider, model_name=model_name, role=role, input_tokens=input_tokens,
        output_tokens=output_tokens, cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens, latency_ms=latency_ms,
        provider_request_id=provider_request_id, retry_count=retry_count, success=True,
        pricing_version=pricing.pricing_version, estimated_cost=cost, cost_status=COST_CALCULATED,
        cost_estimate_basis=cost_estimate_basis or COST_BASIS_DIRECT_API_ESTIMATE, **provenance,
        reasoning_output_tokens=reasoning_output_tokens, token_accounting_policy=token_accounting_policy,
    )
