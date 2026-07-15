from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_research.paper_books import cash_ledger
from trading_research.paper_books import recurring_scheduler as rs
from trading_research.paper_books import soak_campaign as campaign_mod
from trading_research.paper_books.config import (
    ExecutionSection, ExitsSection, LifecycleSection, PaperBookDefinition, PaperBooksConfiguration,
    PendingOrdersSection, RecurringScheduleSection, RecurringSection, RiskSection,
    ScheduledIntegrationSection, SoakCampaignSection, SoakSection, ValuationSection,
)
from trading_research.paper_books.controlled_soak_readiness import ControlledSoakReadinessResult
from trading_research.paper_books.cross_book_verification import persist_verification, verify_cross_book_integrity
from trading_research.shadow.config import load_shadow_operations_config
from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect
from trading_research.storage.research_cycle_repositories import SQLiteResearchCycleRepository

REVIEW_TIME = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
RUN_TIME = datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        c = connect(Path(tmp) / "recurring.db")
        yield c
        c.close()


def cfg(*, enabled=True, maximum_cycles=2, scheduled=True) -> PaperBooksConfiguration:
    return PaperBooksConfiguration(
        version=1, enabled=True,
        baseline=PaperBookDefinition(True, "BASELINE", Decimal("100000")),
        enhanced=PaperBookDefinition(True, "ENHANCED", Decimal("100000")),
        execution=ExecutionSection("local_simulated", False, False),
        risk=RiskSection(
            Decimal("0.9"), Decimal("50000"), Decimal("50000"), Decimal("0.02"), 20,
            Decimal("0.9"), 999999,
        ),
        valuation=ValuationSection("persisted_market_bar", 999999, "MARK_UNVALUED"),
        scheduled_integration=ScheduledIntegrationSection(scheduled),
        lifecycle=LifecycleSection(
            True, PendingOrdersSection(3), ExitsSection(False, Decimal("0.08"), Decimal("0.15"), 20, True),
            SoakSection(1, 1),
        ),
        soak_campaign=SoakCampaignSection(True, 1, 1, 1, 0, True),
        recurring=RecurringSection(
            enabled, RecurringScheduleSection("UTC", True, 13, 0), maximum_cycles, 900, 1200, 10, True,
        ),
        config_hash="recurring-test-config", raw={},
    )


def ready_review(conn, config):
    for book in (config.baseline, config.enhanced):
        cash_ledger.open_book(
            conn, book_id=book.book_id, starting_cash_usd=book.starting_cash_usd,
            config_hash=config.config_hash, clock=lambda: REVIEW_TIME,
        )
    verification = verify_cross_book_integrity(conn, as_of=REVIEW_TIME, paper_books_config=config)
    assert verification.status == "PASSED"
    persist_verification(conn, verification, operator_run_id=None, lifecycle_run_id=None, created_at=REVIEW_TIME)
    repo.save_soak_campaign(conn, {
        "campaign_id": "ready-campaign", "manifest_hash": "manifest", "config_hash": "campaign-config",
        "start_as_of": REVIEW_TIME, "end_as_of": REVIEW_TIME, "requested_date_count": 1,
        "requested_cycle_count": 1, "status": "COMPLETED_READY_FOR_REVIEW",
        "first_blocking_date": None, "first_blocking_status": None, "created_at": REVIEW_TIME,
    })
    record = {
        "activation_review_id": "ready-review", "campaign_id": "ready-campaign",
        "campaign_manifest_hash": "manifest", "completed_market_days": 1, "completed_cycles": 1,
        "provider_provenance_counts": {}, "provider_success_counts": {"real_provider_success_cycles": 1},
        "cross_book_verification_history": [{
            "as_of": REVIEW_TIME.isoformat(), "verification_id": verification.verification_id,
            "status": "PASSED", "stale_at_final_review": False,
        }],
        "reconciliation_history": [], "valuation_history": [], "alert_summary": {},
        "pause_and_kill_summary": {}, "performance_metrics": {}, "comparison_id": None,
        "promotion_evidence_status": "NOT_EVALUATED",
        "controlled_readiness_history": [{
            "as_of": REVIEW_TIME.isoformat(), "status": "READY_FOR_RECURRING_ACTIVATION_REVIEW",
            "all_failed_checks": [], "day_status": "COMPLETED",
        }],
        "final_recommendation": "READY_FOR_RECURRING_ACTIVATION_REVIEW", "reasons": ["ready"],
        "policy_version": "paper-soak-campaign/v1", "created_at": REVIEW_TIME,
    }
    repo.save_soak_activation_review(conn, record)
    return record


def activate_ready(conn, config):
    ready_review(conn, config)
    request = rs.request_recurring_activation(
        conn, activation_review_id="ready-review", operator="alice", reason="approved review",
        paper_books_config=config, now=REVIEW_TIME + timedelta(minutes=1),
    )
    assert rs.current_activation_state(conn).state == rs.STATE_ACTIVATION_REQUESTED
    activation = rs.activate_recurring(
        conn, request_event_id=request["activation_event_id"], operator="bob",
        paper_books_config=config, now=REVIEW_TIME + timedelta(minutes=2),
    )
    assert rs.current_activation_state(conn).state == rs.STATE_ACTIVE
    return request, activation


def test_configuration_alone_is_inactive(conn):
    assert cfg().recurring.enabled is True
    assert rs.current_activation_state(conn).state == rs.STATE_INACTIVE


def test_two_step_activation_deactivation_and_immutable_history(conn):
    config = cfg()
    request, activation = activate_ready(conn, config)
    assert request["activation_event_id"] != activation["activation_event_id"]
    assert len(repo.list_recurring_activation_events(conn)) == 2
    deactivated = rs.deactivate_recurring(
        conn, operator="alice", reason="maintenance", now=REVIEW_TIME + timedelta(minutes=3),
    )
    assert deactivated["new_state"] == rs.STATE_DEACTIVATED
    with pytest.raises(Exception, match="append-only"):
        conn.execute("UPDATE paper_recurring_activation_events SET reason = 'changed'")


def test_missing_and_stale_activation_reviews_fail(conn):
    config = cfg()
    with pytest.raises(rs.RecurringPaperError, match="does not exist"):
        rs.request_recurring_activation(
            conn, activation_review_id="missing", operator="a", reason="r",
            paper_books_config=config, now=RUN_TIME,
        )
    ready_review(conn, config)
    stale_now = REVIEW_TIME + timedelta(days=30)
    with pytest.raises(rs.RecurringPaperError, match="age"):
        rs.request_recurring_activation(
            conn, activation_review_id="ready-review", operator="a", reason="r",
            paper_books_config=config, now=stale_now,
        )


def save_completed_cycle(conn, cycle_id, when=REVIEW_TIME):
    cycle_repo = SQLiteResearchCycleRepository(conn)
    cycle_repo.save_cycle_started(cycle_id, "u", when, "hash", "OBSERVE_ONLY", "fixture", when)
    cycle_repo.mark_cycle_finished(cycle_id, "COMPLETED", when)


def test_queue_is_explicit_deterministic_bounded_and_cancellable(conn):
    for offset, cycle_id in enumerate(("cycle-b", "cycle-a", "cycle-c")):
        save_completed_cycle(conn, cycle_id)
        rs.enqueue_recurring_cycle(
            conn, cycle_id=cycle_id, operator="alice", reason="approved",
            now=REVIEW_TIME + timedelta(seconds=offset),
        )
    duplicate = rs.enqueue_recurring_cycle(
        conn, cycle_id="cycle-a", operator="bob", reason="duplicate", now=REVIEW_TIME + timedelta(seconds=1),
    )
    assert duplicate["cycle_id"] == "cycle-a"
    ordered = repo.list_recurring_queue_items(conn, status="QUEUED")
    assert [item["cycle_id"] for item in ordered] == ["cycle-b", "cycle-a", "cycle-c"]
    cancelled = rs.cancel_recurring_cycle(
        conn, queue_item_id=ordered[-1]["queue_item_id"], operator="alice", reason="withdrawn", now=RUN_TIME,
    )
    assert cancelled["status"] == "CANCELLED"
    claimed = rs._claim_queue_items(
        conn, scheduler_run_id="run", now=RUN_TIME, maximum=1, lease_ttl_seconds=10,
    )
    assert len(claimed) == 1


def test_queue_rejects_unknown_or_running_cycle(conn):
    with pytest.raises(rs.RecurringPaperError, match="unknown"):
        rs.enqueue_recurring_cycle(conn, cycle_id="missing", operator="a", reason="r", now=REVIEW_TIME)
    SQLiteResearchCycleRepository(conn).save_cycle_started(
        "running", "u", REVIEW_TIME, "h", "OBSERVE_ONLY", "fixture", REVIEW_TIME,
    )
    with pytest.raises(rs.RecurringPaperError, match="not completed"):
        rs.enqueue_recurring_cycle(conn, cycle_id="running", operator="a", reason="r", now=REVIEW_TIME)


def test_changed_frozen_cycle_fails_before_claim_and_processed_never_requeues(conn):
    save_completed_cycle(conn, "cycle-frozen")
    item = rs.enqueue_recurring_cycle(
        conn, cycle_id="cycle-frozen", operator="a", reason="r", now=REVIEW_TIME,
    )
    conn.execute("UPDATE research_cycles SET configuration_hash = 'changed' WHERE cycle_id = 'cycle-frozen'")
    conn.commit()
    assert rs._claim_queue_items(
        conn, scheduler_run_id="run", now=RUN_TIME, maximum=1, lease_ttl_seconds=10,
    ) == []
    assert repo.load_recurring_queue_item(conn, item["queue_item_id"])["status"] == "FAILED"
    retry = rs.enqueue_recurring_cycle(
        conn, cycle_id="cycle-frozen", operator="b", reason="explicit retry", now=RUN_TIME,
    )
    assert retry["retry_of_queue_item_id"] == item["queue_item_id"]
    conn.execute(
        "UPDATE paper_recurring_cycle_queue SET status = 'PROCESSED' WHERE queue_item_id = ?", (retry["queue_item_id"],)
    )
    conn.commit()
    assert rs.enqueue_recurring_cycle(
        conn, cycle_id="cycle-frozen", operator="c", reason="must not reprocess", now=RUN_TIME,
    )["queue_item_id"] == retry["queue_item_id"]


def test_lease_conflict_stale_recovery_and_owner_release(conn):
    first = rs.acquire_recurring_lease(
        conn, owner_id="one", scheduler_run_id="run-1", now=REVIEW_TIME, ttl_seconds=10,
    )
    assert isinstance(first, rs.RecurringLeaseHandle)
    conflict = rs.acquire_recurring_lease(
        conn, owner_id="two", scheduler_run_id="run-2", now=REVIEW_TIME + timedelta(seconds=5), ttl_seconds=10,
    )
    assert isinstance(conflict, rs.RecurringLeaseConflict)
    with pytest.raises(rs.RecurringPaperError, match="not held"):
        rs.release_recurring_lease(conn, owner_id="two", now=REVIEW_TIME + timedelta(seconds=5))
    recovered = rs.acquire_recurring_lease(
        conn, owner_id="two", scheduler_run_id="run-2", now=REVIEW_TIME + timedelta(seconds=11), ttl_seconds=10,
    )
    assert isinstance(recovered, rs.RecurringLeaseHandle)
    rs.release_recurring_lease(conn, owner_id="two", now=REVIEW_TIME + timedelta(seconds=12))


def test_due_slot_is_timezone_market_day_aware_and_one_local_date():
    config = cfg()
    assert rs.calculate_due_slot(datetime(2026, 7, 16, 12, 59, tzinfo=timezone.utc), config).due is False
    due = rs.calculate_due_slot(RUN_TIME, config)
    assert due.due is True
    assert due.intended_at.date().isoformat() == "2026-07-16"
    assert rs.calculate_due_slot(datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc), config).due is False


def _ready_controlled(*args, **kwargs):
    return ControlledSoakReadinessResult(
        status="READY_FOR_RECURRING_ACTIVATION_REVIEW", reasons=("ready",),
        paper_soak_status="READY_FOR_RECURRING_ACTIVATION_REVIEW",
        shadow_activation_status="READY_FOR_LIMITED_RECURRING_SHADOW", checks=(), policy_version="test",
    )


def make_scheduler_ready(monkeypatch):
    monkeypatch.setattr(rs, "evaluate_controlled_soak_readiness", _ready_controlled)
    monkeypatch.setattr(campaign_mod, "evaluate_controlled_soak_readiness", _ready_controlled)
    monkeypatch.setattr(
        rs, "compute_real_provider_history",
        lambda *a, **k: SimpleNamespace(real_provider_success_cycle_count=1),
    )


def test_scheduler_lifecycle_only_persists_and_replays_idempotently(conn, monkeypatch):
    config = cfg()
    activate_ready(conn, config)
    make_scheduler_ready(monkeypatch)
    result = rs.run_recurring_paper_scheduler(
        conn, now=RUN_TIME, paper_books_config=config, shadow_config=load_shadow_operations_config(),
        owner_id="owner", audit_clock=lambda: RUN_TIME,
    )
    assert result.status == rs.STATUS_COMPLETED
    assert result.lifecycle_only is True
    assert result.processed_cycle_ids == ()
    replay = rs.run_recurring_paper_scheduler(
        conn, now=RUN_TIME, paper_books_config=config, shadow_config=load_shadow_operations_config(),
        owner_id="owner-2", audit_clock=lambda: RUN_TIME,
    )
    assert replay.status == rs.STATUS_SKIPPED_ALREADY_COMPLETED
    assert conn.execute("SELECT COUNT(*) FROM paper_book_lifecycle_runs").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM paper_recurring_scheduler_leases").fetchone()[0] == "RELEASED"
    with pytest.raises(Exception, match="immutable"):
        conn.execute("UPDATE paper_recurring_scheduler_runs SET status = 'FAILED'")


def test_scheduler_processes_only_bounded_explicit_queue(conn, monkeypatch):
    config = cfg(maximum_cycles=2)
    activate_ready(conn, config)
    make_scheduler_ready(monkeypatch)
    for offset, cycle_id in enumerate(("cycle-1", "cycle-2", "cycle-3")):
        save_completed_cycle(conn, cycle_id)
        rs.enqueue_recurring_cycle(
            conn, cycle_id=cycle_id, operator="alice", reason="approved",
            now=REVIEW_TIME + timedelta(seconds=offset),
        )
    result = rs.run_recurring_paper_scheduler(
        conn, now=RUN_TIME, paper_books_config=config, shadow_config=load_shadow_operations_config(),
        owner_id="owner", audit_clock=lambda: RUN_TIME,
    )
    assert result.status == rs.STATUS_COMPLETED
    assert result.processed_cycle_ids == ("cycle-1", "cycle-2")
    assert [i["status"] for i in repo.list_recurring_queue_items(conn)] == ["PROCESSED", "PROCESSED", "QUEUED"]


def test_safety_block_pauses_and_requires_new_request(conn, monkeypatch):
    config = cfg()
    activate_ready(conn, config)
    make_scheduler_ready(monkeypatch)
    from trading_research.shadow.alerts import OperationalAlert, raise_alert
    raise_alert(conn, OperationalAlert(
        severity="CRITICAL", alert_type="RECONCILIATION_MISMATCH", message="stop", context={}, created_at=RUN_TIME,
    ), (), clock=lambda: RUN_TIME)
    result = rs.run_recurring_paper_scheduler(
        conn, now=RUN_TIME, paper_books_config=config, shadow_config=load_shadow_operations_config(),
        owner_id="owner", audit_clock=lambda: RUN_TIME,
    )
    assert result.status == rs.STATUS_BLOCKED_SAFETY
    assert "unresolved_critical_alerts" in result.all_failed_checks
    assert conn.execute("SELECT COUNT(*) FROM paper_book_lifecycle_runs").fetchone()[0] == 0
    assert rs.current_activation_state(conn).state == rs.STATE_PAUSED_BY_SAFETY
    later = rs.run_recurring_paper_scheduler(
        conn, now=RUN_TIME + timedelta(minutes=1), paper_books_config=config,
        shadow_config=load_shadow_operations_config(), owner_id="owner", audit_clock=lambda: RUN_TIME,
    )
    assert later.status == rs.STATUS_SKIPPED_INACTIVE


def test_all_simultaneous_pre_run_failures_are_ordered_and_returned(conn, monkeypatch):
    config = cfg()
    activate_ready(conn, config)
    make_scheduler_ready(monkeypatch)
    from trading_research.shadow import pause as pause_mod
    from trading_research.shadow.alerts import OperationalAlert, raise_alert
    pause_mod.kill(conn, "stop", "operator", clock=lambda: RUN_TIME)
    raise_alert(conn, OperationalAlert(
        severity="CRITICAL", alert_type="RECONCILIATION_MISMATCH", message="stop", context={}, created_at=RUN_TIME,
    ), (), clock=lambda: RUN_TIME)
    gates = rs.evaluate_pre_run_safety_gates(
        conn, now=RUN_TIME, paper_books_config=config, shadow_config=load_shadow_operations_config(),
    )
    failed = [gate.name for gate in gates if not gate.passed]
    assert failed[:4] == [
        "activation_review_valid", "shadow_kill_state", "shadow_pause_state", "unresolved_critical_alerts",
    ]


def test_exception_releases_lease_and_queue_claim(conn, monkeypatch):
    config = cfg()
    activate_ready(conn, config)
    make_scheduler_ready(monkeypatch)
    save_completed_cycle(conn, "cycle-error")
    rs.enqueue_recurring_cycle(conn, cycle_id="cycle-error", operator="a", reason="r", now=REVIEW_TIME)
    monkeypatch.setattr(rs, "run_controlled_soak_day", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    result = rs.run_recurring_paper_scheduler(
        conn, now=RUN_TIME, paper_books_config=config, shadow_config=load_shadow_operations_config(),
        owner_id="owner", audit_clock=lambda: RUN_TIME,
    )
    assert result.status == rs.STATUS_FAILED
    assert repo.list_recurring_queue_items(conn)[0]["status"] == "QUEUED"
    assert conn.execute("SELECT status FROM paper_recurring_scheduler_leases").fetchone()[0] == "RELEASED"


def test_crash_recovery_reuses_run_and_claim_without_duplicate_processing(conn, monkeypatch):
    config = cfg(maximum_cycles=1)
    _, activation = activate_ready(conn, config)
    make_scheduler_ready(monkeypatch)
    save_completed_cycle(conn, "cycle-crash")
    item = rs.enqueue_recurring_cycle(conn, cycle_id="cycle-crash", operator="a", reason="r", now=REVIEW_TIME)
    slot = rs.calculate_due_slot(RUN_TIME, config)
    run_id = rs._scheduler_run_id(slot.intended_schedule_id)
    repo.save_recurring_scheduler_run_started(conn, {
        "scheduler_run_id": run_id, "intended_schedule_id": slot.intended_schedule_id,
        "intended_at": slot.intended_at, "started_at": REVIEW_TIME, "owner_id": "crashed",
        "lease_name": rs.LEASE_NAME, "activation_event_id": activation["activation_event_id"],
        "activation_review_id": "ready-review", "config_hash": rs.recurring_config_hash(config),
        "policy_version": rs.POLICY_VERSION, "created_at": REVIEW_TIME,
    })
    claimed = rs._claim_queue_items(
        conn, scheduler_run_id=run_id, now=REVIEW_TIME, maximum=1, lease_ttl_seconds=10,
    )
    assert claimed[0]["queue_item_id"] == item["queue_item_id"]
    rs.acquire_recurring_lease(
        conn, owner_id="crashed", scheduler_run_id=run_id, now=REVIEW_TIME, ttl_seconds=10,
    )
    recovered = rs.run_recurring_paper_scheduler(
        conn, now=RUN_TIME, paper_books_config=config, shadow_config=load_shadow_operations_config(),
        owner_id="recovery", audit_clock=lambda: RUN_TIME,
    )
    assert recovered.status == rs.STATUS_COMPLETED
    assert recovered.processed_cycle_ids == ("cycle-crash",)
    assert repo.load_recurring_queue_item(conn, item["queue_item_id"])["status"] == "PROCESSED"
    assert conn.execute("SELECT COUNT(*) FROM paper_recurring_scheduler_runs").fetchone()[0] == 1
