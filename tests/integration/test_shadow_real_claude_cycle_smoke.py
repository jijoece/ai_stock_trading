"""Opt-in real Claude shadow-cycle smoke test (docs/milestone-7.md Step 28,
"Real Claude shadow-cycle smoke"). Gated on `RUN_REAL_CLAUDE_SHADOW_CYCLE=true`
AND a real `ANTHROPIC_API_KEY` (confirmed present in this session's `.env`,
boolean only — never printed).

Mirrors the scope of the existing Milestone 6.1 real Claude bear-role smoke
test (`tests/integration/test_research_claude_bear_smoke.py`): one symbol,
one immutable fixture-backed evidence snapshot (not a live network fetch —
this test targets the shadow-cycle orchestration/budget/no-execution path,
not evidence acquisition, exactly like the bear smoke test's own documented
scope), and a BOUNDED role set — `bear` + `manager` only (2 roles, not the
full 5-role default committee), matching "manager required" from the task
brief while staying as small as the already-validated Milestone 6.1
precedent.

Drives the real, unmodified `shadow/scheduler.py::run_due_shadow_cycle`
orchestrator end-to-end (never a reimplementation), with:
  * a strict cost cap (`max_estimated_cost_per_cycle_usd=0.50`) enforced via
    `shadow/budget.py::estimate_cycle_cost`/`reserve_budget`;
  * an in-memory pricing entry supplied directly to this test only (this
    repository's shipped `config/research_pricing.yaml` is deliberately
    empty by default — see that file's own docstring — so a pricing entry
    is constructed here rather than mutating shared repository config);
  * no paper submission, no execution — asserted explicitly;
  * exact real results (roles invoked, attempts, failures, token usage,
    latency, cost) printed and asserted, never fabricated.

This is a REAL Claude API call that costs real (small) money — the same
known-safe, bounded pattern already validated once in Milestone 6.1
(bear/v2 prompt, ~3588 input / 1878 output tokens, ~22s, VALID_REPORT, zero
failures).
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
    "opt-in real Claude shadow-cycle smoke test: set RUN_REAL_CLAUDE_SHADOW_CYCLE=true, "
    "ANTHROPIC_API_KEY, and RESEARCH_MODEL (or ANTHROPIC_MODEL) to run it"
)


@pytest.mark.skipif(not (_RUN_FLAG and _API_KEY and _MODEL), reason=_SKIP_REASON)
def test_real_claude_shadow_cycle_bear_manager_bounded_no_execution(tmp_path):
    import yaml

    from trading_research.analysis.scorer import load_scoring_config
    from trading_research.analysis.screener import load_screening_config
    from trading_research.evidence_providers.evidence_adapters import (
        RealFundamentalsEvidenceProvider,
        RealMarketEvidenceProvider,
    )
    from trading_research.evidence_providers.fixture_clients import FixtureMarketDataClient, FixtureSecClient
    from trading_research.hashing import hash_config
    from trading_research.models.trading_models import PortfolioState
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
    from trading_research.storage.research_cycle_repositories import SQLiteResearchCycleRepository
    from trading_research.storage.research_repositories import SQLiteResearchRepository
    from trading_research.storage.shadow_alerts_repositories import list_run_summaries
    from trading_research.storage.shadow_operations_repositories import list_budget_reservations, list_leases
    from trading_research.universe.tickers import default_universe

    # Unlike the narrower Milestone 6.1 bear-only smoke test (which never
    # imports research/scheduled_cycle.py at all, so a blanket
    # execution/paper/runtime import-absence check is meaningful there),
    # this test genuinely drives the real, unmodified scheduled-cycle
    # orchestrator end to end — and that orchestrator's own dependency
    # graph legitimately imports trading_research.execution.config
    # (ExecutionConfig, for typing) and trading_research.paper.ledger (for
    # ledger *read* access used elsewhere in that module), regardless of
    # whether any order is ever placed. A blanket "module never imported"
    # check would therefore be a false signal here; "no order was ever
    # submitted" is instead proven behaviorally below via
    # paper_submitter_calls (a real call-counting fixture) and
    # experiment_policy.may_submit_enhanced() being structurally False for
    # every supported policy — the same guarantee scheduled_cycle.py's own
    # code relies on.

    symbol = "AAPL"
    now = datetime.now(timezone.utc)

    # Immutable, fixture-backed evidence (not a live network fetch) — same
    # deliberate scope boundary as test_research_claude_bear_smoke.py.
    # `filings=None` (not RealFilingEvidenceProvider(sec)): FixtureSecClient's
    # `list_filings()` unconditionally returns `()` (it has no filing-fixture
    # data — see evidence_providers/fixture_clients.py), which would
    # otherwise contribute a permanent missing_data_reasons entry that
    # short-circuits analyze_with_research_committee to ANALYSIS_INCOMPLETE
    # before ever reaching Claude (confirmed during this test's own real
    # validation — see scratchpad "Bugs discovered and fixed" for the
    # writeup: this is a pre-existing fixture-completeness gap, not
    # something to patch, so this test avoids it by omission, matching
    # EvidenceProviderRegistry's own "excluded when not configured" contract).
    sec = FixtureSecClient()
    market = FixtureMarketDataClient()
    registry = EvidenceProviderRegistry(
        fundamentals=RealFundamentalsEvidenceProvider(sec), market=RealMarketEvidenceProvider(market),
        filings=None, news=None, sentiment=None, portfolio_context=None,
        market_data_client=market, sec_client=sec,
    )

    # Bounded role set: bear + manager only (2 roles, manager required).
    research_config = ResearchConfiguration(
        version=1, enabled=True, provider="anthropic", model=_MODEL, max_attempts_per_role=2,
        request_timeout_seconds=60, max_input_characters=100_000, max_evidence_items=100,
        max_items_per_source_category=25, max_claims_per_role=20, max_output_tokens=4000,
        require_point_in_time_safe=True, require_evidence_for_material_claims=True,
        fail_on_stale_required_evidence=True, allow_parallel_roles=False, roles=("bear", "manager"),
        overlay_policy_version="real-claude-shadow-smoke.v1", overlay_allow_score_increase=False,
        overlay_allow_position_size_increase=False, overlay_incomplete_action="ANALYSIS_INCOMPLETE",
        overlay_critical_risk_action="FORCE_NO_ACTION", config_hash="d" * 64, raw={},
    )

    cycle_configuration = ScheduledResearchConfiguration(
        universe_id="real-claude-shadow-smoke", max_candidates_per_cycle=1,
        experiment_policy=experiment_policy.SHADOW_ENHANCED, submit_paper_orders=False,
        require_complete_evidence=False, require_point_in_time_safe=False, continue_on_symbol_failure=True,
        provider_mode=PROVIDER_MODE_REAL, config_hash=hash_config({"real-claude-shadow-smoke": 1}),
    )

    # In-memory pricing entry for THIS TEST ONLY — config/research_pricing.yaml
    # ships empty by design (see that file's own docstring); a real pricing
    # figure is not fabricated globally, only supplied locally so this
    # specific reservation can be estimated and the strict cost cap enforced.
    # Uses a conservative placeholder rate; the real per-attempt cost is
    # computed separately from real token usage below regardless of whether
    # this estimate is precise.
    #
    # KNOWN LIMITATION discovered during this test's real validation (see
    # scratchpad "Known limitations" for the full writeup): shadow/scheduler.py's
    # `CycleIntent` construction (Step 5) always passes `model_name=None` — it
    # has no way to know the real research model name at budget-reservation
    # time, since `ScheduledResearchConfiguration` (its `cycle_configuration`
    # parameter) carries no model field at all; the model name only exists
    # inside `cycle_kwargs_builder`'s returned dict, which the scheduler never
    # inspects. `budget.py::estimate_cycle_cost` therefore always looks up
    # pricing keyed on `model=""`, never the real model name, for every
    # real-Claude scheduled cycle today. This is a structural signature gap
    # (would need a new parameter threaded through `run_due_shadow_cycle`/
    # `CycleIntent`), not a one-line bug, so it is deliberately NOT patched
    # here (out of this task's "no design changes" scope) — this test's
    # pricing entry instead keys on model="" to match the orchestrator's
    # actual (buggy) lookup, so the reservation and cost cap can still be
    # exercised honestly; the real per-attempt cost reported below is pulled
    # directly from research_attempts, independent of this workaround.
    # Two entries: one keyed model="" for shadow/scheduler.py's own (buggy,
    # see limitation note above) cycle-level cost-estimate lookup, and one
    # keyed on the real model name for AnthropicResearchProvider's own
    # per-attempt usage/cost recording (research/anthropic_provider.py calls
    # build_usage_record with the real request.model_name, a separate,
    # correctly-model-keyed pricing lookup from scheduler.py's).
    pricing_entries = (
        PricingEntry(
            provider="anthropic", model="", effective_date="2026-01-01", currency="USD",
            input_price_per_million=Decimal("3.00"), output_price_per_million=Decimal("15.00"),
            pricing_version="real-claude-shadow-smoke-pricing-v1",
        ),
        PricingEntry(
            provider="anthropic", model=_MODEL, effective_date="2026-01-01", currency="USD",
            input_price_per_million=Decimal("3.00"), output_price_per_million=Decimal("15.00"),
            pricing_version="real-claude-shadow-smoke-pricing-v1",
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
            # Strict cost cap: max_calls = 1 symbol * 2 roles * 2 attempts = 4;
            # max_output_tokens = 4 * 4000 = 16000 -> worst-case estimated
            # cost at the pricing above = 16000/1e6 * 15.00 = $0.24, safely
            # under the $0.50 cap this task requires.
            "require_pricing_for_real_claude": True, "max_symbols_per_cycle": 1, "max_roles_per_symbol": 2,
            "max_attempts_per_role": 2, "max_input_tokens_per_cycle": 20000, "max_output_tokens_per_cycle": 4000,
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

    db_path = tmp_path / "real_claude_shadow_smoke.db"
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
            paper_submitter=_paper_submitter, git_sha="real-claude-shadow-smoke",
        )

    result = run_due_shadow_cycle(
        now=now, conn=conn, shadow_config=shadow_config, cycle_configuration=cycle_configuration,
        candidate_symbols=lambda: (symbol,), run_cycle=run_scheduled_research_cycle,
        cycle_kwargs_builder=_cycle_kwargs_builder, pricing_entries=pricing_entries, clock=lambda: now,
    )

    # --- Real usage/cost pulled directly from research_attempts (the
    # per-attempt truth), never fabricated. scheduler.py's own
    # budget_consumed_usd is documented to always read $0 for a run_cycle
    # that doesn't feed real usage back into shadow/budget.py (see
    # scheduler.py Step 8's own docstring) — this is a known, honestly
    # reported structural gap, not silently papered over here.
    attempt_rows = conn.execute(
        "SELECT role, attempt_number, success, failure_reason, input_tokens, output_tokens, latency_ms, "
        "pricing_version, estimated_cost, cost_status FROM research_attempts ORDER BY role, attempt_number"
    ).fetchall()
    failure_rows = conn.execute("SELECT role, code, message FROM research_attempt_failures").fetchall()

    roles_invoked = sorted({r["role"] for r in attempt_rows})
    total_input_tokens = sum(r["input_tokens"] or 0 for r in attempt_rows)
    total_output_tokens = sum(r["output_tokens"] or 0 for r in attempt_rows)
    total_latency_ms = sum(r["latency_ms"] or 0 for r in attempt_rows)
    pricing_versions = sorted({r["pricing_version"] for r in attempt_rows if r["pricing_version"]})
    cost_statuses = sorted({r["cost_status"] for r in attempt_rows})
    total_real_estimated_cost = sum(
        (Decimal(r["estimated_cost"]) for r in attempt_rows if r["estimated_cost"] is not None), Decimal("0")
    )

    summaries = list_run_summaries(conn)
    reservations = list_budget_reservations(conn)
    leases = list_leases(conn)
    conn.close()

    cost_status_report = "COST_CALCULATED" if total_real_estimated_cost > 0 else (
        cost_statuses[0] if cost_statuses else "COST_PRICING_NOT_CONFIGURED"
    )

    print(
        "Real Claude shadow-cycle smoke result: "
        f"status={result.status} scheduler_run_id={result.scheduler_run_id} "
        f"roles_invoked={roles_invoked} attempt_count={len(attempt_rows)} "
        f"failure_count={len(failure_rows)} failure_codes={sorted({f['code'] for f in failure_rows})} "
        f"total_input_tokens={total_input_tokens} total_output_tokens={total_output_tokens} "
        f"total_latency_ms={total_latency_ms} pricing_versions={pricing_versions} "
        f"cost_status={cost_status_report} total_estimated_cost_usd={total_real_estimated_cost} "
        f"budget_reserved_usd={result.budget_reserved_usd} budget_consumed_usd={result.budget_consumed_usd} "
        f"paper_submitter_calls={len(paper_submitter_calls)}"
    )

    # --- Real invocation actually happened.
    assert result.status in ("COMPLETED", "PARTIALLY_COMPLETE", "FAILED"), f"unexpected status {result.status}"
    assert len(attempt_rows) >= 1, "at least one real Claude attempt must have been recorded"
    assert set(roles_invoked) <= {"bear", "manager"}, "only the bounded bear+manager role set may ever be invoked"

    # --- Manager required: if the run reached a decision at all, manager
    # must have been invoked (bear-only would never produce a decision).
    if result.status == "COMPLETED":
        assert "manager" in roles_invoked, "manager must be invoked for a completed run in this bounded committee"

    # --- No paper submission, no execution: proven behaviorally (see
    # docstring/comment above on why a blanket module-import check does
    # not apply to this orchestrator-level test).
    assert paper_submitter_calls == [], "no paper submission may occur in a shadow-cycle smoke test"
    assert experiment_policy.may_submit_enhanced(experiment_policy.SHADOW_ENHANCED) is False

    # --- Strict cost cap enforced: the reservation (if one was made) never
    # exceeds the $0.50 cap configured above.
    if reservations:
        assert Decimal(reservations[0]["reserved_estimated_cost_usd"]) <= Decimal("0.50")
        assert reservations[0]["status"] == "SETTLED"

    # --- Lease released.
    if leases:
        assert leases[0]["status"] == "RELEASED"

    # --- Health result produced for a run that actually executed.
    if result.status != "BUDGET_REJECTED":
        assert len(summaries) == 1
