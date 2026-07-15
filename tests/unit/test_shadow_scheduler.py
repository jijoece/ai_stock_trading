"""Tests for shadow/scheduler.py::run_due_shadow_cycle (docs/milestone-7.md
Step 18, Step 27 sections J/M). Offline, deterministic — no network, no
Claude, no `research/scheduled_cycle.py` modification.

Two tiers:
  * Orchestration-level tests use an injected `run_cycle` stub (never the
    real `run_scheduled_research_cycle`) to fast, deterministically exercise
    disabled/not-due/paused/killed/lease-held/budget-rejected/idempotent
    behavior without touching evidence providers at all.
  * One true end-to-end test wires the real, unmodified
    `run_scheduled_research_cycle` against the repository's existing
    fixture evidence providers (the same `FixtureSecClient`/
    `FixtureMarketDataClient` pattern `cli.py::_build_evidence_provider_registry`
    uses for `provider_mode="fixture"`), proving the scheduler really can
    drive one full cycle end to end offline.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from trading_research.hashing import hash_config
from trading_research.research.scheduled_cycle import (
    PROVIDER_MODE_FIXTURE,
    ScheduledResearchConfiguration,
    SymbolCycleResult,
)
from trading_research.shadow import pause as pause_mod
from trading_research.shadow.config import load_shadow_operations_config
from trading_research.shadow.scheduler import (
    STATUS_ALREADY_COMPLETED,
    STATUS_BUDGET_REJECTED,
    STATUS_COMPLETED,
    STATUS_DISABLED,
    STATUS_KILLED,
    STATUS_LEASE_HELD,
    STATUS_NOT_DUE,
    STATUS_PAUSED,
    run_due_shadow_cycle,
)
from trading_research.storage.database import connect

RAW_BASE = {
    "version": 1,
    "shadow_operations": {
        "enabled": True, "mode": "SHADOW_ENHANCED", "allow_baseline_paper_submission": False,
        "allow_enhanced_submission": False, "require_market_open_day": True,
        "run_window_timezone": "America/Los_Angeles", "run_window_start": "06:30", "run_window_end": "08:30",
        "max_catch_up_cycles": 1, "lease_ttl_seconds": 3600, "stale_run_timeout_seconds": 7200,
        "continue_on_symbol_failure": True,
    },
    "schedule": {"enabled": True, "cadence": "DAILY_MARKET_DAY", "intended_local_time": "06:45"},
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

from zoneinfo import ZoneInfo

LA = ZoneInfo("America/Los_Angeles")
DUE_NOW = datetime(2026, 7, 13, 7, 0, tzinfo=LA)  # Monday, within 06:30-08:30 window


def _shadow_config(**overrides):
    with tempfile.TemporaryDirectory() as tmp:
        raw = yaml.safe_load(yaml.safe_dump(RAW_BASE))
        for section, values in overrides.items():
            raw[section].update(values)
        path = Path(tmp) / "shadow_operations.yaml"
        path.write_text(yaml.safe_dump(raw))
        return load_shadow_operations_config(path)


def _cycle_configuration() -> ScheduledResearchConfiguration:
    raw = {"universe_id": "test-universe", "max_candidates_per_cycle": 3}
    return ScheduledResearchConfiguration(
        universe_id="test-universe", max_candidates_per_cycle=3, experiment_policy="SHADOW_ENHANCED",
        submit_paper_orders=False, require_complete_evidence=False, require_point_in_time_safe=False,
        continue_on_symbol_failure=True, provider_mode=PROVIDER_MODE_FIXTURE, config_hash=hash_config(raw),
    )


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "shadow_scheduler_test.db"


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


def _clock_at(t):
    return lambda: t


def _stub_run_cycle_success(*, as_of, symbols, configuration, conn, clock, **_kwargs):
    from trading_research.research.scheduled_cycle import ResearchCycleResult

    results = tuple(SymbolCycleResult(symbol=s, status="COMPLETED") for s in symbols)
    return ResearchCycleResult(
        cycle_id=f"cycle-{as_of.isoformat()}", universe_id=configuration.universe_id, as_of=as_of,
        status="COMPLETED", symbol_results=results, reused_existing_cycle=False,
    )


def _stub_run_cycle_raises(*, as_of, symbols, configuration, conn, clock, **_kwargs):
    raise RuntimeError("simulated cycle crash")


def _base_kwargs(conn, *, run_cycle=_stub_run_cycle_success, shadow_config=None, symbols=("AAPL",)):
    return dict(
        conn=conn, shadow_config=shadow_config or _shadow_config(), cycle_configuration=_cycle_configuration(),
        candidate_symbols=lambda: symbols, run_cycle=run_cycle,
        cycle_kwargs_builder=lambda syms, as_of: {}, pricing_entries=(),
    )


# --- DISABLED -----------------------------------------------------------------


def test_disabled_is_a_no_op_before_touching_anything(conn):
    shadow_config = _shadow_config(shadow_operations={"enabled": False})
    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn, shadow_config=shadow_config))
    assert result.status == STATUS_DISABLED
    assert result.is_successful_no_op
    # Nothing touched: no pause-state row, no lease row, no scheduler run row.
    from trading_research.storage.shadow_operations_repositories import (
        list_leases,
        list_pause_state_history,
        list_scheduler_runs,
    )
    assert list_pause_state_history(conn) == []
    assert list_leases(conn) == []
    assert list_scheduler_runs(conn) == []


def test_disabled_never_calls_run_cycle(conn):
    calls = []

    def _tracking_stub(**kwargs):
        calls.append(kwargs)
        return _stub_run_cycle_success(**kwargs)

    shadow_config = _shadow_config(shadow_operations={"enabled": False})
    run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn, run_cycle=_tracking_stub, shadow_config=shadow_config))
    assert calls == []


# --- NOT_DUE --------------------------------------------------------------


def test_not_due_before_intended_time(conn):
    early = datetime(2026, 7, 13, 5, 0, tzinfo=LA)
    result = run_due_shadow_cycle(now=early, clock=_clock_at(early), **_base_kwargs(conn))
    assert result.status == STATUS_NOT_DUE
    assert result.is_successful_no_op


# --- KILLED / PAUSED --------------------------------------------------------


def test_killed_blocks_before_lease_or_provider_call(conn):
    calls = []

    def _tracking_stub(**kwargs):
        calls.append(kwargs)
        return _stub_run_cycle_success(**kwargs)

    pause_mod.kill(conn, "critical safety issue", "jijo", clock=_clock_at(DUE_NOW))
    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn, run_cycle=_tracking_stub))
    assert result.status == STATUS_KILLED
    assert calls == []
    from trading_research.storage.shadow_operations_repositories import list_leases
    assert list_leases(conn) == []  # no lease acquired


def test_paused_blocks_before_lease(conn):
    pause_mod.request_pause(conn, "operator maintenance", pause_mod.SOURCE_OPERATOR, clock=_clock_at(DUE_NOW))
    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn))
    assert result.status == STATUS_PAUSED
    from trading_research.storage.shadow_operations_repositories import list_leases
    assert list_leases(conn) == []


# --- LEASE_HELD --------------------------------------------------------------


def test_lease_held_by_prior_invocation_blocks_second(conn):
    """Two sequential calls simulating a still-held lease from the first —
    the second call must not call run_cycle."""
    calls = []

    def _tracking_stub(**kwargs):
        calls.append(kwargs)
        return _stub_run_cycle_success(**kwargs)

    from trading_research.shadow import lease as lease_mod
    from trading_research.shadow import schedule as schedule_mod

    shadow_config = _shadow_config()
    intended_time = schedule_mod.intended_schedule_time_for_day(DUE_NOW.date(), shadow_config)
    intended_id = schedule_mod.intended_schedule_id(intended_time)
    lease_key = f"shadow-scheduler:{intended_id}"
    lease_mod.acquire(conn, lease_key, "some-other-owner", 3600, _clock_at(DUE_NOW))

    result = run_due_shadow_cycle(
        now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn, run_cycle=_tracking_stub, shadow_config=shadow_config)
    )
    assert result.status == STATUS_LEASE_HELD
    assert result.is_successful_no_op
    assert calls == []


# --- BUDGET_REJECTED ---------------------------------------------------------


def test_budget_rejected_releases_lease_and_records_run(conn):
    """anthropic provider with no pricing configured -> BudgetConfigError ->
    BUDGET_REJECTED, lease released, run recorded."""
    cycle_configuration = ScheduledResearchConfiguration(
        universe_id="test-universe", max_candidates_per_cycle=3, experiment_policy="SHADOW_ENHANCED",
        submit_paper_orders=False, require_complete_evidence=False, require_point_in_time_safe=False,
        continue_on_symbol_failure=True, provider_mode="real", config_hash=hash_config({"x": 1}),
    )
    shadow_config = _shadow_config()
    result = run_due_shadow_cycle(
        now=DUE_NOW, conn=conn, shadow_config=shadow_config, cycle_configuration=cycle_configuration,
        candidate_symbols=lambda: ("AAPL",), run_cycle=_stub_run_cycle_success,
        cycle_kwargs_builder=lambda syms, as_of: {}, pricing_entries=(), clock=_clock_at(DUE_NOW),
        research_provider_name="anthropic", research_model_name="claude-test-model",
    )
    assert result.status == STATUS_BUDGET_REJECTED
    from trading_research.storage.shadow_operations_repositories import list_leases, list_scheduler_runs
    leases = list_leases(conn)
    assert len(leases) == 1
    assert leases[0]["status"] == "RELEASED"
    runs = list_scheduler_runs(conn)
    assert len(runs) == 1
    assert runs[0]["status"] == STATUS_BUDGET_REJECTED


# --- Successful due-and-not-lease-held path ----------------------------------


def test_due_path_invokes_run_cycle_and_completes(conn):
    calls = []

    def _tracking_stub(**kwargs):
        calls.append(kwargs)
        return _stub_run_cycle_success(**kwargs)

    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn, run_cycle=_tracking_stub))
    assert result.status == STATUS_COMPLETED
    assert len(calls) == 1
    assert result.symbols_completed == 1
    assert result.cycle_id is not None

    from trading_research.storage.shadow_operations_repositories import list_leases, list_scheduler_runs
    # Lease released after completion.
    leases = list_leases(conn)
    assert len(leases) == 1
    assert leases[0]["status"] == "RELEASED"
    runs = list_scheduler_runs(conn)
    assert len(runs) == 1
    assert runs[0]["status"] == STATUS_COMPLETED
    assert runs[0]["symbols_completed"] == 1


def test_rerun_of_completed_intended_slot_is_idempotent_no_duplicate_call(conn):
    calls = []

    def _tracking_stub(**kwargs):
        calls.append(kwargs)
        return _stub_run_cycle_success(**kwargs)

    kwargs = _base_kwargs(conn, run_cycle=_tracking_stub)
    first = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **kwargs)
    assert first.status == STATUS_COMPLETED
    assert len(calls) == 1

    second = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **kwargs)
    assert second.status == STATUS_ALREADY_COMPLETED
    assert second.is_successful_no_op
    assert len(calls) == 1  # no duplicate call


def test_partial_cycle_visible_in_scheduler_runs_on_crash(conn):
    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn, run_cycle=_stub_run_cycle_raises))
    from trading_research.storage.shadow_operations_repositories import list_scheduler_runs

    assert result.status == "FAILED"
    assert result.failure_reason is not None
    runs = list_scheduler_runs(conn)
    assert len(runs) == 1
    assert runs[0]["status"] == "FAILED"
    assert runs[0]["failure_reason"] is not None
    # Lease still released despite the crash.
    from trading_research.storage.shadow_operations_repositories import list_leases
    assert list_leases(conn)[0]["status"] == "RELEASED"


# --- Milestone 8.1: optional paper_book_integrator hook -----------------------


def test_paper_book_integrator_not_supplied_is_zero_behavior_change(conn):
    """Default `paper_book_integrator=None` — every pre-existing caller's
    exact behavior, including the new result fields staying `None`."""
    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn))
    assert result.status == STATUS_COMPLETED
    assert result.paper_book_integration_status is None
    assert result.paper_book_integration_reason is None


def test_paper_book_integrator_invoked_after_cycle_result_and_recorded(conn):
    calls = []

    def _integrator(cycle_result, intended_schedule_time):
        calls.append((cycle_result, intended_schedule_time))

    result = run_due_shadow_cycle(
        now=DUE_NOW, clock=_clock_at(DUE_NOW), paper_book_integrator=_integrator, **_base_kwargs(conn),
    )
    assert result.status == STATUS_COMPLETED
    assert len(calls) == 1
    assert calls[0][0].cycle_id == result.cycle_id  # invoked with the real cycle_result, not a copy/guess
    assert result.paper_book_integration_status == "INTEGRATED"
    assert result.paper_book_integration_reason is None


def test_paper_book_integrator_exception_is_recorded_not_raised_not_misclassified(conn):
    def _failing_integrator(cycle_result, intended_schedule_time):
        raise RuntimeError("simulated paper-book integration failure")

    result = run_due_shadow_cycle(
        now=DUE_NOW, clock=_clock_at(DUE_NOW), paper_book_integrator=_failing_integrator, **_base_kwargs(conn),
    )
    # The cycle itself still completed successfully — a paper-book failure
    # must never mutate/invalidate the frozen research result.
    assert result.status == STATUS_COMPLETED
    assert result.symbols_completed == 1
    assert result.paper_book_integration_status == "FAILED"
    assert "simulated paper-book integration failure" in result.paper_book_integration_reason
    # Never folded into the cycle-level failure_reason (which stays reserved
    # for the Claude/cycle-crash path) — never mislabeled as a provider failure.
    assert result.failure_reason is None


def test_paper_book_integrator_never_invoked_when_cycle_crashes(conn):
    calls = []

    def _integrator(cycle_result, intended_schedule_time):
        calls.append(cycle_result)

    result = run_due_shadow_cycle(
        now=DUE_NOW, clock=_clock_at(DUE_NOW), paper_book_integrator=_integrator,
        **_base_kwargs(conn, run_cycle=_stub_run_cycle_raises),
    )
    assert result.status == "FAILED"
    assert len(calls) == 0  # no frozen recommendations exist yet — nothing to integrate
    assert result.paper_book_integration_status is None


# --- Crash recovery via lease TTL expiry -------------------------------------


def test_crash_recovery_stale_lease_allows_subsequent_invocation(conn):
    """Simulates a crashed first invocation that acquired the lease but
    never released it (e.g. process killed mid-cycle) — a later invocation,
    once the lease TTL has expired, must be able to proceed."""
    from trading_research.shadow import lease as lease_mod
    from trading_research.shadow import schedule as schedule_mod

    shadow_config = _shadow_config(shadow_operations={"lease_ttl_seconds": 60})
    intended_time = schedule_mod.intended_schedule_time_for_day(DUE_NOW.date(), shadow_config)
    intended_id = schedule_mod.intended_schedule_id(intended_time)
    lease_key = f"shadow-scheduler:{intended_id}"
    # Simulate a crashed prior process: lease acquired, never released.
    lease_mod.acquire(conn, lease_key, "crashed-owner", 60, _clock_at(DUE_NOW))

    later = DUE_NOW + timedelta(seconds=120)  # past the 60s TTL
    calls = []

    def _tracking_stub(**kwargs):
        calls.append(kwargs)
        return _stub_run_cycle_success(**kwargs)

    result = run_due_shadow_cycle(
        now=later, clock=_clock_at(later), **_base_kwargs(conn, run_cycle=_tracking_stub, shadow_config=shadow_config)
    )
    assert result.status == STATUS_COMPLETED
    assert len(calls) == 1


# --- True end-to-end: real run_scheduled_research_cycle + fixture providers -


def test_end_to_end_real_scheduled_cycle_with_fixture_providers(conn):
    """Wires the actual, unmodified `run_scheduled_research_cycle` against
    this repository's existing fixture evidence providers (the same pattern
    `cli.py::_build_evidence_provider_registry(provider_mode="fixture")`
    uses) — proves the scheduler genuinely drives one full offline cycle,
    not just a mocked stand-in."""
    from decimal import Decimal

    from trading_research.analysis.scorer import load_scoring_config
    from trading_research.analysis.screener import load_screening_config
    from trading_research.evidence_providers.evidence_adapters import (
        RealFilingEvidenceProvider,
        RealFundamentalsEvidenceProvider,
        RealMarketEvidenceProvider,
    )
    from trading_research.evidence_providers.fixture_clients import FixtureMarketDataClient, FixtureSecClient
    from trading_research.models.trading_models import PortfolioState
    from trading_research.research.configuration import load_research_config
    from trading_research.research.deterministic_provider import DeterministicResearchProvider
    from trading_research.research.prompt_registry import PromptRegistry
    from trading_research.research.scheduled_cycle import EvidenceProviderRegistry, run_scheduled_research_cycle
    from trading_research.storage.research_cycle_repositories import SQLiteResearchCycleRepository
    from trading_research.storage.research_repositories import SQLiteResearchRepository

    sec = FixtureSecClient()
    market = FixtureMarketDataClient()
    registry = EvidenceProviderRegistry(
        fundamentals=RealFundamentalsEvidenceProvider(sec), market=RealMarketEvidenceProvider(market),
        filings=RealFilingEvidenceProvider(sec), news=None, sentiment=None, portfolio_context=None,
        market_data_client=market, sec_client=sec,
    )
    research_config = load_research_config()

    def _real_cycle_kwargs_builder(symbols, as_of):
        from trading_research.universe.tickers import default_universe

        return dict(
            cycle_repository=SQLiteResearchCycleRepository(conn), universe=default_universe(),
            screening_config=load_screening_config(), scoring_config=load_scoring_config(),
            evidence_providers=registry, research_provider=DeterministicResearchProvider(),
            research_provider_name="deterministic", research_model_name="deterministic-v1",
            research_configuration=research_config, research_repository=SQLiteResearchRepository(conn),
            prompt_registry=PromptRegistry(),
            portfolio=PortfolioState(account_equity=Decimal("100000"), settled_cash=Decimal("100000"), as_of=as_of),
            paper_submitter=None, git_sha="test-sha",
        )

    result = run_due_shadow_cycle(
        now=DUE_NOW, conn=conn, shadow_config=_shadow_config(), cycle_configuration=_cycle_configuration(),
        candidate_symbols=lambda: ("AAPL",), run_cycle=run_scheduled_research_cycle,
        cycle_kwargs_builder=_real_cycle_kwargs_builder, pricing_entries=(), clock=_clock_at(DUE_NOW),
    )
    assert result.status in (STATUS_COMPLETED, "PARTIALLY_COMPLETE")
    assert result.cycle_id is not None
    assert result.symbols_attempted == 1


# --- Part 11 (docs/milestone-7.2.md): no automatic resume/kill-clear --------


def test_scheduler_never_calls_resume_or_force_clear_kill():
    """docs/milestone-7.2.md Part 11: no automatic resume, no automatic kill
    clearing. Structural (AST-based) check on the orchestrator module
    itself, mirroring `test_shadow_health.py::
    test_apply_health_result_never_calls_resume`'s approach for `health.py`."""
    import ast
    import trading_research.shadow.scheduler as scheduler_module

    source = Path(scheduler_module.__file__).read_text()
    tree = ast.parse(source)
    forbidden_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("resume", "force_clear_kill")
    ]
    assert forbidden_calls == []


# --- Part 1 (this session): health/alerts wiring -----------------------------


def test_disabled_no_op_writes_run_summary_row(conn):
    """Every invocation type — including a lightweight successful no-op —
    must get a `shadow_run_summaries` row (this task's explicit
    requirement)."""
    shadow_config = _shadow_config(shadow_operations={"enabled": False})
    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn, shadow_config=shadow_config))
    assert result.status == STATUS_DISABLED
    from trading_research.storage.shadow_alerts_repositories import list_run_summaries
    summaries = list_run_summaries(conn)
    assert len(summaries) == 1
    assert summaries[0]["health_status"] == "NOT_EVALUATED"


def test_not_due_no_op_writes_run_summary_row(conn):
    early = datetime(2026, 7, 13, 5, 0, tzinfo=LA)
    run_due_shadow_cycle(now=early, clock=_clock_at(early), **_base_kwargs(conn))
    from trading_research.storage.shadow_alerts_repositories import list_run_summaries
    summaries = list_run_summaries(conn)
    assert len(summaries) == 1


def test_killed_raises_kill_switch_alert(conn):
    pause_mod.kill(conn, "critical safety issue", "jijo", clock=_clock_at(DUE_NOW))
    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn))
    assert result.status == STATUS_KILLED
    from trading_research.storage.shadow_alerts_repositories import list_alerts, list_run_summaries
    alerts = list_alerts(conn)
    assert len(alerts) == 1
    assert alerts[0]["alert_type"] == "KILL_SWITCH_ACTIVATED"
    assert alerts[0]["severity"] == "CRITICAL"
    assert len(list_run_summaries(conn)) == 1


def test_paused_raises_pause_activated_alert(conn):
    pause_mod.request_pause(conn, "operator maintenance", pause_mod.SOURCE_OPERATOR, clock=_clock_at(DUE_NOW))
    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn))
    assert result.status == STATUS_PAUSED
    from trading_research.storage.shadow_alerts_repositories import list_alerts
    alerts = list_alerts(conn)
    assert len(alerts) == 1
    assert alerts[0]["alert_type"] == "PAUSE_ACTIVATED"


def test_lease_conflict_raises_lease_conflict_alert(conn):
    from trading_research.shadow import lease as lease_mod
    from trading_research.shadow import schedule as schedule_mod

    shadow_config = _shadow_config()
    intended_time = schedule_mod.intended_schedule_time_for_day(DUE_NOW.date(), shadow_config)
    intended_id = schedule_mod.intended_schedule_id(intended_time)
    lease_key = f"shadow-scheduler:{intended_id}"
    lease_mod.acquire(conn, lease_key, "some-other-owner", 3600, _clock_at(DUE_NOW))

    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn, shadow_config=shadow_config))
    assert result.status == STATUS_LEASE_HELD
    from trading_research.storage.shadow_alerts_repositories import list_alerts, list_run_summaries
    alerts = list_alerts(conn)
    assert len(alerts) == 1
    assert alerts[0]["alert_type"] == "LEASE_CONFLICT"
    assert len(list_run_summaries(conn)) == 1


def test_budget_rejected_raises_budget_exceeded_alert(conn):
    cycle_configuration = ScheduledResearchConfiguration(
        universe_id="test-universe", max_candidates_per_cycle=3, experiment_policy="SHADOW_ENHANCED",
        submit_paper_orders=False, require_complete_evidence=False, require_point_in_time_safe=False,
        continue_on_symbol_failure=True, provider_mode="real", config_hash=hash_config({"x": 1}),
    )
    shadow_config = _shadow_config()
    result = run_due_shadow_cycle(
        now=DUE_NOW, conn=conn, shadow_config=shadow_config, cycle_configuration=cycle_configuration,
        candidate_symbols=lambda: ("AAPL",), run_cycle=_stub_run_cycle_success,
        cycle_kwargs_builder=lambda syms, as_of: {}, pricing_entries=(), clock=_clock_at(DUE_NOW),
        research_provider_name="anthropic", research_model_name="claude-test-model",
    )
    assert result.status == STATUS_BUDGET_REJECTED
    from trading_research.storage.shadow_alerts_repositories import list_alerts, list_run_summaries
    alerts = list_alerts(conn)
    assert len(alerts) == 1
    assert alerts[0]["alert_type"] == "BUDGET_EXCEEDED"
    assert len(list_run_summaries(conn)) == 1


def test_failed_cycle_raises_cycle_failed_alert_and_writes_health_summary(conn):
    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn, run_cycle=_stub_run_cycle_raises))
    assert result.status == "FAILED"
    from trading_research.storage.shadow_alerts_repositories import list_alerts, list_run_summaries
    alerts = list_alerts(conn)
    assert len(alerts) == 1
    assert alerts[0]["alert_type"] == "CYCLE_FAILED"
    assert alerts[0]["severity"] == "ERROR"
    summaries = list_run_summaries(conn)
    assert len(summaries) == 1
    assert summaries[0]["health_status"] in ("HEALTHY", "DEGRADED", "PAUSE_RECOMMENDED", "PAUSE_REQUIRED")
    assert summaries[0]["policy_version"] == "health/v2"


def test_completed_cycle_writes_health_summary_with_real_provider_success_rate(conn):
    calls = []

    def _tracking_stub(**kwargs):
        calls.append(kwargs)
        return _stub_run_cycle_success(**kwargs)

    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn, run_cycle=_tracking_stub))
    assert result.status == STATUS_COMPLETED
    from trading_research.storage.shadow_alerts_repositories import list_alerts, list_run_summaries
    summaries = list_run_summaries(conn)
    assert len(summaries) == 1
    assert summaries[0]["provider_success_rate"] == 1.0
    assert summaries[0]["health_status"] == "HEALTHY"
    # No alert on a fully-healthy COMPLETED cycle.
    assert list_alerts(conn) == []


def test_retry_exhaustion_rate_denominator_reflects_roles_invoked_not_symbol_count(conn):
    """docs/milestone-7.2.md Part 6-9: the real-validated bug. With 3
    analyst roles configured and only ONE (technical) exhausting its
    attempt budget while the other two succeed, the OLD denominator
    (`len(research_run_ids)` == 1 symbol) produced `retry_exhaustion_rate ==
    1.0` (100%) — a single role's failure misreported as if every attempted
    role had failed. The FIXED denominator (`distinct_roles_invoked_count`
    == 3) correctly reports `1/3`, staying under the 0.50 pause threshold."""
    from trading_research.research.deterministic_provider import ScriptedResearchProvider, ScriptedStep
    from trading_research.research.fixtures import build_fixture_snapshot
    from trading_research.research.orchestration import analyze_with_research_committee
    from trading_research.research.prompt_registry import PromptRegistry
    from trading_research.research.scheduled_cycle import ResearchCycleResult
    from trading_research.shadow.scheduler import _build_health_inputs_from_cycle_result
    from trading_research.storage.research_repositories import SQLiteResearchRepository, save_evidence_snapshot
    from tests.unit.test_attempt_control_hooks import ANALYST_PAYLOAD, _config

    as_of = datetime(2026, 7, 13, tzinfo=timezone.utc)
    snapshot = build_fixture_snapshot("AAPL", as_of, config_hash="d" * 64, git_sha="sha1", clock=lambda: as_of)
    save_evidence_snapshot(conn, snapshot)

    provider = ScriptedResearchProvider({
        ("fundamental", 1): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD),
        ("technical", 1): ScriptedStep(kind="malformed", raw_text="bad"),
        ("bull", 1): ScriptedStep(kind="response", payload=ANALYST_PAYLOAD),
    })
    repo = SQLiteResearchRepository(conn)
    orchestration_result = analyze_with_research_committee(
        snapshot, provider=provider, provider_name="scripted", model_name="test-model", prompt_registry=PromptRegistry(),
        research_repository=repo, configuration=_config(roles=("fundamental", "technical", "bull", "manager"), max_attempts_per_role=1),
        clock=lambda: as_of, run_mode="scripted",
    )

    cycle_result = ResearchCycleResult(
        cycle_id="cycle-test", universe_id="test-universe", as_of=as_of, status="COMPLETED",
        symbol_results=(
            SymbolCycleResult(
                symbol="AAPL", status="COMPLETED", evidence_outcome="COMPLETE",
                research_run_id=orchestration_result.research_run_id,
            ),
        ),
        reused_existing_cycle=False,
    )
    inputs = _build_health_inputs_from_cycle_result(
        conn, cycle_result, symbols_attempted=1, cycle_duration_seconds=1.0,
    )
    assert inputs.retry_exhaustion_rate == pytest.approx(1 / 3)
    assert inputs.retry_exhaustion_rate < 0.50  # correctly under the pause threshold


def test_emergency_margin_breach_is_reflected_in_budget_breached_health_input(conn):
    """docs/milestone-7.2.md Part 9 fix: `budget_breached` was previously
    ALWAYS the CycleHealthInputs default (False) — `check_emergency_margin_breach`
    existed and was fully unit-tested but was never called from the
    scheduler. Pre-inflate the reservation this cycle will idempotently
    reuse (same `shadow-budget:{intended_schedule_id}` key) so its consumed
    cost already exceeds the configured emergency margin before
    `run_due_shadow_cycle` even starts the cycle."""
    from trading_research.shadow import budget as budget_mod
    from trading_research.shadow import schedule as schedule_mod

    shadow_config = _shadow_config(safety={"pause_on_budget_breach": True})
    intended_time = schedule_mod.intended_schedule_time_for_day(DUE_NOW.date(), shadow_config)
    intended_id = schedule_mod.intended_schedule_id(intended_time)
    idempotency_key = f"shadow-budget:{intended_id}"

    intent = budget_mod.CycleIntent(
        provider="deterministic", model_name=None, max_symbols_per_cycle=1, max_roles_per_symbol=1,
        max_attempts_per_role=1, max_output_tokens_per_cycle=100, max_input_tokens_per_cycle=100,
        max_latency_seconds_per_cycle=60,
    )
    estimate = budget_mod.estimate_cycle_cost(intent, (), DUE_NOW.date().isoformat())
    reservation = budget_mod.reserve_budget(
        conn, idempotency_key, intent, estimate, max_actual_cost_per_day_usd=Decimal("10"),
        max_actual_cost_per_month_usd=Decimal("100"), clock=_clock_at(DUE_NOW),
    )
    # Reserved cost is $0 (non-anthropic provider) — any positive consumed
    # cost at all exceeds `reserved * (1 + margin)` for a zero reservation.
    budget_mod.record_actual_usage(
        conn, reservation.reservation_id, actual_cost_usd=Decimal("50.00"), actual_input_tokens=0,
        actual_output_tokens=0, actual_latency_seconds=0, provider="deterministic", model_name=None,
        clock=_clock_at(DUE_NOW),
    )

    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn, shadow_config=shadow_config))
    assert result.status == STATUS_COMPLETED

    from trading_research.storage.shadow_alerts_repositories import list_run_summaries
    summaries = list_run_summaries(conn)
    assert summaries[0]["health_status"] == "PAUSE_REQUIRED"
    assert "budget_breached is True" in summaries[0]["health_reasons_json"]
    assert pause_mod.current_state(conn).state == pause_mod.STATE_PAUSED_BUDGET

    from trading_research.storage.shadow_alerts_repositories import list_alerts

    alerts = [a for a in list_alerts(conn) if a["alert_type"] == "PAUSE_ACTIVATED"]
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "CRITICAL"
    assert "budget_breached is True" in alerts[0]["message"]


def test_budget_breach_with_flag_disabled_recommends_pause_and_alerts_but_does_not_pause(conn):
    """docs/milestone-7.2.md Part 11: `PAUSE_RECOMMENDED -> alert only`."""
    from trading_research.shadow import budget as budget_mod
    from trading_research.shadow import schedule as schedule_mod

    shadow_config = _shadow_config(safety={"pause_on_budget_breach": False})
    intended_time = schedule_mod.intended_schedule_time_for_day(DUE_NOW.date(), shadow_config)
    intended_id = schedule_mod.intended_schedule_id(intended_time)
    idempotency_key = f"shadow-budget:{intended_id}"

    intent = budget_mod.CycleIntent(
        provider="deterministic", model_name=None, max_symbols_per_cycle=1, max_roles_per_symbol=1,
        max_attempts_per_role=1, max_output_tokens_per_cycle=100, max_input_tokens_per_cycle=100,
        max_latency_seconds_per_cycle=60,
    )
    estimate = budget_mod.estimate_cycle_cost(intent, (), DUE_NOW.date().isoformat())
    reservation = budget_mod.reserve_budget(
        conn, idempotency_key, intent, estimate, max_actual_cost_per_day_usd=Decimal("10"),
        max_actual_cost_per_month_usd=Decimal("100"), clock=_clock_at(DUE_NOW),
    )
    budget_mod.record_actual_usage(
        conn, reservation.reservation_id, actual_cost_usd=Decimal("50.00"), actual_input_tokens=0,
        actual_output_tokens=0, actual_latency_seconds=0, provider="deterministic", model_name=None,
        clock=_clock_at(DUE_NOW),
    )

    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn, shadow_config=shadow_config))
    assert result.status == STATUS_COMPLETED  # an expected health verdict is never a scheduler crash

    from trading_research.storage.shadow_alerts_repositories import list_alerts, list_run_summaries

    summaries = list_run_summaries(conn)
    assert summaries[0]["health_status"] == "PAUSE_RECOMMENDED"
    # No automatic pause — the flag was disabled.
    assert pause_mod.current_state(conn).state == pause_mod.STATE_ACTIVE

    alerts = [a for a in list_alerts(conn) if a["alert_type"] == "PAUSE_ACTIVATED"]
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "WARNING"
    assert "no automatic action taken" in alerts[0]["message"]


def test_healthy_cycle_raises_no_pause_alert(conn):
    """docs/milestone-7.2.md Part 11: `HEALTHY -> no pause` and no alert."""
    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn))
    assert result.status == STATUS_COMPLETED
    from trading_research.storage.shadow_alerts_repositories import list_alerts, list_run_summaries

    assert list_run_summaries(conn)[0]["health_status"] == "HEALTHY"
    assert list_alerts(conn) == []
    assert pause_mod.current_state(conn).state == pause_mod.STATE_ACTIVE


def test_degraded_cycle_raises_no_pause_alert_and_no_pause(conn):
    """docs/milestone-7.2.md Part 11: `DEGRADED -> no automatic pause` (and
    no alert either — DEGRADED is this module's own "approaching the line"
    interpretation, not a configured policy breach)."""
    from trading_research.research.scheduled_cycle import ResearchCycleResult

    def _stub_one_of_two_symbols_fails(*, as_of, symbols, configuration, conn, clock, **_kwargs):
        results = tuple(
            SymbolCycleResult(symbol=s, status="COMPLETED" if i == 0 else "FAILED", evidence_outcome="COMPLETE")
            for i, s in enumerate(symbols)
        )
        return ResearchCycleResult(
            cycle_id=f"cycle-{as_of.isoformat()}", universe_id=configuration.universe_id, as_of=as_of,
            status="PARTIALLY_COMPLETE", symbol_results=results, reused_existing_cycle=False,
        )

    result = run_due_shadow_cycle(
        now=DUE_NOW, clock=_clock_at(DUE_NOW),
        **_base_kwargs(conn, run_cycle=_stub_one_of_two_symbols_fails, symbols=("AAPL", "MSFT")),
    )
    assert result.status == "PARTIALLY_COMPLETE"

    from trading_research.storage.shadow_alerts_repositories import list_run_summaries

    summary = [s for s in list_run_summaries(conn) if s["scheduler_run_id"] == result.scheduler_run_id][0]
    assert summary["provider_success_rate"] == 0.5  # 1 of 2 symbols completed -> failure_rate 0.5
    assert summary["health_status"] == "DEGRADED"  # 0.5 > degraded threshold 0.3, not > pause threshold 0.5
    assert pause_mod.current_state(conn).state == pause_mod.STATE_ACTIVE

    from trading_research.storage.shadow_alerts_repositories import list_alerts

    assert [a for a in list_alerts(conn) if a["alert_type"] == "PAUSE_ACTIVATED"] == []


def test_pause_alert_context_excludes_scheduler_run_id_so_dedup_actually_works(conn):
    """docs/milestone-7.2.md Part 11: duplicate pause alerts deduplicate.
    The scheduler's health-triggered alert context deliberately excludes
    `scheduler_run_id` (always unique per invocation) — otherwise two
    scheduler runs producing the IDENTICAL underlying health condition
    (same `health_status`/`triggering_flags`/`health_reasons`) would never
    share a `dedup_key` and `raise_alert`'s suppression could never fire.
    Verified directly against `shadow/alerts.py::raise_alert` using the EXACT
    context shape `shadow/scheduler.py` builds for this alert (Part 11's own
    "duplicate pause alerts deduplicate" requirement), rather than fighting
    the scheduler's own daily-cadence idempotency to force two due cycles
    within one 15-minute dedup window."""
    from trading_research.shadow import alerts as alerts_mod

    context = {
        "pause_state": pause_mod.STATE_PAUSED_BUDGET, "health_status": "PAUSE_REQUIRED",
        "triggering_flags": ["budget_breach"], "health_reasons": ["budget_breached is True"],
    }
    alert1 = alerts_mod.OperationalAlert(
        severity=alerts_mod.SEVERITY_CRITICAL, alert_type=alerts_mod.ALERT_TYPE_PAUSE_ACTIVATED,
        message="shadow operations automatically paused (PAUSED_BUDGET) after scheduler run shadow-run-AAA: budget_breached is True",
        context=context, created_at=DUE_NOW,
    )
    alert2 = alerts_mod.OperationalAlert(
        severity=alerts_mod.SEVERITY_CRITICAL, alert_type=alerts_mod.ALERT_TYPE_PAUSE_ACTIVATED,
        message="shadow operations automatically paused (PAUSED_BUDGET) after scheduler run shadow-run-BBB: budget_breached is True",
        context=context, created_at=DUE_NOW,
    )
    assert alert1.dedup_key == alert2.dedup_key  # identical despite different scheduler_run_id in the message only

    sinks = (alerts_mod.PersistenceOnlyAlertSink(),)
    alerts_mod.raise_alert(conn, alert1, sinks, _clock_at(DUE_NOW))
    alerts_mod.raise_alert(conn, alert2, sinks, _clock_at(DUE_NOW))

    from trading_research.storage.shadow_alerts_repositories import list_alerts

    pause_alerts = [a for a in list_alerts(conn) if a["alert_type"] == "PAUSE_ACTIVATED"]
    assert len(pause_alerts) == 1  # the second, identical alert was suppressed, not duplicated
    assert pause_alerts[0]["suppressed_count"] == 1  # but the suppression itself is recorded


def test_completed_cycle_persists_one_health_check_per_dimension(conn):
    from trading_research.shadow.health import CHECK_NAMES_IN_ORDER
    from trading_research.storage.shadow_alerts_repositories import list_health_checks

    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn))
    checks = list_health_checks(conn, scheduler_run_id=result.scheduler_run_id)
    assert {c["check_name"] for c in checks} == set(CHECK_NAMES_IN_ORDER)
    assert len(checks) == len(CHECK_NAMES_IN_ORDER)
    for c in checks:
        assert c["policy_version"] == "health/v2"
        assert c["scheduler_run_id"] == result.scheduler_run_id


def test_health_checks_queryable_by_cycle_id(conn):
    from trading_research.storage.shadow_alerts_repositories import list_health_checks

    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn))
    checks = list_health_checks(conn, cycle_id=result.cycle_id)
    assert len(checks) > 0
    assert all(c["cycle_id"] == result.cycle_id for c in checks)


def test_health_checks_queryable_by_check_name(conn):
    from trading_research.storage.shadow_alerts_repositories import list_health_checks

    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn))
    checks = list_health_checks(conn, check_name="provider_failure_rate")
    assert len(checks) == 1
    assert checks[0]["scheduler_run_id"] == result.scheduler_run_id


def test_health_checks_not_duplicated_on_reevaluation(conn):
    from trading_research.shadow import health as health_mod
    from trading_research.storage.shadow_alerts_repositories import list_health_checks

    result = run_due_shadow_cycle(now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn))
    before = list_health_checks(conn, scheduler_run_id=result.scheduler_run_id)

    # Simulate a resumed/re-evaluated invocation persisting the identical
    # checks a second time for the same scheduler_run_id — must be a no-op.
    from trading_research.shadow.scheduler import _save_health_checks

    inputs = health_mod.CycleHealthInputs(
        provider_success_rate=1.0, evidence_completeness_rate=1.0, claude_role_success_rate=None, retry_rate=None,
        retry_exhaustion_rate=None, unsupported_claim_rate=None, output_truncation_rate=None, latency_seconds=None,
        input_tokens=None, output_tokens=None, cost_usd=None, pricing_configured=True,
        paper_reconciliation_mismatch=False, duplicate_prevention_violation=False, cycle_duration_seconds=1.0,
    )
    config = health_mod.HealthPolicyConfig.from_shadow_config(_shadow_config())
    health_result = health_mod.evaluate_cycle_health(inputs, config)
    _save_health_checks(
        conn, scheduler_run_id=result.scheduler_run_id, cycle_id=result.cycle_id, health_result=health_result,
        clock=_clock_at(DUE_NOW),
    )
    after = list_health_checks(conn, scheduler_run_id=result.scheduler_run_id)
    assert len(after) == len(before)


def test_partially_complete_cycle_raises_partial_alert(conn):
    from trading_research.research.scheduled_cycle import ResearchCycleResult

    def _stub_partial(*, as_of, symbols, configuration, conn, clock, **_kwargs):
        results = tuple(
            SymbolCycleResult(symbol=s, status="COMPLETED" if i == 0 else "FAILED")
            for i, s in enumerate(symbols)
        )
        return ResearchCycleResult(
            cycle_id=f"cycle-{as_of.isoformat()}", universe_id=configuration.universe_id, as_of=as_of,
            status="PARTIALLY_COMPLETE", symbol_results=results, reused_existing_cycle=False,
        )

    result = run_due_shadow_cycle(
        now=DUE_NOW, clock=_clock_at(DUE_NOW), **_base_kwargs(conn, run_cycle=_stub_partial, symbols=("AAPL", "MSFT")),
    )
    assert result.status == "PARTIALLY_COMPLETE"
    from trading_research.storage.shadow_alerts_repositories import list_alerts, list_run_summaries
    alerts = list_alerts(conn)
    assert len(alerts) == 1
    assert alerts[0]["alert_type"] == "CYCLE_PARTIALLY_COMPLETE"
    summaries = list_run_summaries(conn)
    assert summaries[0]["provider_success_rate"] == 0.5
