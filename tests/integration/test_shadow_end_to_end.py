"""Composed, offline, deterministic end-to-end shadow-operations tests
(docs/milestone-7.md Step 27 section M). Drives the real orchestrator,
`shadow/scheduler.py::run_due_shadow_cycle`, never a reimplementation of it.

All four scenarios use a real on-disk sqlite temp file (via
`storage/database.py::connect()`), not `:memory:` — this matches
`tests/unit/test_shadow_lease.py`'s fixture pattern, since the lease
mechanism's `BEGIN IMMEDIATE` file-locking behavior is only meaningfully
exercised against a real file.

No network anywhere in this file: the deterministic/scripted research
provider and fixture evidence clients (mirroring
`cli.py::run_due_shadow_cycle_cli`'s own fixture-mode wiring, and
`tests/unit/test_shadow_scheduler.py::test_end_to_end_real_scheduled_cycle_with_fixture_providers`)
stand in for Claude and SEC/market-data respectively.
"""
from __future__ import annotations

import socket
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from trading_research.analysis.scorer import load_scoring_config
from trading_research.analysis.screener import load_screening_config
from trading_research.evidence_providers.evidence_adapters import (
    RealFilingEvidenceProvider,
    RealFundamentalsEvidenceProvider,
    RealMarketEvidenceProvider,
)
from trading_research.evidence_providers.fixture_clients import FixtureMarketDataClient, FixtureSecClient
from trading_research.hashing import hash_config
from trading_research.models.trading_models import PortfolioState
from trading_research.research import experiment_policy
from trading_research.research.configuration import load_research_config
from trading_research.research.deterministic_provider import DeterministicResearchProvider
from trading_research.research.prompt_registry import PromptRegistry
from trading_research.research.scheduled_cycle import (
    EvidenceProviderRegistry,
    PROVIDER_MODE_FIXTURE,
    ScheduledResearchConfiguration,
    run_scheduled_research_cycle,
)
from trading_research.shadow import alerts as alerts_mod
from trading_research.shadow import lease as lease_mod
from trading_research.shadow import schedule as schedule_mod
from trading_research.shadow.config import load_shadow_operations_config
from trading_research.shadow.scheduler import (
    STATUS_COMPLETED,
    STATUS_LEASE_HELD,
    run_due_shadow_cycle,
)
from trading_research.storage.database import connect
from trading_research.storage.research_cycle_repositories import SQLiteResearchCycleRepository
from trading_research.storage.research_repositories import SQLiteResearchRepository
from trading_research.storage.shadow_alerts_repositories import list_alerts, list_run_summaries
from trading_research.storage.shadow_operations_repositories import list_budget_reservations, list_leases
from trading_research.universe.tickers import default_universe


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("shadow end-to-end tests must never open a real socket")

    monkeypatch.setattr(socket.socket, "connect", _blocked)


LA = ZoneInfo("America/Los_Angeles")
DUE_NOW = datetime(2026, 7, 13, 7, 0, tzinfo=LA)  # Monday, within the default 06:30-08:30 run window

RAW_SHADOW_CONFIG = {
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
        submit_paper_orders=False, require_complete_evidence=False, require_point_in_time_safe=False,
        continue_on_symbol_failure=True, provider_mode=PROVIDER_MODE_FIXTURE, config_hash=hash_config({"x": 1}),
    )
    base.update(overrides)
    return ScheduledResearchConfiguration(**base)


def _clock_at(t):
    return lambda: t


@pytest.fixture
def db_path():
    """Real on-disk sqlite temp file — required for the lease mechanism's
    file-level locking to be genuinely exercised (matches
    tests/unit/test_shadow_lease.py)."""
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "shadow_e2e.db"


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


class _CountingPaperSubmitter:
    """Records every call — used to prove the enhanced arm never submits."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, recommendation_id: str):
        self.calls.append(recommendation_id)
        return None


def _fixture_registry() -> EvidenceProviderRegistry:
    sec = FixtureSecClient()
    market = FixtureMarketDataClient()
    return EvidenceProviderRegistry(
        fundamentals=RealFundamentalsEvidenceProvider(sec), market=RealMarketEvidenceProvider(market),
        filings=RealFilingEvidenceProvider(sec), news=None, sentiment=None, portfolio_context=None,
        market_data_client=market, sec_client=sec,
    )


def _real_cycle_kwargs_builder(conn, *, paper_submitter=None):
    def _builder(symbols, as_of):
        return dict(
            cycle_repository=SQLiteResearchCycleRepository(conn), universe=default_universe(),
            screening_config=load_screening_config(), scoring_config=load_scoring_config(),
            evidence_providers=_fixture_registry(), research_provider=DeterministicResearchProvider(),
            research_provider_name="deterministic", research_model_name="deterministic-v1",
            research_configuration=load_research_config(), research_repository=SQLiteResearchRepository(conn),
            prompt_registry=PromptRegistry(),
            portfolio=PortfolioState(account_equity=Decimal("100000"), settled_cash=Decimal("100000"), as_of=as_of),
            paper_submitter=paper_submitter, git_sha="test-sha",
        )

    return _builder


# =============================================================================
# 1. Full happy path
# =============================================================================


def test_full_happy_path_enhanced_shadow_no_execution(conn):
    """due scheduled run -> lease acquired -> budget reserved -> fixture
    corporate-status-shaped evidence (via FixtureSecClient) -> fixture
    market data -> deterministic baseline -> scripted/deterministic Claude
    committee -> enhanced shadow recommendation -> NO enhanced-arm paper
    submission -> evaluation records exist -> health computed -> budget
    settled -> lease released."""
    submitter = _CountingPaperSubmitter()
    shadow_config = _shadow_config()
    cycle_configuration = _cycle_configuration(submit_paper_orders=True)  # even with submission allowed...

    result = run_due_shadow_cycle(
        now=DUE_NOW, conn=conn, shadow_config=shadow_config, cycle_configuration=cycle_configuration,
        candidate_symbols=lambda: ("AAPL",), run_cycle=run_scheduled_research_cycle,
        cycle_kwargs_builder=_real_cycle_kwargs_builder(conn, paper_submitter=submitter),
        pricing_entries=(), clock=_clock_at(DUE_NOW),
    )

    assert result.status == STATUS_COMPLETED
    assert result.symbols_attempted == 1
    assert result.symbols_completed == 1
    assert result.cycle_id is not None

    # --- No enhanced-arm paper submission: may_submit_enhanced() is always
    # False for every supported experiment policy, and no call site for it
    # exists in _run_symbol at all. The baseline arm's side is watch/no_action
    # for the fixture AAPL data (not BUY), so the counting submitter should
    # see zero calls in this scenario too; either way, every call recorded is
    # provably for the BASELINE recommendation id, never the ENHANCED one.
    cycle_result_row = conn.execute(
        "SELECT baseline_recommendation_id, enhanced_recommendation_id FROM research_cycle_symbol_results "
        "WHERE cycle_id = ? AND symbol = 'AAPL'",
        (result.cycle_id,),
    ).fetchone()
    assert cycle_result_row is not None
    enhanced_rec_id = cycle_result_row["enhanced_recommendation_id"]
    assert enhanced_rec_id not in submitter.calls, "enhanced arm must never be paper-submitted"

    # --- Evaluation records exist: evidence snapshot + experiment assignments
    # (BASELINE + ENHANCED) were persisted for this cycle.
    snapshot_count = conn.execute("SELECT COUNT(*) AS c FROM research_evidence_snapshots").fetchone()["c"]
    assert snapshot_count == 1
    arm_rows = conn.execute(
        "SELECT arm FROM research_experiment_assignments WHERE experiment_id IN "
        "(SELECT experiment_id FROM research_experiment_assignments)"
    ).fetchall()
    assert {r["arm"] for r in arm_rows} == {"BASELINE", "ENHANCED"}

    # --- Health result computed and persisted.
    summaries = list_run_summaries(conn)
    assert len(summaries) == 1
    assert summaries[0]["health_status"] in ("HEALTHY", "DEGRADED", "PAUSE_RECOMMENDED", "PAUSE_REQUIRED")
    assert summaries[0]["provider_success_rate"] is None
    assert summaries[0]["provider_health_mode"] == "NOT_APPLICABLE"

    # --- Alert(s) persisted as appropriate: a fully healthy COMPLETED cycle
    # raises none — asserted as an explicit, checked fact, not an omission.
    assert list_alerts(conn) == []

    # --- Budget settled: reservation is SETTLED, not left RESERVED.
    reservations = list_budget_reservations(conn)
    assert len(reservations) == 1
    assert reservations[0]["status"] == "SETTLED"

    # --- Lease released: attempting to acquire it again immediately succeeds.
    intended_time = schedule_mod.intended_schedule_time_for_day(DUE_NOW.date(), shadow_config)
    intended_id = schedule_mod.intended_schedule_id(intended_time)
    lease_key = f"shadow-scheduler:{intended_id}"
    reacquire = lease_mod.acquire(conn, lease_key, "post-hoc-check-owner", 60, _clock_at(DUE_NOW + timedelta(seconds=1)))
    assert isinstance(reacquire, lease_mod.LeaseHandle), "lease must be free after a completed cycle"


# =============================================================================
# 2. Concurrent conflict
# =============================================================================


def test_concurrent_invocation_lease_held_no_provider_or_claude_call(conn):
    """Simulates two invocations racing for the same intended schedule slot:
    the lease is acquired directly first (simulating an in-progress first
    invocation), then run_due_shadow_cycle is called once — it must observe
    LEASE_HELD and never call the (call-counting) run_cycle stub, proving no
    provider/Claude call happened on this (the only) invocation that could
    have raced."""
    shadow_config = _shadow_config()
    intended_time = schedule_mod.intended_schedule_time_for_day(DUE_NOW.date(), shadow_config)
    intended_id = schedule_mod.intended_schedule_id(intended_time)
    lease_key = f"shadow-scheduler:{intended_id}"
    # First invocation: acquires the lease and (by hypothesis) is still mid-cycle.
    lease_mod.acquire(conn, lease_key, "invocation-one-owner", 3600, _clock_at(DUE_NOW))

    calls = []

    def _counting_run_cycle(**kwargs):
        calls.append(kwargs)
        raise AssertionError("run_cycle must never be called when the lease is held")

    second_result = run_due_shadow_cycle(
        now=DUE_NOW, conn=conn, shadow_config=shadow_config, cycle_configuration=_cycle_configuration(),
        candidate_symbols=lambda: ("AAPL",), run_cycle=_counting_run_cycle,
        cycle_kwargs_builder=lambda syms, as_of: {}, pricing_entries=(), clock=_clock_at(DUE_NOW),
    )

    assert second_result.status == STATUS_LEASE_HELD
    assert second_result.is_successful_no_op
    assert calls == [], "no provider/Claude call may happen on a lease-conflicted invocation"

    # No duplicate cycle/research_run_id/recommendation created: nothing in
    # any of these tables, since the second invocation never reached the
    # cycle-execution step at all.
    assert conn.execute("SELECT COUNT(*) AS c FROM research_evidence_snapshots").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM research_committee_runs").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM recommendations").fetchone()["c"] == 0

    # Lease still held by the first (simulated) invocation — not touched by
    # the second's no-op.
    leases = list_leases(conn)
    assert len(leases) == 1
    assert leases[0]["status"] == "HELD"
    assert leases[0]["owner"] == "invocation-one-owner"


def test_concurrent_invocation_via_two_run_due_shadow_cycle_calls_second_blocked(conn):
    """A second, stronger form of the same scenario: call
    run_due_shadow_cycle itself twice for the identical intended slot using a
    controlled clock, where the first call's injected run_cycle deliberately
    holds/re-acquires nothing extra (it just completes normally) — proving
    the *first* call's own lease acquisition is what blocks a genuinely
    concurrent second attempt is covered above (direct acquire simulates
    "still in progress"); this test instead proves the idempotency layer
    also prevents a second *sequential* call (lease already released) from
    re-running Claude for the same completed slot."""
    calls = []

    def _counting_run_cycle(*, as_of, symbols, configuration, conn, clock, **_kwargs):
        calls.append(symbols)
        return run_scheduled_research_cycle(
            as_of=as_of, symbols=symbols, configuration=configuration, conn=conn, clock=clock, **_kwargs,
        )

    shadow_config = _shadow_config()
    cycle_configuration = _cycle_configuration()
    kwargs = dict(
        shadow_config=shadow_config, cycle_configuration=cycle_configuration, candidate_symbols=lambda: ("AAPL",),
        run_cycle=_counting_run_cycle, cycle_kwargs_builder=_real_cycle_kwargs_builder(conn), pricing_entries=(),
    )

    first = run_due_shadow_cycle(now=DUE_NOW, conn=conn, clock=_clock_at(DUE_NOW), **kwargs)
    assert first.status == STATUS_COMPLETED
    assert len(calls) == 1

    second = run_due_shadow_cycle(now=DUE_NOW, conn=conn, clock=_clock_at(DUE_NOW), **kwargs)
    assert second.status == "ALREADY_COMPLETED"
    assert len(calls) == 1, "no duplicate Claude/provider call for an already-completed intended slot"
    assert second.cycle_id is None  # no-op path never looks up/returns a cycle id

    # No duplicate research_run / recommendation rows.
    run_count = conn.execute("SELECT COUNT(*) AS c FROM research_committee_runs").fetchone()["c"]
    assert run_count == 1
    rec_count = conn.execute("SELECT COUNT(*) AS c FROM recommendations").fetchone()["c"]
    assert rec_count == 2  # baseline + enhanced, from the one real cycle only


# =============================================================================
# 3. Budget exhausted mid-batch
# =============================================================================


def test_budget_exhausted_mid_batch_first_symbol_retained_rest_skipped(conn):
    """Configures a run_cycle stub that processes symbols one at a time
    against the *real* budget reservation `run_due_shadow_cycle` already
    made (via `shadow/budget.py::remaining_reservation_budget`) — after the
    first symbol's (simulated) usage is recorded, the reservation's
    remaining output-token budget is intentionally driven to zero, so the
    stub must skip every subsequent symbol without ever invoking the
    (call-counting) research provider for them. This composes the real
    `shadow/budget.py` primitives rather than reimplementing
    `research/scheduled_cycle.py::_run_symbol` (which does not itself call
    `role_budget.py`/`budget.py` per-symbol yet — a documented, intentional
    scope boundary of `shadow/scheduler.py`, see that module's own
    docstring). A budget-related alert is raised explicitly by this stub via
    the same `shadow/alerts.py::raise_alert` primitive `scheduler.py` itself
    uses, proving the alert-persistence path works for a per-symbol budget
    cutoff, not just a whole-cycle rejection."""
    from trading_research.research.deterministic_provider import DeterministicResearchProvider
    from trading_research.research.fixtures import build_fixture_snapshot
    from trading_research.research.orchestration import analyze_with_research_committee
    from trading_research.research.scheduled_cycle import ResearchCycleResult, SymbolCycleResult
    from trading_research.shadow import budget as budget_mod

    claude_call_log: list[str] = []

    class _CountingResearchProvider:
        """Wraps DeterministicResearchProvider (no network) and records every
        role invoked — this is the call-counting proof that Claude is (or
        is not) actually called, not just an inference from a status
        string."""

        def __init__(self):
            self._inner = DeterministicResearchProvider()

        def generate_structured(self, request):
            claude_call_log.append(request.role)
            return self._inner.generate_structured(request)

    research_config = load_research_config()

    def _budget_aware_stub(*, as_of, symbols, configuration, conn, clock, reservation_id, **_kwargs):
        results = []
        for i, symbol in enumerate(symbols):
            remaining = budget_mod.remaining_reservation_budget(conn, reservation_id)
            if remaining["remaining_output_tokens"] <= 0:
                results.append(SymbolCycleResult(symbol=symbol, status="SKIPPED", failure_reason="budget exhausted mid-batch"))
                continue
            # Simulate processing symbol i: run the real committee
            # orchestrator (the default config/research.yaml role set,
            # including manager) through a call-counting provider wrapper,
            # then record consumption against the real reservation to
            # genuinely drain the budget after symbol one.
            snapshot = build_fixture_snapshot(symbol, as_of, config_hash=research_config.config_hash, git_sha="budget-test", clock=clock)
            from trading_research.storage.research_repositories import save_evidence_snapshot

            save_evidence_snapshot(conn, snapshot)
            analyze_with_research_committee(
                snapshot, provider=_CountingResearchProvider(), provider_name="deterministic",
                model_name="deterministic-v1", prompt_registry=PromptRegistry(),
                research_repository=SQLiteResearchRepository(conn), configuration=research_config, clock=clock,
                run_mode="deterministic", require_decision=False,
            )
            # Consume the entire cycle's reserved output-token budget after
            # this one (real, more-expensive-than-projected) symbol —
            # genuinely draining shadow_budget_reservations.consumed_output_tokens
            # to match reserved_output_tokens, so the next iteration's
            # remaining_reservation_budget() check correctly reports zero.
            reserved = budget_mod.remaining_reservation_budget(conn, reservation_id)
            budget_mod.record_actual_usage(
                conn, reservation_id, actual_cost_usd=Decimal("0"), actual_input_tokens=1000,
                actual_output_tokens=reserved["remaining_output_tokens"], actual_latency_seconds=1,
                provider="deterministic", model_name="deterministic-v1", clock=clock,
            )
            results.append(SymbolCycleResult(symbol=symbol, status="COMPLETED"))

        # Budget-related alert, persisted via the same primitive scheduler.py uses.
        if any(r.status == "SKIPPED" for r in results):
            alerts_mod.raise_alert(
                conn,
                alerts_mod.OperationalAlert(
                    severity=alerts_mod.SEVERITY_ERROR, alert_type=alerts_mod.ALERT_TYPE_BUDGET_EXCEEDED,
                    message="budget exhausted mid-batch: remaining symbols skipped",
                    context={"cycle_symbols": list(symbols), "reservation_id": reservation_id}, created_at=clock(),
                ),
                (alerts_mod.PersistenceOnlyAlertSink(),), clock,
            )

        status = "PARTIALLY_COMPLETE" if any(r.status == "SKIPPED" for r in results) else "COMPLETED"
        return ResearchCycleResult(
            cycle_id=f"cycle-{as_of.isoformat()}", universe_id=configuration.universe_id, as_of=as_of,
            status=status, symbol_results=tuple(results), reused_existing_cycle=False,
        )

    def _cycle_kwargs_builder(symbols, as_of):
        # reservation_id is threaded through by capturing it from the
        # scheduler's own reservation, looked up by idempotency key after
        # the fact is not possible before the call — instead this builder
        # reads it back out of shadow_budget_reservations by idempotency
        # key convention `shadow-budget:{intended_schedule_id}`, which the
        # scheduler always uses (see scheduler.py Step 5).
        intended_time = schedule_mod.intended_schedule_time_for_day(DUE_NOW.date(), shadow_config)
        intended_id = schedule_mod.intended_schedule_id(intended_time)
        from trading_research.storage.shadow_operations_repositories import load_budget_reservation_by_idempotency_key

        reservation = load_budget_reservation_by_idempotency_key(conn, f"shadow-budget:{intended_id}")
        assert reservation is not None, "budget must already be reserved before the cycle runs"
        return {"reservation_id": reservation["reservation_id"]}

    # max_calls = max_symbols_per_cycle * max_roles_per_symbol * max_attempts_per_role = 3 * 1 * 1 = 3
    # reserved output tokens = max_calls * max_output_tokens_per_cycle = 3 * 50000 = 150000 worst-case —
    # deliberately bounds the *whole cycle's* reservation to exactly what one symbol's simulated usage
    # (below) will fully consume, so after symbol one the reservation's remaining output-token budget
    # is driven to zero and the stub's own remaining-budget check (via
    # shadow/budget.py::remaining_reservation_budget) correctly skips every subsequent symbol.
    shadow_config = _shadow_config(
        budgets={"max_symbols_per_cycle": 3, "max_roles_per_symbol": 1, "max_attempts_per_role": 1, "max_output_tokens_per_cycle": 50000},
    )
    result = run_due_shadow_cycle(
        now=DUE_NOW, conn=conn, shadow_config=shadow_config, cycle_configuration=_cycle_configuration(),
        candidate_symbols=lambda: ("AAPL", "MSFT", "SHEL"), run_cycle=_budget_aware_stub,
        cycle_kwargs_builder=_cycle_kwargs_builder, pricing_entries=(), clock=_clock_at(DUE_NOW),
    )

    assert result.status == "PARTIALLY_COMPLETE"
    assert result.symbols_attempted == 3
    assert result.symbols_completed == 1  # only AAPL retained
    assert result.symbols_skipped == 2  # MSFT + SHEL skipped, not attempted-and-failed

    # Claude/provider was called only for the first (retained) symbol's
    # configured roles (fundamental, technical, bull, bear, manager — the
    # default config/research.yaml role set; require_decision=False only
    # suppresses the manager call when manager is *not* configured, which
    # is not the case here). No additional call happened for the skipped
    # symbols — proven by call-count, not just status text.
    assert claude_call_log == ["fundamental", "technical", "bull", "bear", "manager"]

    # A budget-related alert was persisted (the explicit BUDGET_EXCEEDED
    # alert this stub raised, plus scheduler.py's own PARTIALLY_COMPLETE alert).
    alerts = list_alerts(conn)
    alert_types = {a["alert_type"] for a in alerts}
    assert "BUDGET_EXCEEDED" in alert_types
    assert "CYCLE_PARTIALLY_COMPLETE" in alert_types


# =============================================================================
# 4. Critical corporate-status unknown
# =============================================================================


class _UnknownCorporateStatusSecClient:
    """A fixture SEC client whose `list_filings` raises, forcing
    `derive_corporate_status` to resolve to `SOURCE_UNAVAILABLE` — one of
    the explicit `has_any_critical_uncertainty()`-triggering states (the
    other being an empty filing history, which resolves to `UNKNOWN`)."""

    def get_company_facts(self, symbol, *, as_of):
        return ()

    def list_filings(self, symbol, *, available_by, cik=None):
        from trading_research.evidence_providers.errors import ProviderError

        raise ProviderError("simulated SEC submissions-endpoint outage")


def test_critical_corporate_status_unknown_claude_skipped_explicit_reason(conn):
    """Feeds a fixture where corporate-status evidence resolves to
    SOURCE_UNAVAILABLE for the one symbol -> deterministic
    evidence-completeness policy (research/evidence_completeness.py) comes
    back with a critical blocker -> Claude must never be called for this
    symbol -> no recommendation execution -> the result carries an explicit
    evidence-completeness reason, never a silent skip.

    UPDATE (docs/milestone-7.1.md, Milestone 7.1): `research/scheduled_cycle.py::
    _run_symbol` NOW calls `derive_corporate_status`/`evaluate_completeness`
    directly as part of the real symbol loop — the "documented scope
    boundary" this test's docstring used to describe was closed in
    Milestone 7.1. This test is kept as a hand-rolled `run_cycle` stub
    composing the same primitives directly (still a legitimate, faster unit
    of coverage for the composition itself); the FUSED, real
    `run_scheduled_research_cycle` path is covered end-to-end by
    `tests/integration/test_milestone_7_1_shadow_integration.py::
    test_blocking_completeness_no_claude_call` instead, which drives the
    actual production code path with no stub in between."""
    from trading_research.evidence_providers.corporate_status_adapters import derive_corporate_status
    from trading_research.research.deterministic_provider import DeterministicResearchProvider
    from trading_research.research.evidence_completeness import evaluate_completeness
    from trading_research.research.fixtures import build_fixture_snapshot
    from trading_research.research.orchestration import analyze_with_research_committee
    from trading_research.research.scheduled_cycle import ResearchCycleResult, SymbolCycleResult

    claude_call_log: list[str] = []

    class _CountingResearchProvider:
        """Wraps DeterministicResearchProvider (no network) — recording every
        role invoked is the call-counting proof that Claude is skipped, not
        just an inference from a status string."""

        def __init__(self):
            self._inner = DeterministicResearchProvider()

        def generate_structured(self, request):
            claude_call_log.append(request.role)
            return self._inner.generate_structured(request)

    research_config = load_research_config()
    captured_symbol_results: list = []

    def _corporate_status_gated_stub(*, as_of, symbols, configuration, conn, clock, **_kwargs):
        results = []
        for symbol in symbols:
            corp_status = derive_corporate_status(symbol, sec_client=_UnknownCorporateStatusSecClient(), as_of=as_of)
            assert corp_status.reporting_status == "SOURCE_UNAVAILABLE"
            assert corp_status.has_any_critical_uncertainty() is True

            completeness = evaluate_completeness(
                symbol=symbol, snapshot_outcome="COMPLETE", corporate_status=corp_status,
                news_present=False, sentiment_present=False,
            )
            assert completeness.screening_blocked is True
            assert "MISSING_CRITICAL_CORPORATE_STATUS" in completeness.blocking_categories

            if completeness.screening_blocked:
                # Claude is skipped entirely — no provider call for this symbol.
                results.append(
                    SymbolCycleResult(
                        symbol=symbol, status="SKIPPED",
                        evidence_outcome=completeness.screening_completeness,
                        failure_reason=(
                            f"evidence-completeness policy={completeness.policy_version} blocked screening: "
                            f"{completeness.blocking_categories}"
                        ),
                    )
                )
                continue
            # Unreachable in this scenario (screening_blocked is always True
            # here), but exercised by test_full_happy_path_enhanced_shadow_no_execution
            # and the budget-exhaustion scenario above, proving this is a
            # faithful gated-cycle shape, not a stub that only ever skips.
            snapshot = build_fixture_snapshot(symbol, as_of, config_hash=research_config.config_hash, git_sha="cs-test", clock=clock)
            analyze_with_research_committee(
                snapshot, provider=_CountingResearchProvider(), provider_name="deterministic",
                model_name="deterministic-v1", prompt_registry=PromptRegistry(),
                research_repository=SQLiteResearchRepository(conn), configuration=research_config, clock=clock,
                run_mode="deterministic", require_decision=False,
            )
            results.append(SymbolCycleResult(symbol=symbol, status="COMPLETED"))

        status = "COMPLETED" if all(r.status == "COMPLETED" for r in results) else (
            "FAILED" if all(r.status == "SKIPPED" for r in results) else "PARTIALLY_COMPLETE"
        )
        captured_symbol_results.extend(results)
        return ResearchCycleResult(
            cycle_id=f"cycle-{as_of.isoformat()}", universe_id=configuration.universe_id, as_of=as_of,
            status=status, symbol_results=tuple(results), reused_existing_cycle=False,
        )

    shadow_config = _shadow_config()
    result = run_due_shadow_cycle(
        now=DUE_NOW, conn=conn, shadow_config=shadow_config, cycle_configuration=_cycle_configuration(),
        candidate_symbols=lambda: ("AAPL",), run_cycle=_corporate_status_gated_stub,
        cycle_kwargs_builder=lambda syms, as_of: {}, pricing_entries=(), clock=_clock_at(DUE_NOW),
    )

    assert result.symbols_attempted == 1
    assert result.symbols_skipped == 1
    assert result.symbols_completed == 0
    assert claude_call_log == [], "Claude must never be called when corporate-status is a critical unknown"

    # No recommendation execution: nothing written to recommendations table.
    assert conn.execute("SELECT COUNT(*) AS c FROM recommendations").fetchone()["c"] == 0

    # The result carries an explicit evidence-completeness reason — not a
    # silent skip. `ShadowCycleRunResult` itself doesn't surface per-symbol
    # detail, so this reads it off the actual `SymbolCycleResult` the
    # real-shaped `run_cycle` produced (captured via closure above), the
    # same object `cli.py`/a real cycle_repository would persist.
    assert len(captured_symbol_results) == 1
    aapl_result = captured_symbol_results[0]
    assert aapl_result.status == "SKIPPED"
    assert aapl_result.evidence_outcome == "MISSING_CRITICAL_CORPORATE_STATUS"
    assert aapl_result.failure_reason is not None
    assert "MISSING_CRITICAL_CORPORATE_STATUS" in aapl_result.failure_reason
    assert "evidence-completeness-v1" in aapl_result.failure_reason
