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
from ..evaluation.market_calendar import (
    MARKET_TIMEZONE_NAME, is_trading_day, regular_session_close,
)
from ..research.provider_provenance import compute_real_provider_history
from ..shadow import pause as pause_mod
from ..shadow import readiness as shadow_readiness
from ..storage import paper_books_repositories as pb_repo
from ..storage import shadow_alerts_repositories as alerts_repo
from ..storage import shadow_operations_repositories as shadow_ops_repo
from ..utc import TimestampError, canonical_utc, canonical_utc_iso, parse_aware_utc
from zoneinfo import ZoneInfo
from . import lifecycle
from .config import PaperBooksConfiguration
from .controlled_soak_readiness import evaluate_controlled_soak_readiness
from .cross_book_verification import persist_verification, verification_is_stale, verify_cross_book_integrity

POLICY_VERSION = "paper-soak-campaign/v2"
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
DAY_RECOVERY_REQUIRES_REVIEW = "RECOVERY_REQUIRES_REVIEW"

ATTEMPT_RUNNING = "RUNNING"
ATTEMPT_FAILED = "FAILED"

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
    lifecycle_only: bool = False


@dataclass(frozen=True)
class SoakCampaignManifest:
    campaign_id: str
    dates: tuple[SoakCampaignDate, ...]
    manifest_hash: str


def _parse_aware_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise SoakCampaignError("manifest date as_of must be a non-empty ISO-8601 string")
    try:
        parsed = parse_aware_utc(value)
    except TimestampError as exc:
        if "timezone-aware" in str(exc):
            raise SoakCampaignError(f"manifest as_of {value!r} must be timezone-aware") from exc
        raise SoakCampaignError(f"invalid manifest as_of {value!r}") from exc
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
        unknown = set(item) - {"as_of", "cycle_ids", "lifecycle_only"}
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
        lifecycle_only = item.get("lifecycle_only", False)
        if type(lifecycle_only) is not bool:
            raise SoakCampaignError(f"manifest dates[{index}].lifecycle_only must be a boolean")
        if lifecycle_only and cycle_ids:
            raise SoakCampaignError(f"manifest dates[{index}] lifecycle-only dates require empty cycle_ids")
        as_of = _parse_aware_timestamp(item.get("as_of"))
        if previous is not None and as_of <= previous:
            raise SoakCampaignError("campaign dates must be strictly increasing; duplicate dates are rejected")
        previous = as_of
        local = as_of.astimezone(ZoneInfo(MARKET_TIMEZONE_NAME))
        if not is_trading_day(local.date()) and not lifecycle_only:
            raise SoakCampaignError(
                f"manifest dates[{index}] is not a trading day; set lifecycle_only=true with empty cycle_ids"
            )
        if not lifecycle_only and as_of < regular_session_close(local.date()).astimezone(timezone.utc):
            raise SoakCampaignError(
                f"manifest dates[{index}] is before the regular market close; same-day close is unavailable"
            )
        dates.append(SoakCampaignDate(
            as_of=as_of, cycle_ids=tuple(cycle_ids), lifecycle_only=lifecycle_only,
        ))

    canonical = {
        "campaign_id": campaign_id,
        "dates": [
            {"as_of": canonical_utc_iso(day.as_of), "cycle_ids": list(day.cycle_ids),
             "lifecycle_only": day.lifecycle_only}
            for day in dates
        ],
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
    as_of = canonical_utc(as_of)
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


def _review_scope_id(campaign_id: str, manifest_hash: str) -> str:
    digest = hashlib.sha256(f"{campaign_id}|{manifest_hash}".encode()).hexdigest()[:32]
    return f"pb-soak-review-scope-{digest}"


def _activation_review_id(
    scope_id: str, campaign_attempt_id: str, config_hash: str, evidence_state_hash: str,
) -> str:
    digest = hashlib.sha256(
        f"{scope_id}|{campaign_attempt_id}|{config_hash}|{evidence_state_hash}|{POLICY_VERSION}".encode()
    ).hexdigest()[:32]
    return f"pb-soak-review-{digest}"


def _at_or_before(value: str | None, cutoff: datetime) -> bool:
    if not value:
        return False
    try:
        return parse_aware_utc(value) <= cutoff
    except TimestampError:
        return False


def _effective_attempt_days(conn, campaign_id: str, selected_attempt_id: str | None) -> tuple[list[dict], dict | None]:
    attempts = pb_repo.list_soak_campaign_attempts(conn, campaign_id)
    if not attempts:
        return pb_repo.list_soak_campaign_days(conn, campaign_id), None
    if selected_attempt_id is None:
        selected = attempts[-1]
    else:
        selected = next((a for a in attempts if a["campaign_attempt_id"] == selected_attempt_id), None)
        if selected is None:
            raise SoakCampaignError(f"unknown campaign_attempt_id {selected_attempt_id!r}")
    effective: dict[str, dict] = {
        day["as_of"]: day for day in pb_repo.list_soak_campaign_days(conn, campaign_id)
    }
    for attempt in attempts:
        if attempt["attempt_number"] > selected["attempt_number"]:
            break
        for day in pb_repo.list_soak_campaign_attempt_days(conn, attempt["campaign_attempt_id"]):
            current = effective.get(day["as_of"])
            # A continuation skip never erases an earlier completed result.
            if current is None or day["day_status"] != DAY_SKIPPED:
                effective[day["as_of"]] = day
    return [effective[key] for key in sorted(effective, key=parse_aware_utc)], selected


def build_activation_review(
    conn, *, manifest: SoakCampaignManifest, config: PaperBooksConfiguration,
    audit_clock: Callable[[], datetime] | None = None, campaign_attempt_id: str | None = None,
) -> dict:
    audit_clock = audit_clock or (lambda: datetime.now(timezone.utc))
    days, selected_attempt = _effective_attempt_days(conn, manifest.campaign_id, campaign_attempt_id)
    start_as_of = canonical_utc(manifest.dates[0].as_of)
    end_as_of = canonical_utc(manifest.dates[-1].as_of)
    selected_attempt_id = selected_attempt["campaign_attempt_id"] if selected_attempt else "legacy-attempt"
    config_hash = campaign_config_hash(config)
    lifecycle_ids = [d["lifecycle_run_id"] for d in days if d.get("lifecycle_run_id")]
    lifecycle_runs = [pb_repo.load_lifecycle_run(conn, run_id) for run_id in lifecycle_ids]
    lifecycle_runs = [r for r in lifecycle_runs if r is not None]
    integrated_cycle_ids = sorted({cycle for run in lifecycle_runs for cycle in run["processed_cycle_ids"]})
    market_dates = {day.as_of.date() for day in manifest.dates if not day.lifecycle_only}
    completed_market_days = len({
        parse_aware_utc(r["as_of"]).date() for r in lifecycle_runs
        if parse_aware_utc(r["as_of"]).date() in market_dates
    })
    requested_cycle_ids = sorted({cycle for day in manifest.dates for cycle in day.cycle_ids})
    provenance = compute_real_provider_history(conn, end_as_of, cycle_ids=set(requested_cycle_ids))

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
    lifecycle_instants = {canonical_utc_iso(parse_aware_utc(r["as_of"])) for r in lifecycle_runs}
    performance: dict[str, object] = {
        "requested_cycle_ids": sorted({cycle for day in manifest.dates for cycle in day.cycle_ids}),
        "successfully_integrated_cycle_ids": integrated_cycle_ids,
        "requested_market_days": sum(not day.lifecycle_only for day in manifest.dates),
        "books": {},
    }
    for book_id in (config.baseline.book_id, config.enhanced.book_id):
        reconciliations = [
            r for r in pb_repo.list_reconciliations(conn, book_id)
            if canonical_utc_iso(parse_aware_utc(r["as_of"])) in lifecycle_instants
        ]
        reconciliation_history.extend({"book_id": book_id, "as_of": r["as_of"], "status": r["status"]} for r in reconciliations)
        campaign_snapshot_ids = {
            r.get("snapshot_ids", {}).get(book_id) for r in lifecycle_runs
            if r.get("snapshot_ids", {}).get(book_id)
        }
        snapshots = [s for s in pb_repo.list_snapshots(conn, book_id) if s["snapshot_id"] in campaign_snapshot_ids]
        valuation_history.extend({"book_id": book_id, "as_of": s["as_of"], "status": s["valuation_status"]} for s in snapshots)
        latest = snapshots[-1] if snapshots else None
        orders = [
            o for o in pb_repo.list_order_intents(conn, book_id)
            if _at_or_before(o["as_of"], end_as_of)
            and (o.get("cycle_id") in requested_cycle_ids
                 or canonical_utc_iso(parse_aware_utc(o["as_of"])) in lifecycle_instants)
        ]
        campaign_order_ids = {o["paper_order_intent_id"] for o in orders}
        fills = [
            f for f in pb_repo.list_fills(conn, book_id)
            if f["paper_order_intent_id"] in campaign_order_ids
            and _at_or_before(f["fill_timestamp"], end_as_of)
        ]
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
            "open_positions": sum(
                Decimal(p["quantity"]) > 0
                for p in (pb_repo.list_snapshot_positions(conn, book_id, latest["snapshot_id"]) if latest else [])
            ),
            "closed_trades": len([f for f in fills if f["side"] == "SELL"]),
            "pending_orders": len([o for o in orders if o["status"] == "PENDING_SUBMISSION"]),
            "maximum_drawdown": str(maximum_drawdown) if maximum_drawdown is not None else None,
        }

    all_alerts = [a for a in alerts_repo.list_alerts(conn) if _at_or_before(a.get("created_at"), end_as_of)]
    unresolved = [
        a for a in all_alerts
        if a.get("resolved_at") is None or not _at_or_before(a.get("resolved_at"), end_as_of)
    ]
    unresolved_warnings = [a for a in unresolved if a["severity"] == "WARNING"]
    alert_summary = {
        "unresolved_count": len(unresolved), "unresolved_warning_count": len(unresolved_warnings),
        "unresolved_critical_count": sum(a["severity"] == "CRITICAL" for a in unresolved),
        "resolved_critical_history": [
            {"alert_id": a["alert_id"], "resolved_at": a.get("resolved_at")} for a in all_alerts
            if a["severity"] == "CRITICAL" and a.get("resolved_at") is not None
            and _at_or_before(a.get("resolved_at"), end_as_of)
        ],
    }
    pause_history = [
        row for row in shadow_ops_repo.list_pause_state_history(conn)
        if _at_or_before(row.get("created_at"), end_as_of)
    ]
    cutoff_pause_state = pause_history[-1]["state"] if pause_history else "ACTIVE"
    pause_summary = {
        "current_state": cutoff_pause_state,
        "history": [
            {"state": row["state"], "reason": row["reason"], "changed_at": row["created_at"]}
            for row in pause_history
        ],
    }
    cost_rows = []
    if requested_cycle_ids:
        placeholders = ",".join("?" for _ in requested_cycle_ids)
        cost_rows = conn.execute(
            "SELECT s.cost_usd, s.created_at FROM shadow_run_summaries s JOIN shadow_scheduler_runs r "
            "ON r.scheduler_run_id = s.scheduler_run_id WHERE r.cycle_id IN (" + placeholders + ") "
            "AND s.cost_usd IS NOT NULL",
            requested_cycle_ids,
        ).fetchall()
        cost_rows = [row for row in cost_rows if _at_or_before(row["created_at"], end_as_of)]
    performance["estimated_model_cost_usd"] = str(sum((Decimal(r["cost_usd"]) for r in cost_rows), Decimal("0")))

    comparison = None
    for candidate in conn.execute(
        "SELECT comparison_id, window_start, window_end, created_at FROM paper_book_experiment_comparisons "
        "WHERE baseline_book_id = ? AND enhanced_book_id = ? ORDER BY window_end DESC, created_at DESC",
        (config.baseline.book_id, config.enhanced.book_id),
    ).fetchall():
        if (parse_aware_utc(candidate["window_start"]) == start_as_of
                and parse_aware_utc(candidate["window_end"]) == end_as_of
                and _at_or_before(candidate["created_at"], end_as_of)):
            comparison = candidate
            break
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
        promotion = next((row for row in conn.execute(
            "SELECT result, created_at FROM paper_book_promotion_evidence WHERE comparison_id = ? "
            "ORDER BY created_at DESC", (comparison_id,),
        ).fetchall() if _at_or_before(row["created_at"], end_as_of)), None)
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
        and provenance.qualifying_real_provider_cycle_count >= config.soak_campaign.minimum_successful_real_provider_cycles
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

    evidence = {
        "days": _serialize_check_history(days), "lifecycle_ids": lifecycle_ids,
        "integrated_cycle_ids": integrated_cycle_ids, "verification_history": verification_history,
        "reconciliation_history": reconciliation_history, "valuation_history": valuation_history,
        "alert_summary": alert_summary, "pause_summary": pause_summary, "performance": performance,
        "comparison_id": comparison_id, "promotion_status": promotion_status,
        "provider_qualifying_count": provenance.qualifying_real_provider_cycle_count,
    }
    evidence_state_hash = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    scope_id = _review_scope_id(manifest.campaign_id, manifest.manifest_hash)
    review_id = _activation_review_id(scope_id, selected_attempt_id, config_hash, evidence_state_hash)
    existing_review = pb_repo.load_soak_activation_review(conn, review_id)
    if existing_review is not None:
        return existing_review
    prior_reviews = pb_repo.list_soak_activation_reviews(conn, manifest.campaign_id)
    supersedes = prior_reviews[-1]["activation_review_id"] if prior_reviews else None
    record = {
        "activation_review_id": review_id, "activation_review_scope_id": scope_id,
        "campaign_attempt_id": selected_attempt["campaign_attempt_id"] if selected_attempt else None,
        "config_hash": config_hash, "evidence_state_hash": evidence_state_hash,
        "supersedes_activation_review_id": supersedes,
        "campaign_start_as_of": canonical_utc_iso(start_as_of),
        "campaign_end_as_of": canonical_utc_iso(end_as_of),
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
            "qualifying_real_provider_cycles": provenance.qualifying_real_provider_cycle_count,
        },
        "cross_book_verification_history": verification_history,
        "reconciliation_history": reconciliation_history, "valuation_history": valuation_history,
        "alert_summary": alert_summary, "pause_and_kill_summary": pause_summary,
        "performance_metrics": performance, "comparison_id": comparison_id,
        "promotion_evidence_status": promotion_status,
        "controlled_readiness_history": _serialize_check_history(days),
        "final_recommendation": recommendation, "reasons": reasons,
        "policy_version": POLICY_VERSION, "created_at": canonical_utc(audit_clock()),
    }
    pb_repo.save_soak_activation_review(conn, record)
    return pb_repo.load_soak_activation_review(conn, record["activation_review_id"])


def _attempt_id(campaign_id: str, attempt_number: int, manifest_hash: str, config_hash: str) -> str:
    digest = hashlib.sha256(
        f"{campaign_id}|{attempt_number}|{manifest_hash}|{config_hash}|{POLICY_VERSION}".encode()
    ).hexdigest()[:32]
    return f"pb-soak-attempt-{digest}"


def _attempt_day_record(
    attempt_id: str, campaign_id: str, day: SoakCampaignDate, *, controlled_status: str,
    day_status: str, audit_clock: Callable[[], datetime], operator_run_id: str | None = None,
    lifecycle_run_id: str | None = None, verification_id: str | None = None,
    verification_status: str | None = None, all_failed_checks: list[str] | None = None,
    failure_codes: list[str] | None = None, failure_reasons: list[str] | None = None,
) -> dict:
    return {
        "campaign_attempt_id": attempt_id, "campaign_id": campaign_id, "as_of": day.as_of,
        "requested_cycle_ids": day.cycle_ids, "lifecycle_only": day.lifecycle_only,
        "operator_run_id": operator_run_id, "lifecycle_run_id": lifecycle_run_id,
        "cross_book_verification_id": verification_id,
        "cross_book_verification_status": verification_status,
        "controlled_readiness_status": controlled_status,
        "all_failed_checks": all_failed_checks or [], "failure_codes": failure_codes or [],
        "failure_reasons": failure_reasons or [], "day_status": day_status,
        "created_at": canonical_utc(audit_clock()),
    }


def _save_legacy_day(conn, record: dict) -> None:
    """Dual-write attempt one for Milestone 9.3 readers; never rewritten."""
    pb_repo.save_soak_campaign_day(conn, {
        "campaign_id": record["campaign_id"], "as_of": record["as_of"],
        "requested_cycle_ids": record["requested_cycle_ids"], "operator_run_id": record.get("operator_run_id"),
        "lifecycle_run_id": record.get("lifecycle_run_id"),
        "cross_book_verification_id": record.get("cross_book_verification_id"),
        "cross_book_verification_status": record.get("cross_book_verification_status"),
        "controlled_readiness_status": record["controlled_readiness_status"],
        "all_failed_checks": record.get("all_failed_checks", []),
        "failure_reasons": record.get("failure_reasons", []), "day_status": record["day_status"],
        "created_at": record["created_at"],
    })


def manifest_from_persisted_campaign(conn, campaign_id: str) -> SoakCampaignManifest:
    campaign = pb_repo.load_soak_campaign(conn, campaign_id)
    if campaign is None:
        raise SoakCampaignError(f"unknown campaign_id {campaign_id!r}")
    rows = pb_repo.list_soak_campaign_definition_dates(conn, campaign_id)
    attempts = pb_repo.list_soak_campaign_attempts(conn, campaign_id)
    if not rows:
        rows = (
            pb_repo.list_soak_campaign_attempt_days(conn, attempts[0]["campaign_attempt_id"])
            if attempts else pb_repo.list_soak_campaign_days(conn, campaign_id)
        )
    if not rows:
        raise SoakCampaignError(f"campaign {campaign_id!r} has no persisted manifest dates")
    dates = tuple(
        SoakCampaignDate(
            as_of=parse_aware_utc(row["as_of"]), cycle_ids=tuple(row["requested_cycle_ids"]),
            lifecycle_only=bool(row.get("lifecycle_only", False)),
        ) for row in rows
    )
    return SoakCampaignManifest(campaign_id=campaign_id, dates=dates, manifest_hash=campaign["manifest_hash"])


def run_soak_campaign(
    conn, *, manifest: SoakCampaignManifest, paper_books_config: PaperBooksConfiguration, shadow_config,
    stop_on_blocker: bool = True, audit_clock: Callable[[], datetime] | None = None,
    continuation_operator: str | None = None, continuation_reason: str | None = None,
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
    else:
        now = canonical_utc(audit_clock())
        pb_repo.save_soak_campaign(conn, {
            "campaign_id": manifest.campaign_id, "manifest_hash": manifest.manifest_hash, "config_hash": config_hash,
            "start_as_of": manifest.dates[0].as_of, "end_as_of": manifest.dates[-1].as_of,
            "requested_date_count": len(manifest.dates),
            "requested_cycle_count": sum(len(day.cycle_ids) for day in manifest.dates), "status": "DEFINED",
            "first_blocking_date": None, "first_blocking_status": None, "created_at": now,
        })
        for requested in manifest.dates:
            pb_repo.save_soak_campaign_definition_date(conn, {
                "campaign_id": manifest.campaign_id, "as_of": requested.as_of,
                "requested_cycle_ids": requested.cycle_ids, "lifecycle_only": requested.lifecycle_only,
                "created_at": now,
            })

    attempts = pb_repo.list_soak_campaign_attempts(conn, manifest.campaign_id)
    running = next((item for item in attempts if item["status"] == ATTEMPT_RUNNING), None)
    continuation = not stop_on_blocker
    if running:
        attempt = running
    else:
        if attempts and not continuation:
            return show_soak_campaign(conn, manifest.campaign_id)
        if not attempts and existing and existing["status"] != "DEFINED" and not continuation:
            return show_soak_campaign(conn, manifest.campaign_id)  # legacy completed campaign
        if continuation and (attempts or (existing and existing["status"] != "DEFINED")):
            if not continuation_operator or not continuation_operator.strip():
                raise SoakCampaignError("campaign continuation requires a non-empty operator")
            if not continuation_reason or not continuation_reason.strip():
                raise SoakCampaignError("campaign continuation requires a non-empty reason")
            if len(continuation_operator) > MAX_IDENTIFIER_LENGTH or len(continuation_reason) > 500:
                raise SoakCampaignError("campaign continuation operator/reason exceeds bounded storage limits")
            if attempts:
                latest = attempts[-1]
                if (latest["continue_after_blocker"] and latest.get("operator") == continuation_operator
                        and latest.get("reason") == continuation_reason):
                    return show_soak_campaign(conn, manifest.campaign_id)
        legacy_days = pb_repo.list_soak_campaign_days(conn, manifest.campaign_id)
        number = (attempts[-1]["attempt_number"] + 1) if attempts else (2 if legacy_days else 1)
        attempt_id = _attempt_id(manifest.campaign_id, number, manifest.manifest_hash, config_hash)
        now = canonical_utc(audit_clock())
        pb_repo.save_soak_campaign_attempt(conn, {
            "campaign_attempt_id": attempt_id, "campaign_id": manifest.campaign_id,
            "manifest_hash": manifest.manifest_hash, "config_hash": config_hash,
            "previous_attempt_id": attempts[-1]["campaign_attempt_id"] if attempts else None,
            "attempt_number": number, "continue_after_blocker": bool(attempts or legacy_days) and continuation,
            "status": ATTEMPT_RUNNING, "started_at": now, "created_at": now,
            "operator": continuation_operator, "reason": continuation_reason,
        })
        attempt = pb_repo.load_soak_campaign_attempt(conn, attempt_id)

    attempt_id = attempt["campaign_attempt_id"]
    current = {d["as_of"]: d for d in pb_repo.list_soak_campaign_attempt_days(conn, attempt_id)}
    effective, _ = _effective_attempt_days(conn, manifest.campaign_id, attempt_id)
    prior = {d["as_of"]: d for d in effective if d.get("campaign_attempt_id") != attempt_id}
    blocked = any(d["day_status"] in (DAY_BLOCKED, DAY_FAILED, DAY_RECOVERY_REQUIRES_REVIEW) for d in current.values())
    first_date = attempt.get("first_blocking_date")
    first_status = attempt.get("first_blocking_status")
    stop_this_attempt = stop_on_blocker if attempt["attempt_number"] == 1 else paper_books_config.soak_campaign.stop_on_blocker

    from .controlled_soak_readiness import ControlledSoakReadinessError
    from .cross_book_verification import CrossBookVerificationError
    known_errors = (SoakCampaignError, lifecycle.LifecycleError, ControlledSoakReadinessError, CrossBookVerificationError)

    for requested in manifest.dates:
        key = canonical_utc_iso(requested.as_of)
        if key in current:
            continue
        old = prior.get(key)
        if old and old["day_status"] in (DAY_COMPLETED, DAY_COMPLETED_WARNINGS):
            continue
        if old and old["day_status"] == DAY_FAILED:
            blocked = True
            record = _attempt_day_record(
                attempt_id, manifest.campaign_id, requested, controlled_status=DAY_RECOVERY_REQUIRES_REVIEW,
                day_status=DAY_RECOVERY_REQUIRES_REVIEW, failure_codes=["PRIOR_FAILURE_NOT_RETRY_SAFE"],
                failure_reasons=["prior failed date requires explicit operator review"], audit_clock=audit_clock,
            )
            pb_repo.save_soak_campaign_attempt_day(conn, record)
            continue
        if blocked and stop_this_attempt:
            record = _attempt_day_record(
                attempt_id, manifest.campaign_id, requested, controlled_status=first_status or "BLOCKED",
                day_status=DAY_SKIPPED, failure_codes=["SKIPPED_AFTER_BLOCKER"],
                failure_reasons=["skipped after earlier hard blocker"], audit_clock=audit_clock,
            )
            pb_repo.save_soak_campaign_attempt_day(conn, record)
            if attempt["attempt_number"] == 1:
                _save_legacy_day(conn, record)
            continue

        op_id = operator_run_id(requested.as_of, requested.cycle_ids)
        persisted_op = pb_repo.load_operator_run(conn, op_id)
        lifecycle_id = lifecycle._lifecycle_run_id(requested.as_of, paper_books_config.config_hash)
        persisted_lifecycle = pb_repo.load_lifecycle_run(conn, lifecycle_id)
        if persisted_op and old is None:
            status = persisted_op["controlled_readiness_status"]
            hard = status in {
                "NOT_READY_SHADOW_PAUSED", "NOT_READY_SHADOW_KILLED", "NOT_READY_HEALTH_UNEXPLAINED",
                "NOT_READY_CRITICAL_ALERTS", "NOT_READY_RECONCILIATION", "NOT_READY_VALUATION",
                "NOT_READY_CROSS_BOOK",
            } or bool(persisted_op["failure_reasons"])
            record = _attempt_day_record(
                attempt_id, manifest.campaign_id, requested, controlled_status=status,
                day_status=DAY_BLOCKED if hard else DAY_COMPLETED, operator_run_id=op_id,
                lifecycle_run_id=persisted_op["lifecycle_run_id"],
                verification_id=persisted_op.get("cross_book_verification_id"),
                verification_status=persisted_op.get("cross_book_verification_status"),
                failure_codes=["RECOVERED_FROM_OPERATOR_EVIDENCE"],
                failure_reasons=list(persisted_op["failure_reasons"]), audit_clock=audit_clock,
            )
            pb_repo.save_soak_campaign_attempt_day(conn, record)
            if hard:
                blocked, first_date, first_status = True, first_date or key, first_status or status
            continue
        if persisted_lifecycle and old is None:
            record = _attempt_day_record(
                attempt_id, manifest.campaign_id, requested, controlled_status=DAY_RECOVERY_REQUIRES_REVIEW,
                day_status=DAY_RECOVERY_REQUIRES_REVIEW, lifecycle_run_id=lifecycle_id,
                failure_codes=["INCOMPLETE_STAGE_EVIDENCE"],
                failure_reasons=["lifecycle persisted without complete operator evidence; mutation was not rerun"],
                audit_clock=audit_clock,
            )
            pb_repo.save_soak_campaign_attempt_day(conn, record)
            blocked, first_date, first_status = True, first_date or key, first_status or DAY_RECOVERY_REQUIRES_REVIEW
            continue
        try:
            result = run_controlled_soak_day(
                conn, as_of=requested.as_of, cycle_ids=requested.cycle_ids,
                paper_books_config=paper_books_config, shadow_config=shadow_config, audit_clock=audit_clock,
            )
            readiness = result["controlled_readiness"]
            lifecycle_result = result["lifecycle_result"]
            verification = result["verification"]
            hard = _hard_blocker(result)
            warnings = [
                alert for alert in alerts_repo.list_alerts(conn, severity="WARNING")
                if _at_or_before(alert.get("created_at"), requested.as_of)
                and (not alert.get("resolved_at") or not _at_or_before(alert["resolved_at"], requested.as_of))
            ]
            record = _attempt_day_record(
                attempt_id, manifest.campaign_id, requested, controlled_status=readiness.status,
                day_status=DAY_BLOCKED if hard else (DAY_COMPLETED_WARNINGS if warnings else DAY_COMPLETED),
                operator_run_id=result["operator_run"]["operator_run_id"] if result["operator_run"] else None,
                lifecycle_run_id=lifecycle_result.lifecycle_run_id if lifecycle_result else None,
                verification_id=verification.verification_id if verification else None,
                verification_status=verification.status if verification else None,
                all_failed_checks=[c.name for c in readiness.checks if c.passed is False],
                failure_reasons=list(lifecycle_result.failure_reasons) if lifecycle_result else [result["block_reason"]],
                audit_clock=audit_clock,
            )
            pb_repo.save_soak_campaign_attempt_day(conn, record)
            if attempt["attempt_number"] == 1:
                _save_legacy_day(conn, record)
            if hard:
                blocked, first_date, first_status = True, first_date or key, first_status or readiness.status
        except known_errors as exc:
            code = f"DOMAIN_{type(exc).__name__.upper()}"[:80]
            record = _attempt_day_record(
                attempt_id, manifest.campaign_id, requested, controlled_status="FAILED", day_status=DAY_FAILED,
                all_failed_checks=["campaign_day_domain_error"], failure_codes=[code],
                failure_reasons=["known campaign domain operation failed"], audit_clock=audit_clock,
            )
            pb_repo.save_soak_campaign_attempt_day(conn, record)
            if attempt["attempt_number"] == 1:
                _save_legacy_day(conn, record)
            blocked, first_date, first_status = True, first_date or key, first_status or "FAILED"
        except Exception as exc:
            conn.rollback()
            code = f"UNEXPECTED_{type(exc).__name__.upper()}"[:80]
            pb_repo.finalize_soak_campaign_attempt(conn, attempt_id, {
                "status": ATTEMPT_FAILED, "completed_at": canonical_utc(audit_clock()),
                "first_blocking_date": first_date or key, "first_blocking_status": first_status or "FAILED",
                "failure_code": code, "failure_stage": "DAY_PROCESSING",
                "sanitized_message": "unexpected campaign day failure",
            })
            raise SoakCampaignError(f"campaign attempt failed unexpectedly ({code})") from exc

    effective, _ = _effective_attempt_days(conn, manifest.campaign_id, attempt_id)
    blocked = any(d["day_status"] in (DAY_BLOCKED, DAY_FAILED, DAY_RECOVERY_REQUIRES_REVIEW) for d in effective)
    pb_repo.finalize_soak_campaign_attempt(conn, attempt_id, {
        "status": CAMPAIGN_BLOCKED if blocked else CAMPAIGN_COMPLETED_NOT_READY,
        "completed_at": canonical_utc(audit_clock()), "first_blocking_date": first_date,
        "first_blocking_status": first_status,
    })
    review = build_activation_review(
        conn, manifest=manifest, config=paper_books_config, audit_clock=audit_clock,
        campaign_attempt_id=attempt_id,
    )
    if review["final_recommendation"] == RECOMMENDATION_READY:
        conn.execute(
            "UPDATE paper_soak_campaign_attempts SET status = ? WHERE campaign_attempt_id = ? AND status = ?",
            (CAMPAIGN_COMPLETED_READY, attempt_id, CAMPAIGN_COMPLETED_NOT_READY),
        )
        conn.commit()
    return show_soak_campaign(conn, manifest.campaign_id)


def show_soak_campaign(conn, campaign_id: str) -> dict:
    campaign = pb_repo.load_soak_campaign(conn, campaign_id)
    if campaign is None:
        raise SoakCampaignError(f"unknown campaign_id {campaign_id!r}")
    attempts = pb_repo.list_soak_campaign_attempts(conn, campaign_id)
    rendered_attempts = []
    for attempt in attempts:
        item = dict(attempt)
        item["continue_after_blocker"] = bool(item["continue_after_blocker"])
        item["days"] = pb_repo.list_soak_campaign_attempt_days(conn, item["campaign_attempt_id"])
        rendered_attempts.append(item)
    effective, _ = _effective_attempt_days(conn, campaign_id, None)
    campaign_view = dict(campaign)
    if attempts:
        campaign_view.update({
            "status": attempts[-1]["status"],
            "first_blocking_date": attempts[-1].get("first_blocking_date"),
            "first_blocking_status": attempts[-1].get("first_blocking_status"),
        })
    reviews = pb_repo.list_soak_activation_reviews(conn, campaign_id)
    return {
        "campaign": campaign_view, "attempts": rendered_attempts,
        "latest_attempt_id": attempts[-1]["campaign_attempt_id"] if attempts else None,
        "days": effective, "activation_reviews": reviews,
        "latest_activation_review_id": reviews[-1]["activation_review_id"] if reviews else None,
        "activation_review": reviews[-1] if reviews else None,
    }
