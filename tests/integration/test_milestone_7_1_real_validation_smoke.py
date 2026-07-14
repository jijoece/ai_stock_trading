"""Opt-in, bounded REAL validation for Milestone 7.1's runtime closure
(docs/milestone-7.1.md Step 23). Gated on `RUN_REAL_CLAUDE_SHADOW_CYCLE=true`
AND a real `ANTHROPIC_API_KEY` AND `RESEARCH_MODEL`/`ANTHROPIC_MODEL` — never
run by default `pytest tests/ -q`.

Drives the real, unmodified `shadow/scheduler.py::run_due_shadow_cycle` end
to end with:
  * a REAL SEC EDGAR corporate-status fetch (`SecEdgarClient` + a real
    `FilingDocumentClient` for the disclosure-composition path) — real
    network, no credentials required (SEC EDGAR needs none);
  * `FixtureMarketDataClient` for market data — `ALPACA_MARKET_DATA_API_KEY`/
    `_SECRET` are absent from `.env` this session, so market data is
    explicitly NOT real (asserted/reported below, never silently faked);
  * no real news, no real Reddit — both absent-credential this session;
  * a bounded role set (`bear` + `manager`), one attempt per role;
  * a strict cost cap enforced through the real `shadow/budget.py::reserve_budget`;
  * an in-memory `PricingEntry` supplied to THIS TEST ONLY — the shipped
    `config/research_pricing.yaml` stays empty;
  * `research_roles=("bear", "manager")` so `shadow/attempt_controller.py`
    genuinely gates each attempt before it happens;
  * no paper submission, no enhanced execution — asserted behaviorally.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal

import pytest

pytestmark = pytest.mark.real_claude_shadow_cycle

_RUN_FLAG = os.environ.get("RUN_REAL_CLAUDE_SHADOW_CYCLE", "").strip().lower() == "true"
_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
_MODEL = os.environ.get("RESEARCH_MODEL") or os.environ.get("ANTHROPIC_MODEL")

_SKIP_REASON = (
    "opt-in Milestone 7.1 real-validation smoke test: set RUN_REAL_CLAUDE_SHADOW_CYCLE=true, "
    "ANTHROPIC_API_KEY, and RESEARCH_MODEL (or ANTHROPIC_MODEL) to run it"
)


@pytest.mark.skipif(not (_RUN_FLAG and _API_KEY and _MODEL), reason=_SKIP_REASON)
def test_milestone_7_1_real_sec_and_claude_shadow_cycle_closure(tmp_path):
    import yaml

    from trading_research.analysis.scorer import load_scoring_config
    from trading_research.analysis.screener import load_screening_config
    from trading_research.evidence_providers.corporate_status_adapters import SecCorporateStatusProvider
    from trading_research.evidence_providers.evidence_adapters import (
        RealFilingEvidenceProvider,
        RealFundamentalsEvidenceProvider,
        RealMarketEvidenceProvider,
    )
    from trading_research.evidence_providers.filing_documents import FilingDocumentCache, FilingDocumentClient
    from trading_research.evidence_providers.fixture_clients import FixtureMarketDataClient
    from trading_research.evidence_providers.http_client import HttpJsonClient
    from trading_research.evidence_providers.rate_limits import MinIntervalRateLimiter
    from trading_research.evidence_providers.sec_provider import SecEdgarClient
    from trading_research.hashing import hash_config
    from trading_research.research import experiment_policy
    from trading_research.research.anthropic_provider import AnthropicProviderConfig, AnthropicResearchProvider
    from trading_research.research.configuration import ResearchConfiguration
    from trading_research.research.prompt_registry import PromptRegistry
    from trading_research.research.scheduled_cycle import (
        EvidenceProviderRegistry,
        PROVIDER_MODE_REAL,
        ScheduledResearchConfiguration,
        run_scheduled_research_cycle,
    )
    from trading_research.research.usage import PricingEntry
    from trading_research.shadow.config import load_shadow_operations_config
    from trading_research.shadow.scheduler import run_due_shadow_cycle
    from trading_research.storage.database import connect
    from trading_research.storage.research_cycle_repositories import SQLiteResearchCycleRepository, list_symbol_evidence_status
    from trading_research.storage.research_repositories import SQLiteResearchRepository
    from trading_research.storage.shadow_alerts_repositories import list_run_summaries
    from trading_research.storage.shadow_operations_repositories import (
        list_budget_reservations,
        list_leases,
        list_role_budget_checks,
    )
    from trading_research.universe.tickers import default_universe
    from trading_research.models.trading_models import PortfolioState

    symbol = "AAPL"
    now = datetime.now(timezone.utc)

    user_agent = "agentic-trading-desk-milestone7-1 real-validation (contact: research@example.com)"
    sec_http = HttpJsonClient(
        base_headers={"User-Agent": user_agent}, rate_limiter=MinIntervalRateLimiter(0.15),
        max_attempts=3, timeout_seconds=15, provider="sec-edgar",
    )
    sec = SecEdgarClient(http_client=sec_http, user_agent=user_agent)
    filing_document_client = FilingDocumentClient(
        user_agent=user_agent, rate_limiter=MinIntervalRateLimiter(0.15), cache=FilingDocumentCache(),
    )
    market = FixtureMarketDataClient()  # ALPACA_MARKET_DATA credentials absent this session
    market_data_is_real = False

    registry = EvidenceProviderRegistry(
        fundamentals=RealFundamentalsEvidenceProvider(sec), market=RealMarketEvidenceProvider(market),
        filings=RealFilingEvidenceProvider(sec), news=None, sentiment=None, portfolio_context=None,
        market_data_client=market, sec_client=sec,
        corporate_status=SecCorporateStatusProvider(sec, filing_document_client=filing_document_client),
    )

    research_config = ResearchConfiguration(
        version=1, enabled=True, provider="anthropic", model=_MODEL, max_attempts_per_role=1,
        request_timeout_seconds=60, max_input_characters=100_000, max_evidence_items=100,
        max_items_per_source_category=25, max_claims_per_role=20, max_output_tokens=4000,
        require_point_in_time_safe=False, require_evidence_for_material_claims=True,
        fail_on_stale_required_evidence=False, allow_parallel_roles=False, roles=("bear", "manager"),
        overlay_policy_version="m71-real-validation.v1", overlay_allow_score_increase=False,
        overlay_allow_position_size_increase=False, overlay_incomplete_action="ANALYSIS_INCOMPLETE",
        overlay_critical_risk_action="FORCE_NO_ACTION", config_hash="e" * 64, raw={},
    )

    cycle_configuration = ScheduledResearchConfiguration(
        universe_id="m71-real-validation", max_candidates_per_cycle=1,
        experiment_policy=experiment_policy.SHADOW_ENHANCED, submit_paper_orders=False,
        require_complete_evidence=False, require_point_in_time_safe=False, continue_on_symbol_failure=True,
        provider_mode=PROVIDER_MODE_REAL, config_hash=hash_config({"m71-real-validation": 1}),
    )

    # In-memory pricing entry for THIS TEST ONLY — config/research_pricing.yaml
    # ships empty by design. Now correctly keyed on the REAL model name
    # (docs/milestone-7.1.md Step 11 closed the model_name=None gap).
    pricing_entries = (
        PricingEntry(
            provider="anthropic", model=_MODEL, effective_date="2020-01-01", currency="USD",
            input_price_per_million=Decimal("3.00"), output_price_per_million=Decimal("15.00"),
            pricing_version="m71-real-validation-pricing-v1",
        ),
    )

    raw_shadow_config = {
        "version": 1,
        "shadow_operations": {
            "enabled": True, "mode": "SHADOW_ENHANCED", "allow_baseline_paper_submission": False,
            "allow_enhanced_submission": False, "require_market_open_day": False,
            "run_window_timezone": "UTC", "run_window_start": "00:00", "run_window_end": "23:59",
            "max_catch_up_cycles": 1, "lease_ttl_seconds": 600, "stale_run_timeout_seconds": 1200,
            "continue_on_symbol_failure": True,
        },
        "schedule": {"enabled": True, "cadence": "DAILY_MARKET_DAY", "intended_local_time": "00:00"},
        "budgets": {
            # max_calls = 1 symbol * 2 roles * 1 attempt = 2; max_output_tokens = 2*4000=8000
            # -> worst-case estimate = 8000/1e6*15.00 = $0.12, well under the $0.50 cap.
            "require_pricing_for_real_claude": True, "max_symbols_per_cycle": 1, "max_roles_per_symbol": 2,
            "max_attempts_per_role": 1, "max_input_tokens_per_cycle": 20000, "max_output_tokens_per_cycle": 4000,
            "max_latency_seconds_per_cycle": 120, "max_estimated_cost_per_cycle_usd": 0.50,
            "max_actual_cost_per_day_usd": 2.0, "max_actual_cost_per_month_usd": 10.0,
            "emergency_margin_fraction": 0.5,
        },
        "safety": {
            "pause_on_provider_failure_rate": 0.5, "pause_on_retry_exhaustion_rate": 0.5,
            "pause_on_unsupported_claim_rate": 0.25, "pause_on_reconciliation_mismatch": True,
            "pause_on_budget_breach": True,
        },
    }
    shadow_config_path = tmp_path / "shadow_operations.yaml"
    shadow_config_path.write_text(yaml.safe_dump(raw_shadow_config))
    shadow_config = load_shadow_operations_config(shadow_config_path)

    db_path = tmp_path / "m71_real_validation.db"
    conn = connect(db_path)

    paper_submitter_calls: list[str] = []

    def _paper_submitter(rec_id: str):
        paper_submitter_calls.append(rec_id)
        return None

    provider = AnthropicResearchProvider(
        AnthropicProviderConfig(api_key=_API_KEY, request_timeout_seconds=60, pricing_entries=pricing_entries)
    )

    def _cycle_kwargs_builder(symbols, as_of):
        return dict(
            cycle_repository=SQLiteResearchCycleRepository(conn), universe=default_universe(),
            screening_config=load_screening_config(), scoring_config=load_scoring_config(),
            evidence_providers=registry, research_provider=provider, research_provider_name="anthropic",
            research_model_name=_MODEL, research_configuration=research_config,
            research_repository=SQLiteResearchRepository(conn), prompt_registry=PromptRegistry(),
            portfolio=PortfolioState(account_equity=Decimal("100000"), settled_cash=Decimal("100000"), as_of=as_of),
            paper_submitter=_paper_submitter, git_sha="m71-real-validation",
        )

    result = run_due_shadow_cycle(
        now=now, conn=conn, shadow_config=shadow_config, cycle_configuration=cycle_configuration,
        candidate_symbols=lambda: (symbol,), run_cycle=run_scheduled_research_cycle,
        cycle_kwargs_builder=_cycle_kwargs_builder, pricing_entries=pricing_entries, clock=lambda: now,
        research_provider_name="anthropic", research_model_name=_MODEL, research_roles=research_config.roles,
    )

    attempt_rows = conn.execute(
        "SELECT role, attempt_number, success, input_tokens, output_tokens, latency_ms, "
        "pricing_version, estimated_cost, cost_status FROM research_attempts ORDER BY role, attempt_number"
    ).fetchall()
    role_checks = list_role_budget_checks(conn, scheduler_run_id=result.scheduler_run_id)
    assoc_rows = list_symbol_evidence_status(conn, result.cycle_id) if result.cycle_id else []
    summaries = list_run_summaries(conn)
    reservations = list_budget_reservations(conn)
    leases = list_leases(conn)

    total_input_tokens = sum(r["input_tokens"] or 0 for r in attempt_rows)
    total_output_tokens = sum(r["output_tokens"] or 0 for r in attempt_rows)
    total_latency_ms = sum(r["latency_ms"] or 0 for r in attempt_rows)
    total_priced_cost = sum(
        (Decimal(r["estimated_cost"]) for r in attempt_rows if r["estimated_cost"] is not None), Decimal("0"),
    )
    usage_rows = conn.execute(
        "SELECT u.actual_cost_usd FROM shadow_budget_usage_attempts a "
        "JOIN shadow_budget_usage u ON a.usage_id = u.usage_id"
    ).fetchall()
    consumed_from_usage_table = sum((Decimal(r["actual_cost_usd"]) for r in usage_rows), Decimal("0"))

    conn.close()

    # --- Sanitized report only: no raw prompts/responses/credentials.
    print(
        "Milestone 7.1 real validation: "
        f"scheduler_run_id={result.scheduler_run_id} cycle_id={result.cycle_id} symbol={symbol} "
        f"provider=anthropic model={_MODEL} "
        f"corporate_status_id={assoc_rows[0]['corporate_status_evidence_id'] if assoc_rows else None} "
        f"screening_completeness={assoc_rows[0]['screening_completeness'] if assoc_rows else None} "
        f"role_budget_check_count={len(role_checks)} "
        f"role_budget_decisions={[c['decision'] for c in role_checks]} "
        f"attempt_count={len(attempt_rows)} input_tokens={total_input_tokens} output_tokens={total_output_tokens} "
        f"latency_ms={total_latency_ms} reserved_cost_usd={result.budget_reserved_usd} "
        f"consumed_cost_usd={result.budget_consumed_usd} priced_attempt_cost_usd={total_priced_cost} "
        f"health_status={summaries[0]['health_status'] if summaries else None} "
        f"health_reasons={summaries[0]['health_reasons_json'] if summaries else None} "
        f"cycle_status={result.status} "
        f"paper_submission_count={len(paper_submitter_calls)} market_data_is_real={market_data_is_real}"
    )

    assert result.status in ("COMPLETED", "PARTIALLY_COMPLETE", "FAILED"), f"unexpected status {result.status}"

    # --- Corporate status was fetched from real SEC and entered the snapshot.
    assert assoc_rows, "corporate-status/completeness association must be persisted"
    assert assoc_rows[0]["corporate_status_evidence_id"] is not None
    assert assoc_rows[0]["completeness_result_id"] is not None
    assert assoc_rows[0]["policy_version"] == "evidence-completeness-v1"

    # --- CycleIntent contained the actual model; model-specific pricing matched;
    # reserved estimated cost > 0.
    assert result.budget_reserved_usd is not None
    assert Decimal(result.budget_reserved_usd) > 0

    # --- Role-budget check persisted before EACH role actually attempted.
    assert len(role_checks) >= 1
    assert all(c["model_name"] == _MODEL for c in role_checks)
    checked_roles = {c["role"] for c in role_checks}
    attempted_roles = {r["role"] for r in attempt_rows}
    assert attempted_roles <= checked_roles, "every attempted role must have a persisted pre-attempt check"

    # --- Real Claude attempts occurred only after approved checks (no attempt
    # for a role whose only check was a denial).
    for role in attempted_roles:
        role_decisions = [c["decision"] for c in role_checks if c["role"] == role]
        assert "PROCEED" in role_decisions

    if attempt_rows:
        # --- Actual input/output tokens > 0, actual latency > 0.
        assert total_input_tokens > 0
        assert total_output_tokens > 0
        assert total_latency_ms > 0
        # --- Priced consumed cost > 0, and equals the persisted attempt-level priced usage.
        assert total_priced_cost > 0
        assert consumed_from_usage_table == total_priced_cost

    # --- Health received real retry/token/latency/cost values; run summary
    # contains the same values.
    if summaries:
        summary = summaries[0]
        if attempt_rows:
            assert summary["input_tokens"] == total_input_tokens
            assert summary["output_tokens"] == total_output_tokens

    # --- Zero paper submissions, zero enhanced executions.
    assert paper_submitter_calls == []
    assert experiment_policy.may_submit_enhanced(experiment_policy.SHADOW_ENHANCED) is False

    # --- Budget settled; lease released.
    if reservations:
        assert reservations[0]["status"] == "SETTLED"
    if leases:
        assert leases[0]["status"] == "RELEASED"
