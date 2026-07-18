"""Offline, deterministic end-to-end tests proving the Milestone 7.1 runtime
closure (docs/milestone-7.1.md Step 21): due scheduler invocation -> lease
acquired -> model/pricing resolved -> budget reserved -> fixture SEC filings
returned -> corporate status built -> corporate status normalized into
snapshot -> completeness persisted -> completeness allows/blocks research ->
role budget checked before each attempt -> scripted Claude call -> actual
usage recorded -> cycle telemetry aggregated -> consumed budget reconciles
-> health receives populated telemetry -> run summary persisted -> lease
released.

Every scenario drives the real, unmodified `shadow/scheduler.py::
run_due_shadow_cycle` -> `research/scheduled_cycle.py::run_scheduled_research_cycle`
-> `research/orchestration.py::analyze_with_research_committee` chain — never
a reimplementation. No network anywhere in this file.
"""
from __future__ import annotations

import socket
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from trading_research.analysis.scorer import load_scoring_config
from trading_research.analysis.screener import load_screening_config
from trading_research.evidence_providers.corporate_status_adapters import SecCorporateStatusProvider
from trading_research.evidence_providers.errors import ProviderError
from trading_research.evidence_providers.evidence_adapters import (
    RealFilingEvidenceProvider,
    RealFundamentalsEvidenceProvider,
    RealMarketEvidenceProvider,
)
from trading_research.evidence_providers.fixture_clients import FixtureMarketDataClient, FixtureSecClient
from trading_research.hashing import hash_config
from trading_research.models.trading_models import PortfolioState
from trading_research.research import experiment_policy
from trading_research.research.configuration import ResearchConfiguration, load_research_config
from trading_research.research.deterministic_provider import DeterministicResearchProvider, ScriptedResearchProvider, ScriptedStep
from trading_research.research.prompt_registry import PromptRegistry
from trading_research.research.scheduled_cycle import (
    EvidenceProviderRegistry,
    PROVIDER_MODE_FIXTURE,
    ScheduledResearchConfiguration,
    run_scheduled_research_cycle,
)
from trading_research.research.usage import PricingEntry
from trading_research.shadow import budget as budget_mod
from trading_research.shadow.config import load_shadow_operations_config
from trading_research.shadow.scheduler import STATUS_ALREADY_COMPLETED, STATUS_COMPLETED, run_due_shadow_cycle
from trading_research.storage.database import connect
from trading_research.storage.research_cycle_repositories import SQLiteResearchCycleRepository, list_symbol_evidence_status
from trading_research.storage.research_repositories import SQLiteResearchRepository
from trading_research.storage.shadow_alerts_repositories import list_run_summaries
from trading_research.storage.shadow_operations_repositories import list_role_budget_checks
from trading_research.universe.tickers import default_universe


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("milestone-7.1 shadow integration tests must never open a real socket")

    monkeypatch.setattr(socket.socket, "connect", _blocked)


DUE_NOW = datetime(2026, 7, 1, 23, 30, tzinfo=timezone.utc)

RAW_SHADOW_CONFIG = {
    "version": 1,
    "shadow_operations": {
        "enabled": True, "mode": "SHADOW_ENHANCED", "allow_baseline_paper_submission": False,
        "allow_enhanced_submission": False, "require_market_open_day": False,
        "run_window_timezone": "UTC", "run_window_start": "00:00", "run_window_end": "23:59",
        "max_catch_up_cycles": 1, "lease_ttl_seconds": 3600, "stale_run_timeout_seconds": 7200,
        "continue_on_symbol_failure": True,
    },
    "schedule": {"enabled": True, "cadence": "DAILY_MARKET_DAY", "intended_local_time": "23:30"},
    "budgets": {
        "require_pricing_for_real_claude": True, "max_symbols_per_cycle": 3, "max_roles_per_symbol": 5,
        "max_attempts_per_role": 2, "max_input_tokens_per_cycle": 100000, "max_output_tokens_per_cycle": 50000,
        "max_latency_seconds_per_cycle": 900, "max_estimated_cost_per_cycle_usd": 5.0,
        "max_actual_cost_per_day_usd": 10.0, "max_actual_cost_per_month_usd": 100.0,
        "emergency_margin_fraction": 0.1,
    },
    "safety": {
        "pause_on_provider_failure_rate": 0.5, "pause_on_retry_exhaustion_rate": 0.5,
        "pause_on_unsupported_claim_rate": 0.25, "pause_on_reconciliation_mismatch": True,
        "pause_on_budget_breach": True,
    },
}


def _shadow_config(**overrides):
    with tempfile.TemporaryDirectory() as tmp:
        raw = yaml.safe_load(yaml.safe_dump(RAW_SHADOW_CONFIG))
        for section, values in overrides.items():
            raw[section].update(values)
        path = Path(tmp) / "shadow_operations.yaml"
        path.write_text(yaml.safe_dump(raw))
        return load_shadow_operations_config(path)


def _cycle_configuration(**overrides) -> ScheduledResearchConfiguration:
    base = dict(
        universe_id="test-universe", max_candidates_per_cycle=3, experiment_policy=experiment_policy.SHADOW_ENHANCED,
        submit_paper_orders=False, require_complete_evidence=True, require_point_in_time_safe=True,
        continue_on_symbol_failure=True, provider_mode=PROVIDER_MODE_FIXTURE, config_hash=hash_config({"x": 1}),
    )
    base.update(overrides)
    return ScheduledResearchConfiguration(**base)


def _clock_at(t):
    return lambda: t


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "m71_e2e.db"


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


def _fixture_registry(*, sec=None) -> EvidenceProviderRegistry:
    sec = sec or FixtureSecClient()
    market = FixtureMarketDataClient()
    return EvidenceProviderRegistry(
        fundamentals=RealFundamentalsEvidenceProvider(sec), market=RealMarketEvidenceProvider(market),
        filings=RealFilingEvidenceProvider(sec), news=None, sentiment=None, portfolio_context=None,
        market_data_client=market, sec_client=sec, corporate_status=SecCorporateStatusProvider(sec),
    )


class _UnavailableSecClient(FixtureSecClient):
    """list_filings always raises -> corporate status resolves to
    SOURCE_UNAVAILABLE -> has_any_critical_uncertainty() is True."""

    def list_filings(self, symbol, *, available_by, cik=None):
        raise ProviderError("SEC EDGAR unavailable (scripted)")


def _real_cycle_kwargs_builder(conn, *, provider=None, research_configuration=None, sec=None):
    def _builder(symbols, as_of):
        return dict(
            cycle_repository=SQLiteResearchCycleRepository(conn), universe=default_universe(),
            screening_config=load_screening_config(), scoring_config=load_scoring_config(),
            evidence_providers=_fixture_registry(sec=sec), research_provider=provider or DeterministicResearchProvider(),
            research_provider_name="deterministic", research_model_name="deterministic-v1",
            research_configuration=research_configuration or load_research_config(),
            research_repository=SQLiteResearchRepository(conn), prompt_registry=PromptRegistry(),
            portfolio=PortfolioState(account_equity=Decimal("100000"), settled_cash=Decimal("100000"), as_of=as_of),
            paper_submitter=None, git_sha="test-sha",
        )

    return _builder


def _two_role_config(**overrides) -> ResearchConfiguration:
    base = dict(
        version=1, enabled=True, provider="scripted", model="test-model", max_attempts_per_role=2,
        request_timeout_seconds=30, max_input_characters=100_000, max_evidence_items=100,
        max_items_per_source_category=25, max_claims_per_role=20, max_output_tokens=2000,
        require_point_in_time_safe=True, require_evidence_for_material_claims=True,
        fail_on_stale_required_evidence=True, allow_parallel_roles=False, roles=("fundamental", "manager"),
        overlay_policy_version="test.v1", overlay_allow_score_increase=False,
        overlay_allow_position_size_increase=False, overlay_incomplete_action="ANALYSIS_INCOMPLETE",
        overlay_critical_risk_action="FORCE_NO_ACTION", config_hash="c" * 64, raw={},
    )
    base.update(overrides)
    return ResearchConfiguration(**base)


ANALYST_PAYLOAD = {
    "stance": "BULLISH", "summary": "growth", "claims": [], "catalysts": [], "risks": ["some risk"],
    "uncertainties": [], "missing_data_reasons": [],
}
MANAGER_PAYLOAD = {
    "rating": "OVERWEIGHT", "confidence": 0.6, "thesis": "t", "bull_case": "bull", "bear_case": "bear",
    "catalysts": [], "risks": ["some risk"], "invalidation_conditions": [], "claims": [], "evidence_ids": [],
    "missing_data_reasons": [],
}


# =============================================================================
# 1. Full happy path
# =============================================================================


def test_full_happy_path_role_budget_checked_telemetry_and_settlement(conn):
    """Real corporate-status provider + real completeness gate (non-blocking)
    + role-budget-checked deterministic Claude committee + real cycle
    telemetry + populated health summary + settled budget + released lease.
    """
    shadow_config = _shadow_config()
    cycle_configuration = _cycle_configuration()
    research_config = load_research_config()

    result = run_due_shadow_cycle(
        now=DUE_NOW, conn=conn, shadow_config=shadow_config, cycle_configuration=cycle_configuration,
        candidate_symbols=lambda: ("AAPL",), run_cycle=run_scheduled_research_cycle,
        cycle_kwargs_builder=_real_cycle_kwargs_builder(conn), pricing_entries=(), clock=_clock_at(DUE_NOW),
        research_provider_name="deterministic", research_model_name="deterministic-v1",
        research_roles=research_config.roles,
    )

    assert result.status == STATUS_COMPLETED
    assert result.cycle_id is not None

    # --- Corporate status + completeness persisted and associated with the cycle/symbol.
    assoc_rows = list_symbol_evidence_status(conn, result.cycle_id)
    assert len(assoc_rows) == 1
    assoc = assoc_rows[0]
    assert assoc["corporate_status_evidence_id"] is not None
    assert assoc["completeness_result_id"] is not None
    assert assoc["screening_completeness"] == "COMPLETE_FOR_SCREENING"
    assert assoc["policy_version"] == "evidence-completeness-v1"

    # --- Role-budget checks persisted, one per (role, attempt) actually invoked, all PROCEED.
    checks = list_role_budget_checks(conn, scheduler_run_id=result.scheduler_run_id)
    assert len(checks) == len(research_config.roles)
    assert {c["role"] for c in checks} == set(research_config.roles)
    assert all(c["decision"] == "PROCEED" for c in checks)
    assert all(c["symbol"] == "AAPL" for c in checks)

    # --- Cycle telemetry reached the health summary (Step 16/18): real, not None.
    summaries = list_run_summaries(conn)
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["claude_role_success_rate"] == 1.0
    assert summary["retry_rate"] == 0.0
    assert summary["retry_exhaustion_rate"] == 0.0
    assert summary["unsupported_claim_rate"] == 0.0
    assert summary["health_status"] == "HEALTHY"

    # --- Budget reservation settled.
    reservation = budget_mod.load_budget_reservation(conn, result.budget_reservation_id)
    assert reservation["status"] == budget_mod.RESERVATION_STATUS_SETTLED

    # --- Lease released: re-acquiring the same key succeeds.
    from trading_research.shadow import lease as lease_mod

    lease_key = f"shadow-scheduler:{result.intended_schedule_id}"
    reacquired = lease_mod.acquire(conn, lease_key, "post-test-owner", 60, _clock_at(DUE_NOW))
    assert isinstance(reacquired, lease_mod.LeaseHandle)


# =============================================================================
# 2. Blocking completeness
# =============================================================================


def test_blocking_completeness_no_claude_call(conn):
    """Critical corporate status unknown -> completeness persisted and
    blocking -> Claude never called -> no role-budget check required ->
    explicit incomplete symbol result -> enhanced execution absent."""

    class _CountingProvider(DeterministicResearchProvider):
        calls = 0

        def generate_structured(self, request):
            _CountingProvider.calls += 1
            return super().generate_structured(request)

    shadow_config = _shadow_config()
    cycle_configuration = _cycle_configuration()
    research_config = load_research_config()

    result = run_due_shadow_cycle(
        now=DUE_NOW, conn=conn, shadow_config=shadow_config, cycle_configuration=cycle_configuration,
        candidate_symbols=lambda: ("AAPL",), run_cycle=run_scheduled_research_cycle,
        cycle_kwargs_builder=_real_cycle_kwargs_builder(
            conn, provider=_CountingProvider(), sec=_UnavailableSecClient(),
        ),
        pricing_entries=(), clock=_clock_at(DUE_NOW), research_provider_name="deterministic",
        research_model_name="deterministic-v1", research_roles=research_config.roles,
    )

    assert result.status == STATUS_COMPLETED  # baseline still built; enhanced arm blocked
    assert _CountingProvider.calls == 0, "Claude must never be called when corporate status is critically uncertain"

    assoc_rows = list_symbol_evidence_status(conn, result.cycle_id)
    assert len(assoc_rows) == 1
    assert assoc_rows[0]["screening_completeness"] == "MISSING_CRITICAL_CORPORATE_STATUS"

    checks = list_role_budget_checks(conn, scheduler_run_id=result.scheduler_run_id)
    assert checks == []

    symbol_row = conn.execute(
        "SELECT research_run_id FROM research_cycle_symbol_results WHERE cycle_id = ? AND symbol = 'AAPL'",
        (result.cycle_id,),
    ).fetchone()
    assert symbol_row["research_run_id"] is None


# =============================================================================
# 3. Mid-cycle budget exhaustion
# =============================================================================


def test_mid_role_budget_exhaustion_skips_remaining_role(conn):
    """The first role's real (scripted) usage consumes most of the
    reservation's output-token budget -> the second (manager) role's
    pre-attempt check is denied -> no manager provider call ->
    SKIPPED_BUDGET_EXHAUSTED persisted -> not counted as a provider
    failure -> cycle incomplete -> actual fundamental-role usage retained."""
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD, usage_overrides={"output_tokens": 1500, "input_tokens": 100}),
    })
    research_configuration = _two_role_config()

    # reserved_output_tokens = max_symbols(1) * max_roles(2) * max_attempts(1) * 1000 = 2000.
    # fundamental's real usage (1500) leaves 500 remaining — below manager's own
    # declared per-role max (1000) — denying the manager attempt.
    shadow_config = _shadow_config(
        budgets={"max_symbols_per_cycle": 1, "max_roles_per_symbol": 2, "max_output_tokens_per_cycle": 1000, "max_attempts_per_role": 1},
    )
    cycle_configuration = _cycle_configuration()

    result = run_due_shadow_cycle(
        now=DUE_NOW, conn=conn, shadow_config=shadow_config, cycle_configuration=cycle_configuration,
        candidate_symbols=lambda: ("AAPL",), run_cycle=run_scheduled_research_cycle,
        cycle_kwargs_builder=_real_cycle_kwargs_builder(conn, provider=provider, research_configuration=research_configuration),
        pricing_entries=(), clock=_clock_at(DUE_NOW), research_provider_name="scripted",
        research_model_name="test-model", research_roles=research_configuration.roles,
    )

    assert result.status == STATUS_COMPLETED

    checks = list_role_budget_checks(conn, scheduler_run_id=result.scheduler_run_id)
    decisions = {c["role"]: c["decision"] for c in checks}
    assert decisions["fundamental"] == "PROCEED"
    assert decisions["manager"] == "SKIPPED_BUDGET_EXHAUSTED"

    # scripted provider was called exactly once (fundamental) — manager was never invoked.
    assert len(provider.calls) == 1
    assert provider.calls[0].role == "fundamental"

    # actual fundamental-role usage was retained (not discarded because a later role was skipped).
    usage_rows = conn.execute("SELECT * FROM shadow_budget_usage_attempts").fetchall()
    assert len(usage_rows) == 1


# =============================================================================
# 4. Retry accounting
# =============================================================================


def test_retry_attempt_budget_checked_with_reduced_balance_and_usage_not_double_charged(conn):
    """First attempt rejected (schema-invalid) -> tokens/cost still charged
    for that attempt -> second attempt's role-budget check uses the reduced
    remaining balance -> second attempt succeeds -> both attempt usages
    retained -> no double charge."""
    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload={"stance": "BULLISH"}, usage_overrides={"output_tokens": 200, "input_tokens": 50}),  # missing required fields -> schema rejection
        ("fundamental", 2): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD, usage_overrides={"output_tokens": 200, "input_tokens": 50}),
        ("manager", 1): ScriptedStep(kind="response", payload=MANAGER_PAYLOAD, usage_overrides={"output_tokens": 200, "input_tokens": 50}),
    })
    research_configuration = _two_role_config(max_attempts_per_role=2)

    shadow_config = _shadow_config(budgets={"max_roles_per_symbol": 2, "max_attempts_per_role": 2, "max_output_tokens_per_cycle": 5000})
    cycle_configuration = _cycle_configuration()

    result = run_due_shadow_cycle(
        now=DUE_NOW, conn=conn, shadow_config=shadow_config, cycle_configuration=cycle_configuration,
        candidate_symbols=lambda: ("AAPL",), run_cycle=run_scheduled_research_cycle,
        cycle_kwargs_builder=_real_cycle_kwargs_builder(conn, provider=provider, research_configuration=research_configuration),
        pricing_entries=(), clock=_clock_at(DUE_NOW), research_provider_name="scripted",
        research_model_name="test-model", research_roles=research_configuration.roles,
    )

    assert result.status == STATUS_COMPLETED
    assert len(provider.calls) == 3  # fundamental attempt 1 (rejected), attempt 2 (ok), manager attempt 1

    checks = list_role_budget_checks(conn, scheduler_run_id=result.scheduler_run_id)
    fundamental_checks = sorted((c for c in checks if c["role"] == "fundamental"), key=lambda c: c["attempt_number"])
    assert [c["attempt_number"] for c in fundamental_checks] == [1, 2]
    assert all(c["decision"] == "PROCEED" for c in fundamental_checks)
    # Second attempt's recorded remaining budget is strictly less than the first's,
    # proving the first (rejected) attempt's usage was actually charged before the
    # second attempt's check ran.
    assert int(fundamental_checks[1]["remaining_output_tokens"]) < int(fundamental_checks[0]["remaining_output_tokens"])

    usage_rows = conn.execute("SELECT attempt_id FROM shadow_budget_usage_attempts ORDER BY recorded_at").fetchall()
    assert len(usage_rows) == 3  # fundamental attempt 1 + attempt 2 + manager attempt 1 — no double charge
    assert len(set(r["attempt_id"] for r in usage_rows)) == 3


# =============================================================================
# 5. Resume idempotency
# =============================================================================


def test_resume_idempotency_no_duplicate_calls_checks_or_usage(conn):
    """Completed cycle invoked again for the identical intended slot -> no
    new provider call -> no new Claude call -> no duplicate budget checks ->
    no duplicate usage -> no duplicate completeness result.

    With a first-ever run and `max_catch_up_cycles=1`, the actionable window
    includes yesterday's unresolved slot as well as today's — so the first
    two invocations resolve two genuinely DIFFERENT intended slots (yesterday,
    then today), each completed exactly once. Only the THIRD invocation (same
    `now`, no more actionable backlog) hits the identical, already-completed
    slot — that is the one this test asserts produces zero new side effects."""
    research_config = load_research_config()
    shadow_config = _shadow_config()
    cycle_configuration = _cycle_configuration()

    kwargs = dict(
        conn=conn, shadow_config=shadow_config, cycle_configuration=cycle_configuration,
        candidate_symbols=lambda: ("AAPL",), run_cycle=run_scheduled_research_cycle,
        cycle_kwargs_builder=_real_cycle_kwargs_builder(conn), pricing_entries=(),
        research_provider_name="deterministic", research_model_name="deterministic-v1",
        research_roles=research_config.roles,
    )

    first = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **kwargs)
    assert first.status == STATUS_COMPLETED
    second = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **kwargs)
    assert second.status == STATUS_COMPLETED
    assert second.intended_schedule_id != first.intended_schedule_id

    checks_after_second = list_role_budget_checks(conn)
    usage_after_second = conn.execute("SELECT COUNT(*) AS c FROM shadow_budget_usage_attempts").fetchone()["c"]
    completeness_after_second = conn.execute("SELECT COUNT(*) AS c FROM evidence_completeness_results").fetchone()["c"]

    third = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **kwargs)
    assert third.status == STATUS_ALREADY_COMPLETED
    assert third.intended_schedule_id == second.intended_schedule_id

    checks_after_third = list_role_budget_checks(conn)
    usage_after_third = conn.execute("SELECT COUNT(*) AS c FROM shadow_budget_usage_attempts").fetchone()["c"]
    completeness_after_third = conn.execute("SELECT COUNT(*) AS c FROM evidence_completeness_results").fetchone()["c"]

    assert len(checks_after_third) == len(checks_after_second)
    assert usage_after_third == usage_after_second
    assert completeness_after_third == completeness_after_second


# =============================================================================
# 6. Pricing failure
# =============================================================================


def test_pricing_failure_blocks_before_any_provider_call(conn):
    """anthropic provider + unknown model pricing -> fails before any Claude
    call, before lease work that could otherwise proceed to spend money."""
    shadow_config = _shadow_config()
    cycle_configuration = _cycle_configuration()

    class _ExplodingProvider:
        def generate_structured(self, request):
            raise AssertionError("must never be called — pricing preflight should block first")

    result = run_due_shadow_cycle(
        now=DUE_NOW, conn=conn, shadow_config=shadow_config, cycle_configuration=cycle_configuration,
        candidate_symbols=lambda: ("AAPL",), run_cycle=run_scheduled_research_cycle,
        cycle_kwargs_builder=_real_cycle_kwargs_builder(conn, provider=_ExplodingProvider()),
        pricing_entries=(), clock=_clock_at(DUE_NOW),
        research_provider_name="anthropic", research_model_name="claude-unknown-model",
    )

    assert result.status == "BUDGET_REJECTED"
    assert result.cycle_id is None
    assert result.failure_reason is not None and "pricing" in result.failure_reason.lower()

    # Lease was acquired-then-released (try/finally), never left held.
    from trading_research.shadow import lease as lease_mod

    lease_key = f"shadow-scheduler:{result.intended_schedule_id}"
    current = lease_mod.current_lease(conn, lease_key)
    assert current is None or current["status"] != lease_mod.LEASE_STATUS_HELD

    # No role-budget checks, no usage, no research attempts — nothing was spent.
    assert list_role_budget_checks(conn) == []
    assert conn.execute("SELECT COUNT(*) AS c FROM research_attempts").fetchone()["c"] == 0


def test_pricing_failure_cli_preflight_blocks_before_db_session(monkeypatch, db_path):
    """The CLI's own preflight (docs/milestone-7.1.md Step 20/21) rejects a
    real-mode anthropic run with unknown pricing before `session(db_path)`
    is ever opened — no lease/scheduler-run row exists afterward."""
    import trading_research.research.configuration as research_configuration_mod

    monkeypatch.setattr(
        research_configuration_mod, "load_research_config", lambda: _AnthropicResearchConfigStub(),
    )

    from trading_research import cli as cli_mod

    class _CfgStub:
        anthropic_api_key = "sk-test-present"

    monkeypatch.setattr(cli_mod, "load_config", lambda *a, **k: _CfgStub())

    result = cli_mod.run_due_shadow_cycle_cli(db_path, provider_mode="real", symbols=["AAPL"])
    assert result["status"] == "PRICING_NOT_CONFIGURED"
    assert not db_path.exists() or conn_has_no_scheduler_runs(db_path)


class _AnthropicResearchConfigStub:
    provider = "anthropic"
    model = "claude-unknown-model"
    request_timeout_seconds = 30
    roles = ("fundamental", "manager")

    def require_ready(self):
        return None


def conn_has_no_scheduler_runs(db_path: Path) -> bool:
    c = connect(db_path)
    try:
        return c.execute("SELECT COUNT(*) AS n FROM shadow_scheduler_runs").fetchone()["n"] == 0
    finally:
        c.close()
