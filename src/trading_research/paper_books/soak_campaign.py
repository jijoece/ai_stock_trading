"""Manual, point-in-time controlled paper-soak campaigns (Milestone 9.3).

No scheduler, provider, external broker, or live-trading path exists here.
Cycle IDs and historical dates come only from a validated bounded manifest.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from ..hashing import hash_config
from ..research.provider_provenance import compute_real_provider_history
from ..shadow import pause as pause_mod
from ..shadow import readiness as shadow_readiness
from ..storage import paper_books_repositories as pb_repo
from ..storage import shadow_alerts_repositories as alerts_repo
from ..storage import shadow_operations_repositories as shadow_ops_repo
from . import lifecycle
from .config import PaperBooksConfiguration
from .controlled_soak_readiness import evaluate_controlled_soak_readiness
from .cross_book_verification import persist_verification, verification_is_stale, verify_cross_book_integrity

POLICY_VERSION = "paper-soak-campaign/v1"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_CAMPAIGN_DATES = 366
MAX_CYCLES_PER_DATE = 100
MAX_IDENTIFIER_LENGTH = 128

CAMPAIGN_BLOCKED = "BLOCKED"
CAMPAIGN_COMPLETED_NOT_READY = "COMPLETED_NOT_READY"
CAMPAIGN_COMPLETED_READY = "COMPLETED_READY_FOR_REVIEW"
DAY_COMPLETED = "COMPLETED"
DAY_COMPLETED_WARNINGS = "COMPLETED_WITH_WARNINGS"
DAY_BLOCKED = "BLOCKED"
DAY_FAILED = "FAILED"
DAY_SKIPPED = "SKIPPED_AFTER_BLOCKER"

RECOMMENDATION_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
RECOMMENDATION_CONTINUE = "CONTINUE_MANUAL_SOAK"
RECOMMENDATION_BLOCKED = "BLOCKED_REQUIRES_REMEDIATION"
RECOMMENDATION_READY = "READY_FOR_RECURRING_ACTIVATION_REVIEW"


class SoakCampaignError(RuntimeError):
    pass


@dataclass(frozen=True)
class SoakCampaignDate:
    as_of: datetime
    cycle_ids: tuple[str, ...]


@dataclass(frozen=True)
class SoakCampaignManifest:
    campaign_id: str
    dates: tuple[SoakCampaignDate, ...]
    manifest_hash: str


def _parse_aware_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise SoakCampaignError("manifest date as_of must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SoakCampaignError(f"invalid manifest as_of {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SoakCampaignError(f"manifest as_of {value!r} must be timezone-aware")
    return parsed


def validate_campaign_manifest(raw: object) -> SoakCampaignManifest:
    if not isinstance(raw, dict):
        raise SoakCampaignError("campaign manifest must be a JSON object")
    unknown = set(raw) - {"campaign_id", "dates"}
    if unknown:
        raise SoakCampaignError(f"campaign manifest has unknown keys: {sorted(unknown)}")
    campaign_id = raw.get("campaign_id")
    if not isinstance(campaign_id, str) or not campaign_id.strip() or len(campaign_id) > MAX_IDENTIFIER_LENGTH:
        raise SoakCampaignError(f"campaign_id is required and must be <= {MAX_IDENTIFIER_LENGTH} characters")
    dates_raw = raw.get("dates")
    if not isinstance(dates_raw, list) or not dates_raw:
        raise SoakCampaignError("campaign manifest dates must be a non-empty list")
    if len(dates_raw) > MAX_CAMPAIGN_DATES:
        raise SoakCampaignError(f"campaign manifest exceeds {MAX_CAMPAIGN_DATES} dates")

    dates: list[SoakCampaignDate] = []
    previous: datetime | None = None
    for index, item in enumerate(dates_raw):
        if not isinstance(item, dict):
            raise SoakCampaignError(f"manifest dates[{index}] must be an object")
        unknown = set(item) - {"as_of", "cycle_ids"}
        if unknown:
            raise SoakCampaignError(f"manifest dates[{index}] has unknown keys: {sorted(unknown)}")
        if "cycle_ids" not in item or not isinstance(item["cycle_ids"], list):
            raise SoakCampaignError(f"manifest dates[{index}].cycle_ids must be an explicit list")
        if len(item["cycle_ids"]) > MAX_CYCLES_PER_DATE:
            raise SoakCampaignError(f"manifest dates[{index}] exceeds {MAX_CYCLES_PER_DATE} cycle IDs")
        cycle_ids: list[str] = []
        for cycle_id in item["cycle_ids"]:
            if not isinstance(cycle_id, str) or not cycle_id or len(cycle_id) > MAX_IDENTIFIER_LENGTH:
                raise SoakCampaignError(f"manifest dates[{index}] contains an invalid cycle ID")
            cycle_ids.append(cycle_id)
        if len(set(cycle_ids)) != len(cycle_ids):
            raise SoakCampaignError(f"manifest dates[{index}] contains duplicate cycle IDs")
        as_of = _parse_aware_timestamp(item.get("as_of"))
        if previous is not None and as_of <= previous:
            raise SoakCampaignError("campaign dates must be strictly increasing; duplicate dates are rejected")
        previous = as_of
        dates.append(SoakCampaignDate(as_of=as_of, cycle_ids=tuple(cycle_ids)))

    canonical = {
        "campaign_id": campaign_id,
        "dates": [{"as_of": day.as_of.isoformat(), "cycle_ids": list(day.cycle_ids)} for day in dates],
    }
    manifest_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return SoakCampaignManifest(campaign_id=campaign_id, dates=tuple(dates), manifest_hash=manifest_hash)


def load_campaign_manifest(path: str | Path) -> SoakCampaignManifest:
    manifest_path = Path(path)
    try:
        size = manifest_path.stat().st_size
        if size > MAX_MANIFEST_BYTES:
            raise SoakCampaignError(f"campaign manifest exceeds {MAX_MANIFEST_BYTES} bytes")
        raw = json.loads(manifest_path.read_text())
    except SoakCampaignError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise SoakCampaignError(f"cannot read valid campaign JSON at {manifest_path}: {exc}") from exc
    return validate_campaign_manifest(raw)


def campaign_config_hash(config: PaperBooksConfiguration) -> str:
    section = config.soak_campaign
    return hash_config({
        "paper_books_config_hash": config.config_hash,
        "soak_campaign": {
            "enabled": section.enabled, "minimum_market_days": section.minimum_market_days,
            "minimum_completed_cycles": section.minimum_completed_cycles,
            "minimum_successful_real_provider_cycles": section.minimum_successful_real_provider_cycles,
            "maximum_unresolved_warnings": section.maximum_unresolved_warnings,
            "stop_on_blocker": section.stop_on_blocker,
        },
    })


def operator_run_id(as_of: datetime, cycle_ids: tuple[str, ...]) -> str:
    digest = hashlib.sha256(
        f"{as_of.isoformat()}|{','.join(sorted(cycle_ids))}|paper-soak-operator-run-v1".encode()
    ).hexdigest()[:32]
    return f"pb-soak-op-{digest}"


def run_controlled_soak_day(
    conn, *, as_of: datetime, cycle_ids: tuple[str, ...], paper_books_config: PaperBooksConfiguration,
    shadow_config, audit_clock: Callable[[], datetime] | None = None, price_provider=None,
) -> dict:
    """Shared service for one ``paper-soak-run`` day and campaign days."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise SoakCampaignError("controlled soak as_of must be timezone-aware")
    if not paper_books_config.enabled or not paper_books_config.lifecycle.enabled:
        raise SoakCampaignError("paper books and lifecycle must both be enabled")
    audit_clock = audit_clock or (lambda: datetime.now(timezone.utc))

    pause_state = pause_mod.current_state(conn)
    if pause_state.is_blocking:
        readiness = evaluate_controlled_soak_readiness(conn, as_of, paper_books_config, shadow_config)
        return {
            "blocked_before_lifecycle": True, "block_reason": pause_state.reason,
            "controlled_readiness": readiness, "operator_run": None, "lifecycle_result": None,
            "verification": None, "soak_report": None,
        }

    op_id = operator_run_id(as_of, cycle_ids)
    lifecycle_result = lifecycle.run_paper_book_lifecycle(
        conn, as_of=as_of, paper_books_config=paper_books_config,
        integrate_cycle_ids=cycle_ids, clock=None, price_provider=price_provider,
    )
    verification = verify_cross_book_integrity(
        conn, as_of=as_of, paper_books_config=paper_books_config, operator_run_id=op_id,
        lifecycle_run_id=lifecycle_result.lifecycle_run_id,
    )
    persist_verification(
        conn, verification, operator_run_id=op_id, lifecycle_run_id=lifecycle_result.lifecycle_run_id,
        created_at=audit_clock(),
    )

    # Import at service invocation to avoid a module-level cycle: legacy
    # paper readiness currently lives in cli_support and is itself reused by
    # controlled_soak_readiness.
    from . import cli_support
    soak_report = cli_support._build_soak_report(conn, as_of, paper_books_config)
    thresholds = shadow_readiness.ReadinessThresholds(
        min_real_provider_cycles_for_ready=paper_books_config.soak_campaign.minimum_successful_real_provider_cycles,
    )
    readiness = evaluate_controlled_soak_readiness(
        conn, as_of, paper_books_config, shadow_config, shadow_thresholds=thresholds,
    )
    record = {
        "operator_run_id": op_id, "as_of": as_of, "requested_cycle_ids": list(cycle_ids),
        "lifecycle_run_id": lifecycle_result.lifecycle_run_id,
        "baseline_reconciliation_status": lifecycle_result.reconciliation_statuses.get(paper_books_config.baseline.book_id),
        "enhanced_reconciliation_status": lifecycle_result.reconciliation_statuses.get(paper_books_config.enhanced.book_id),
        "soak_report_status": soak_report["status"], "controlled_readiness_status": readiness.status,
        "failure_reasons": list(lifecycle_result.failure_reasons), "policy_version": readiness.policy_version,
        "created_at": audit_clock(), "cross_book_verification_id": verification.verification_id,
        "cross_book_verification_status": verification.status,
    }
    pb_repo.save_operator_run(conn, record)
    return {
        "blocked_before_lifecycle": False, "operator_run": pb_repo.load_operator_run(conn, op_id),
        "lifecycle_result": lifecycle_result, "verification": verification,
        "soak_report": soak_report, "controlled_readiness": readiness,
    }


def _hard_blocker(day: dict) -> bool:
    readiness = day["controlled_readiness"]
    hard_statuses = {
        "NOT_READY_SHADOW_PAUSED", "NOT_READY_SHADOW_KILLED", "NOT_READY_HEALTH_UNEXPLAINED",
        "NOT_READY_CRITICAL_ALERTS", "NOT_READY_RECONCILIATION", "NOT_READY_VALUATION", "NOT_READY_CROSS_BOOK",
    }
    if readiness.status in hard_statuses:
        return True
    failed = {c.name for c in readiness.checks if c.passed is False}
    return "lifecycle_failures" in failed


def _serialize_check_history(days: list[dict]) -> list[dict]:
    return [
        {"as_of": day["as_of"], "status": day["controlled_readiness_status"],
         "all_failed_checks": list(day["all_failed_checks"]), "day_status": day["day_status"]}
        for day in days
    ]


def _activation_review_id(campaign_id: str, manifest_hash: str) -> str:
    digest = hashlib.sha256(f"{campaign_id}|{manifest_hash}|{POLICY_VERSION}".encode()).hexdigest()[:32]
    return f"pb-soak-review-{digest}"


def build_activation_review(
    conn, *, manifest: SoakCampaignManifest, config: PaperBooksConfiguration,
    audit_clock: Callable[[], datetime] | None = None,
) -> dict:
    audit_clock = audit_clock or (lambda: datetime.now(timezone.utc))
    days = pb_repo.list_soak_campaign_days(conn, manifest.campaign_id)
    end_as_of = manifest.dates[-1].as_of
    lifecycle_ids = [d["lifecycle_run_id"] for d in days if d.get("lifecycle_run_id")]
    lifecycle_runs = [pb_repo.load_lifecycle_run(conn, run_id) for run_id in lifecycle_ids]
    lifecycle_runs = [r for r in lifecycle_runs if r is not None]
    integrated_cycle_ids = sorted({cycle for run in lifecycle_runs for cycle in run["processed_cycle_ids"]})
    completed_market_days = len({r["as_of"][:10] for r in lifecycle_runs})
    provenance = compute_real_provider_history(conn, end_as_of)

    verification_history = []
    for day in days:
        verification_id = day.get("cross_book_verification_id")
        if not verification_id:
            continue
        verification = pb_repo.load_cross_book_verification(conn, verification_id)
        verification_history.append({
            "as_of": day["as_of"], "verification_id": verification_id,
            "status": verification["status"], "stale_at_final_review": verification_is_stale(conn, verification, end_as_of),
        })

    reconciliation_history = []
    valuation_history = []
    performance: dict[str, object] = {
        "requested_cycle_ids": sorted({cycle for day in manifest.dates for cycle in day.cycle_ids}),
        "successfully_integrated_cycle_ids": integrated_cycle_ids,
        "requested_market_days": len(manifest.dates),
        "books": {},
    }
    for book_id in (config.baseline.book_id, config.enhanced.book_id):
        reconciliations = [r for r in pb_repo.list_reconciliations(conn, book_id) if r["as_of"] <= end_as_of.isoformat()]
        reconciliation_history.extend({"book_id": book_id, "as_of": r["as_of"], "status": r["status"]} for r in reconciliations)
        snapshots = [s for s in pb_repo.list_snapshots(conn, book_id) if s["as_of"] <= end_as_of.isoformat()]
        valuation_history.extend({"book_id": book_id, "as_of": s["as_of"], "status": s["valuation_status"]} for s in snapshots)
        latest = snapshots[-1] if snapshots else None
        orders = [o for o in pb_repo.list_order_intents(conn, book_id) if o["as_of"] <= end_as_of.isoformat()]
        fills = [f for f in pb_repo.list_fills(conn, book_id) if f["fill_timestamp"] <= end_as_of.isoformat()]
        book = pb_repo.load_book(conn, book_id)
        net_liq = Decimal(latest["net_liquidation_value_usd"]) if latest and latest["net_liquidation_value_usd"] is not None else None
        starting_cash = book.starting_cash_usd if book else None
        peak: Decimal | None = None
        maximum_drawdown: Decimal | None = None
        for snapshot in snapshots:
            if snapshot["net_liquidation_value_usd"] is None:
                continue
            value = Decimal(snapshot["net_liquidation_value_usd"])
            peak = value if peak is None else max(peak, value)
            drawdown = (value - peak) / peak if peak else Decimal("0")
            maximum_drawdown = drawdown if maximum_drawdown is None else min(maximum_drawdown, drawdown)
        performance["books"][book_id] = {
            "net_liquidation_value_usd": str(net_liq) if net_liq is not None else None,
            "return": str((net_liq / starting_cash) - 1) if net_liq is not None and starting_cash else None,
            "realized_pnl_usd": latest["realized_pnl_usd"] if latest else None,
            "unrealized_pnl_usd": latest["unrealized_pnl_usd"] if latest else None,
            "open_positions": len(pb_repo.list_positions(conn, book_id, open_only=True)),
            "closed_trades": len([f for f in fills if f["side"] == "SELL"]),
            "pending_orders": len([o for o in orders if o["status"] == "PENDING_SUBMISSION"]),
            "maximum_drawdown": str(maximum_drawdown) if maximum_drawdown is not None else None,
        }

    all_alerts = alerts_repo.list_alerts(conn)
    unresolved = [a for a in all_alerts if a.get("resolved_at") is None]
    unresolved_warnings = [a for a in unresolved if a["severity"] == "WARNING"]
    alert_summary = {
        "unresolved_count": len(unresolved), "unresolved_warning_count": len(unresolved_warnings),
        "unresolved_critical_count": sum(a["severity"] == "CRITICAL" for a in unresolved),
        "resolved_critical_history": [
            {"alert_id": a["alert_id"], "resolved_at": a.get("resolved_at")} for a in all_alerts
            if a["severity"] == "CRITICAL" and a.get("resolved_at") is not None
        ],
    }
    pause_history = shadow_ops_repo.list_pause_state_history(conn)
    current_pause = pause_mod.current_state(conn)
    pause_summary = {
        "current_state": current_pause.state,
        "history": [
            {"state": row["state"], "reason": row["reason"], "changed_at": row["created_at"]}
            for row in pause_history
        ],
    }
    cost_rows = conn.execute("SELECT cost_usd FROM shadow_run_summaries WHERE cost_usd IS NOT NULL").fetchall()
    performance["estimated_model_cost_usd"] = str(sum((Decimal(r["cost_usd"]) for r in cost_rows), Decimal("0")))

    comparison = conn.execute(
        "SELECT comparison_id FROM paper_book_experiment_comparisons ORDER BY window_end DESC, created_at DESC LIMIT 1"
    ).fetchone()
    comparison_id = comparison["comparison_id"] if comparison else None
    performance["baseline_vs_enhanced_comparison"] = None
    if comparison_id:
        comparison_record = pb_repo.load_experiment_comparison(conn, comparison_id)
        performance["baseline_vs_enhanced_comparison"] = {
            "comparison_id": comparison_id, "comparable": bool(comparison_record["comparable"]),
            "metric_deltas": {
                key: str(value) if value is not None else None
                for key, value in comparison_record["metric_deltas"].items()
            },
        }
    promotion = None
    if comparison_id:
        promotion = conn.execute(
            "SELECT result FROM paper_book_promotion_evidence WHERE comparison_id = ? ORDER BY created_at DESC LIMIT 1",
            (comparison_id,),
        ).fetchone()
    promotion_status = promotion["result"] if promotion else "NOT_EVALUATED"
    performance["stale_verification_count"] = sum(
        bool(item["stale_at_final_review"]) for item in verification_history
    )
    performance["lifecycle_failures"] = [
        reason for day in days for reason in day["failure_reasons"] if day["day_status"] != DAY_SKIPPED
    ]

    hard_blocked = any(day["day_status"] in (DAY_BLOCKED, DAY_FAILED) for day in days)
    sample_floors_met = (
        completed_market_days >= config.soak_campaign.minimum_market_days
        and len(integrated_cycle_ids) >= config.soak_campaign.minimum_completed_cycles
        and provenance.real_provider_success_cycle_count >= config.soak_campaign.minimum_successful_real_provider_cycles
    )
    warnings_within_limit = len(unresolved_warnings) <= config.soak_campaign.maximum_unresolved_warnings
    last_status = next((d["controlled_readiness_status"] for d in reversed(days) if d["day_status"] != DAY_SKIPPED), None)
    if hard_blocked:
        recommendation = RECOMMENDATION_BLOCKED
        reasons = ["at least one campaign date has an unresolved hard blocker"]
    elif not lifecycle_runs:
        recommendation = RECOMMENDATION_INSUFFICIENT
        reasons = ["no campaign date completed lifecycle processing"]
    elif not sample_floors_met:
        recommendation = RECOMMENDATION_INSUFFICIENT
        reasons = ["one or more required market-day, completed-cycle, or successful-provider floors are unsatisfied"]
    elif not warnings_within_limit:
        recommendation = RECOMMENDATION_BLOCKED
        reasons = ["unresolved warning count exceeds the configured campaign maximum"]
    elif last_status == "READY_FOR_RECURRING_ACTIVATION_REVIEW":
        recommendation = RECOMMENDATION_READY
        reasons = ["configured evidence floors and final controlled-readiness review are satisfied"]
    else:
        recommendation = RECOMMENDATION_CONTINUE
        reasons = ["one or more evidence floors or final controlled-readiness gates remain unsatisfied"]

    record = {
        "activation_review_id": _activation_review_id(manifest.campaign_id, manifest.manifest_hash),
        "campaign_id": manifest.campaign_id, "campaign_manifest_hash": manifest.manifest_hash,
        "completed_market_days": completed_market_days, "completed_cycles": len(integrated_cycle_ids),
        "provider_provenance_counts": {
            "fixture_only_cycles": provenance.fixture_only_cycle_count,
            "successful_real_evidence_cycles": provenance.real_evidence_only_cycle_count,
            "successful_real_claude_cycles": provenance.real_claude_only_cycle_count,
            "successful_real_evidence_and_claude_cycles": provenance.real_evidence_and_claude_cycle_count,
            "mixed_cycles": provenance.mixed_cycle_count, "unknown_cycles": provenance.unknown_cycle_count,
        },
        "provider_success_counts": {
            "real_provider_attempt_cycles": provenance.real_provider_attempt_cycle_count,
            "real_provider_success_cycles": provenance.real_provider_success_cycle_count,
            "failed_real_provider_cycles": provenance.real_provider_failure_cycle_count,
            "partial_provider_cycles": provenance.partial_provider_cycle_count,
        },
        "cross_book_verification_history": verification_history,
        "reconciliation_history": reconciliation_history, "valuation_history": valuation_history,
        "alert_summary": alert_summary, "pause_and_kill_summary": pause_summary,
        "performance_metrics": performance, "comparison_id": comparison_id,
        "promotion_evidence_status": promotion_status,
        "controlled_readiness_history": _serialize_check_history(days),
        "final_recommendation": recommendation, "reasons": reasons,
        "policy_version": POLICY_VERSION, "created_at": audit_clock(),
    }
    pb_repo.save_soak_activation_review(conn, record)
    return pb_repo.load_soak_activation_review(conn, record["activation_review_id"])


def run_soak_campaign(
    conn, *, manifest: SoakCampaignManifest, paper_books_config: PaperBooksConfiguration, shadow_config,
    stop_on_blocker: bool = True, audit_clock: Callable[[], datetime] | None = None,
) -> dict:
    if not paper_books_config.soak_campaign.enabled:
        raise SoakCampaignError("paper_books.soak_campaign.enabled is false — campaign run fails closed")
    if not paper_books_config.enabled or not paper_books_config.lifecycle.enabled:
        raise SoakCampaignError("paper books and lifecycle must be enabled for a campaign")
    if type(stop_on_blocker) is not bool:
        raise SoakCampaignError("stop_on_blocker must be a boolean")
    audit_clock = audit_clock or (lambda: datetime.now(timezone.utc))
    config_hash = campaign_config_hash(paper_books_config)
    existing = pb_repo.load_soak_campaign(conn, manifest.campaign_id)
    if existing:
        if existing["manifest_hash"] != manifest.manifest_hash or existing["config_hash"] != config_hash:
            raise SoakCampaignError("campaign_id already exists with a different manifest or configuration")
        return show_soak_campaign(conn, manifest.campaign_id)

    first_blocking_date = None
    first_blocking_status = None
    blocked = False
    for requested_day in manifest.dates:
        if blocked and stop_on_blocker:
            pb_repo.save_soak_campaign_day(conn, {
                "campaign_id": manifest.campaign_id, "as_of": requested_day.as_of,
                "requested_cycle_ids": requested_day.cycle_ids, "operator_run_id": None, "lifecycle_run_id": None,
                "cross_book_verification_id": None, "cross_book_verification_status": None,
                "controlled_readiness_status": first_blocking_status or "BLOCKED",
                "all_failed_checks": [], "failure_reasons": ["skipped after earlier hard blocker"],
                "day_status": DAY_SKIPPED, "created_at": audit_clock(),
            })
            continue
        try:
            day = run_controlled_soak_day(
                conn, as_of=requested_day.as_of, cycle_ids=requested_day.cycle_ids,
                paper_books_config=paper_books_config, shadow_config=shadow_config, audit_clock=audit_clock,
            )
            readiness = day["controlled_readiness"]
            all_failed = [c.name for c in readiness.checks if c.passed is False]
            hard = _hard_blocker(day)
            lifecycle_result = day["lifecycle_result"]
            warning_count = len(alerts_repo.list_alerts(conn, severity="WARNING", unresolved_only=True))
            if hard:
                day_status = DAY_BLOCKED
            elif warning_count > 0:
                day_status = DAY_COMPLETED_WARNINGS
            else:
                day_status = DAY_COMPLETED
            op = day["operator_run"]
            verification = day["verification"]
            pb_repo.save_soak_campaign_day(conn, {
                "campaign_id": manifest.campaign_id, "as_of": requested_day.as_of,
                "requested_cycle_ids": requested_day.cycle_ids,
                "operator_run_id": op["operator_run_id"] if op else None,
                "lifecycle_run_id": lifecycle_result.lifecycle_run_id if lifecycle_result else None,
                "cross_book_verification_id": verification.verification_id if verification else None,
                "cross_book_verification_status": verification.status if verification else None,
                "controlled_readiness_status": readiness.status, "all_failed_checks": all_failed,
                "failure_reasons": list(lifecycle_result.failure_reasons) if lifecycle_result else [day["block_reason"]],
                "day_status": day_status, "created_at": audit_clock(),
            })
            if hard:
                blocked = True
                first_blocking_date = first_blocking_date or requested_day.as_of.isoformat()
                first_blocking_status = first_blocking_status or readiness.status
        except Exception as exc:
            pb_repo.save_soak_campaign_day(conn, {
                "campaign_id": manifest.campaign_id, "as_of": requested_day.as_of,
                "requested_cycle_ids": requested_day.cycle_ids, "operator_run_id": None, "lifecycle_run_id": None,
                "cross_book_verification_id": None, "cross_book_verification_status": None,
                "controlled_readiness_status": "FAILED", "all_failed_checks": ["campaign_day_exception"],
                "failure_reasons": [str(exc)], "day_status": DAY_FAILED, "created_at": audit_clock(),
            })
            blocked = True
            first_blocking_date = first_blocking_date or requested_day.as_of.isoformat()
            first_blocking_status = first_blocking_status or "FAILED"

    review = build_activation_review(
        conn, manifest=manifest, config=paper_books_config, audit_clock=audit_clock,
    )
    if review["final_recommendation"] == RECOMMENDATION_BLOCKED:
        status = CAMPAIGN_BLOCKED
    elif review["final_recommendation"] == RECOMMENDATION_READY:
        status = CAMPAIGN_COMPLETED_READY
    else:
        status = CAMPAIGN_COMPLETED_NOT_READY
    pb_repo.save_soak_campaign(conn, {
        "campaign_id": manifest.campaign_id, "manifest_hash": manifest.manifest_hash, "config_hash": config_hash,
        "start_as_of": manifest.dates[0].as_of, "end_as_of": manifest.dates[-1].as_of,
        "requested_date_count": len(manifest.dates),
        "requested_cycle_count": sum(len(day.cycle_ids) for day in manifest.dates), "status": status,
        "first_blocking_date": first_blocking_date, "first_blocking_status": first_blocking_status,
        "created_at": audit_clock(),
    })
    return show_soak_campaign(conn, manifest.campaign_id)


def show_soak_campaign(conn, campaign_id: str) -> dict:
    campaign = pb_repo.load_soak_campaign(conn, campaign_id)
    if campaign is None:
        raise SoakCampaignError(f"unknown campaign_id {campaign_id!r}")
    return {
        "campaign": campaign, "days": pb_repo.list_soak_campaign_days(conn, campaign_id),
        "activation_review": pb_repo.load_soak_activation_review_for_campaign(conn, campaign_id),
    }
