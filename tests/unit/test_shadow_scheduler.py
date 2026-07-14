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
    assert summaries[0]["policy_version"] == "health/v1"


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
