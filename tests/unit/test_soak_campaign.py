from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_research.paper_books.config import (
    ExecutionSection, ExitsSection, LifecycleSection, PaperBookDefinition, PaperBooksConfiguration,
    PendingOrdersSection, RiskSection, ScheduledIntegrationSection, SoakCampaignSection, SoakSection,
    ValuationSection,
)
from trading_research.paper_books.soak_campaign import (
    DAY_BLOCKED, DAY_COMPLETED, DAY_SKIPPED, SoakCampaignError, build_activation_review, run_soak_campaign,
    validate_campaign_manifest,
)
from trading_research.shadow import pause as pause_mod
from trading_research.shadow.config import load_shadow_operations_config
from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import connect

DAY1 = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 7, 16, 20, 0, tzinfo=timezone.utc)
DAY3 = datetime(2026, 7, 17, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        connection = connect(Path(tmp) / "campaign.db")
        yield connection
        connection.close()


def _config(*, campaign_enabled: bool = True) -> PaperBooksConfiguration:
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
        scheduled_integration=ScheduledIntegrationSection(False),
        lifecycle=LifecycleSection(
            True, PendingOrdersSection(3), ExitsSection(True, Decimal("0.08"), Decimal("0.15"), 20, True),
            SoakSection(1, 1),
        ),
        config_hash="campaign-config-hash", raw={},
        soak_campaign=SoakCampaignSection(campaign_enabled, 3, 1, 1, 0, True),
    )


def _manifest():
    return validate_campaign_manifest({
        "campaign_id": "manual-soak-july-2026",
        "dates": [
            {"as_of": DAY1.isoformat(), "cycle_ids": []},
            {"as_of": DAY2.isoformat(), "cycle_ids": []},
            {"as_of": DAY3.isoformat(), "cycle_ids": []},
        ],
    })


def test_valid_manifest_and_lifecycle_only_date():
    manifest = _manifest()
    assert len(manifest.dates) == 3
    assert manifest.dates[0].cycle_ids == ()
    assert manifest.manifest_hash


@pytest.mark.parametrize("raw, match", [
    ({"campaign_id": "x", "dates": [{"as_of": DAY1.isoformat(), "cycle_ids": [], "extra": 1}]}, "unknown"),
    ({"campaign_id": "x", "dates": [{"as_of": "2026-07-15T20:00:00", "cycle_ids": []}]}, "timezone-aware"),
    ({"campaign_id": "x", "dates": [
        {"as_of": DAY2.isoformat(), "cycle_ids": []}, {"as_of": DAY1.isoformat(), "cycle_ids": []},
    ]}, "strictly increasing"),
    ({"campaign_id": "x", "dates": [{"as_of": DAY1.isoformat(), "cycle_ids": ["c", "c"]}]}, "duplicate cycle"),
])
def test_invalid_manifests_fail_closed(raw, match):
    with pytest.raises(SoakCampaignError, match=match):
        validate_campaign_manifest(raw)


def test_campaign_disabled_fails_closed(conn):
    with pytest.raises(SoakCampaignError, match="enabled is false"):
        run_soak_campaign(
            conn, manifest=_manifest(), paper_books_config=_config(campaign_enabled=False),
            shadow_config=load_shadow_operations_config(),
        )


def test_three_day_historical_campaign_is_point_in_time_and_idempotent(conn):
    manifest = _manifest()
    first = run_soak_campaign(
        conn, manifest=manifest, paper_books_config=_config(), shadow_config=load_shadow_operations_config(),
        audit_clock=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    assert [day["day_status"] for day in first["days"]] == [DAY_COMPLETED] * 3
    assert [day["as_of"] for day in first["days"]] == [d.as_of.isoformat() for d in manifest.dates]
    lifecycle_runs = repo.list_lifecycle_runs(conn)
    assert [run["as_of"] for run in lifecycle_runs] == [d.as_of.isoformat() for d in manifest.dates]
    assert all(run["created_at"] == run["as_of"] for run in lifecycle_runs)
    assert first["activation_review"]["completed_market_days"] == 3
    assert first["activation_review"]["final_recommendation"] == "INSUFFICIENT_EVIDENCE"

    second = run_soak_campaign(
        conn, manifest=manifest, paper_books_config=_config(), shadow_config=load_shadow_operations_config(),
    )
    assert second["campaign"]["campaign_id"] == first["campaign"]["campaign_id"]
    assert len(repo.list_soak_campaign_days(conn, manifest.campaign_id)) == 3
    assert conn.execute("SELECT COUNT(*) FROM paper_soak_campaigns").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM paper_soak_activation_reviews").fetchone()[0] == 1
    pause_mod.kill(conn, "future event", "operator", clock=lambda: datetime(2026, 7, 21, tzinfo=timezone.utc))
    frozen = build_activation_review(
        conn, manifest=manifest, config=_config(),
        campaign_attempt_id=first["latest_attempt_id"],
        audit_clock=lambda: datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    assert frozen["activation_review_id"] == first["activation_review"]["activation_review_id"]
    assert conn.execute("SELECT COUNT(*) FROM paper_soak_activation_reviews").fetchone()[0] == 1


def test_hard_blocker_stops_later_dates_by_default(conn):
    pause_mod.kill(conn, "test kill", "operator", clock=lambda: DAY1)
    result = run_soak_campaign(
        conn, manifest=_manifest(), paper_books_config=_config(), shadow_config=load_shadow_operations_config(),
    )
    assert [day["day_status"] for day in result["days"]] == [DAY_BLOCKED, DAY_SKIPPED, DAY_SKIPPED]
    assert len(result["days"][0]["all_failed_checks"]) >= 4
    assert result["campaign"]["status"] == "BLOCKED"
    assert result["activation_review"]["final_recommendation"] == "BLOCKED_REQUIRES_REMEDIATION"


def test_explicit_continue_on_blocker_processes_every_date(conn):
    pause_mod.kill(conn, "test kill", "operator", clock=lambda: DAY1)
    result = run_soak_campaign(
        conn, manifest=_manifest(), paper_books_config=_config(), shadow_config=load_shadow_operations_config(),
        stop_on_blocker=False,
    )
    assert [day["day_status"] for day in result["days"]] == [DAY_BLOCKED] * 3
    assert all(day["day_status"] != DAY_SKIPPED for day in result["days"])


def test_remediation_continuation_creates_new_attempt_without_rewriting_first(conn):
    manifest = _manifest()
    pause_mod.kill(conn, "test kill", "operator", clock=lambda: DAY1)
    first = run_soak_campaign(
        conn, manifest=manifest, paper_books_config=_config(), shadow_config=load_shadow_operations_config(),
    )
    first_days = [dict(day) for day in first["attempts"][0]["days"]]
    pause_mod.force_clear_kill(conn, "remediated", "operator", clock=lambda: DAY1)
    second = run_soak_campaign(
        conn, manifest=manifest, paper_books_config=_config(), shadow_config=load_shadow_operations_config(),
        stop_on_blocker=False, continuation_operator="operator", continuation_reason="remediated kill state",
    )
    assert len(second["attempts"]) == 2
    assert len(second["activation_reviews"]) == 2
    assert second["activation_review"]["supersedes_activation_review_id"] == first["activation_review"]["activation_review_id"]
    assert repo.load_soak_activation_review(conn, first["activation_review"]["activation_review_id"]) == first["activation_review"]
    assert second["attempts"][0]["days"] == first_days
    assert [day["day_status"] for day in second["days"]] == [DAY_COMPLETED] * 3
    assert all(day["as_of"] != DAY1.isoformat() for day in second["attempts"][0]["days"][1:])

    replay = run_soak_campaign(
        conn, manifest=manifest, paper_books_config=_config(), shadow_config=load_shadow_operations_config(),
        stop_on_blocker=False, continuation_operator="operator", continuation_reason="remediated kill state",
    )
    assert len(replay["attempts"]) == 2


def test_equivalent_offsets_have_same_manifest_hash_and_preclose_fails():
    utc = validate_campaign_manifest({
        "campaign_id": "offsets", "dates": [{"as_of": "2026-07-15T20:00:00+00:00", "cycle_ids": []}],
    })
    eastern = validate_campaign_manifest({
        "campaign_id": "offsets", "dates": [{"as_of": "2026-07-15T16:00:00-04:00", "cycle_ids": []}],
    })
    assert utc.manifest_hash == eastern.manifest_hash
    with pytest.raises(SoakCampaignError, match="before the regular market close"):
        validate_campaign_manifest({
            "campaign_id": "preclose", "dates": [{"as_of": "2026-07-15T19:59:59Z", "cycle_ids": []}],
        })
    lifecycle_only = validate_campaign_manifest({
        "campaign_id": "weekend", "dates": [
            {"as_of": "2026-07-18T20:00:00Z", "cycle_ids": [], "lifecycle_only": True},
        ],
    })
    assert lifecycle_only.dates[0].lifecycle_only is True


def test_crash_after_lifecycle_persistence_requires_review_without_duplicate(conn, monkeypatch):
    from trading_research.paper_books import lifecycle, soak_campaign as campaign_module

    original = campaign_module.run_controlled_soak_day

    def crash_after_lifecycle(connection, **kwargs):
        lifecycle.run_paper_book_lifecycle(
            connection, as_of=kwargs["as_of"], paper_books_config=kwargs["paper_books_config"],
            integrate_cycle_ids=kwargs["cycle_ids"],
        )
        raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(campaign_module, "run_controlled_soak_day", crash_after_lifecycle)
    with pytest.raises(KeyboardInterrupt):
        run_soak_campaign(
            conn, manifest=_manifest(), paper_books_config=_config(),
            shadow_config=load_shadow_operations_config(),
        )
    assert repo.list_soak_campaign_attempts(conn, _manifest().campaign_id)[0]["status"] == "RUNNING"
    assert len(repo.list_lifecycle_runs(conn)) == 1

    monkeypatch.setattr(campaign_module, "run_controlled_soak_day", original)
    recovered_manifest = campaign_module.manifest_from_persisted_campaign(conn, _manifest().campaign_id)
    assert len(recovered_manifest.dates) == 3
    resumed = run_soak_campaign(
        conn, manifest=recovered_manifest, paper_books_config=_config(), shadow_config=load_shadow_operations_config(),
    )
    assert resumed["days"][0]["day_status"] == "RECOVERY_REQUIRES_REVIEW"
    assert len(repo.list_lifecycle_runs(conn)) == 1


def test_unexpected_exception_fails_attempt_and_aborts_remaining_dates(conn, monkeypatch):
    from trading_research.paper_books import soak_campaign as campaign_module

    monkeypatch.setattr(
        campaign_module, "run_controlled_soak_day",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("sensitive raw details")),
    )
    with pytest.raises(SoakCampaignError, match="UNEXPECTED_RUNTIMEERROR"):
        run_soak_campaign(
            conn, manifest=_manifest(), paper_books_config=_config(),
            shadow_config=load_shadow_operations_config(),
        )
    attempt = repo.list_soak_campaign_attempts(conn, _manifest().campaign_id)[0]
    assert attempt["status"] == "FAILED"
    assert attempt["sanitized_message"] == "unexpected campaign day failure"
    assert "sensitive" not in str(attempt)
    assert repo.list_soak_campaign_attempt_days(conn, attempt["campaign_attempt_id"]) == []
