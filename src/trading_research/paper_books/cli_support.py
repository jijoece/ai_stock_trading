"""Business logic backing the `paper-book-*`/`paper-experiment-compare`/
`paper-promotion-status` CLI commands (docs/milestone-8.md Step 22).

Every function here returns a plain JSON-serializable dict (sanitized —
never a raw Claude prompt/response, never a credential) so `cli.py` can
`json.dumps(..., default=str)` it directly. Mutating commands (snapshot,
reconcile, run-cycle) fail closed with an `"error"` key when
`paper_books.enabled` is `false` or the requested book is not configured —
they never silently proceed.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ..research import experiment_policy as ep
from ..shadow import pause as pause_mod
from ..storage import paper_books_repositories as pb_repo
from ..storage.database import session
from . import cash_ledger, comparison as comparison_module, execution, lifecycle, order_intent, promotion_evidence, reconciliation, risk as risk_module, valuation
from .config import PaperBooksConfigError, load_paper_books_config
from .exit_policy import EXIT_DECISIONS
from .models import VALUATION_COMPLETE
from .scheduled_integration import ScheduledIntegrationError, integrate_scheduled_cycle_into_paper_books


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_config_or_error():
    try:
        return load_paper_books_config(), None
    except PaperBooksConfigError as exc:
        return None, str(exc)


def paper_book_list_cli(db_path: Path) -> dict:
    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    with session(db_path) as conn:
        books = pb_repo.list_books(conn)
        return {
            "paper_books_enabled": cfg.enabled,
            "configured_books": {
                "baseline": {"book_id": cfg.baseline.book_id, "enabled": cfg.is_book_enabled(cfg.baseline.book_id)},
                "enhanced": {"book_id": cfg.enhanced.book_id, "enabled": cfg.is_book_enabled(cfg.enhanced.book_id)},
            },
            "opened_books": [
                {"book_id": b.book_id, "experiment_arm": b.experiment_arm, "status": b.status,
                 "starting_cash_usd": str(b.starting_cash_usd)}
                for b in books
            ],
        }


def paper_book_show_cli(db_path: Path, book_id: str) -> dict:
    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    if book_id not in (cfg.baseline.book_id, cfg.enhanced.book_id):
        return {"error": f"unknown book_id {book_id!r} — fails closed"}
    with session(db_path) as conn:
        book = pb_repo.load_book(conn, book_id)
        if book is None:
            return {"error": f"book {book_id!r} has not been opened yet — fails closed"}
        return {
            "book_id": book.book_id, "experiment_arm": book.experiment_arm, "status": book.status,
            "starting_cash_usd": str(book.starting_cash_usd),
            "available_cash_usd": str(cash_ledger.available_cash(conn, book_id)),
            "reserved_cash_usd": str(cash_ledger.reserved_cash(conn, book_id)),
            "positions": [
                {k: (str(v) if not isinstance(v, (str, int, type(None))) else v) for k, v in p.items()}
                for p in pb_repo.list_positions(conn, book_id)
            ],
        }


def paper_book_snapshot_cli(db_path: Path, book_id: str, as_of: datetime) -> dict:
    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    if not cfg.enabled:
        return {"error": "paper_books.enabled is false — mutating commands require paper configuration enabled"}
    if book_id not in (cfg.baseline.book_id, cfg.enhanced.book_id):
        return {"error": f"unknown book_id {book_id!r} — fails closed"}
    if not cfg.is_book_enabled(book_id):
        return {"error": f"book {book_id!r} is not enabled in config/paper_books.yaml — fails closed"}
    with session(db_path) as conn:
        if pb_repo.load_book(conn, book_id) is None:
            return {"error": f"book {book_id!r} has not been opened yet — fails closed"}
        snap = valuation.build_portfolio_snapshot(
            conn, book_id, as_of, maximum_price_age_seconds=cfg.valuation.maximum_price_age_seconds,
        )
        return {
            "snapshot_id": snap.snapshot_id, "book_id": snap.book_id, "as_of": snap.as_of.isoformat(),
            "valuation_status": snap.valuation_status,
            "cash_available_usd": str(snap.cash_available_usd),
            "cash_reserved_usd": str(snap.cash_reserved_usd),
            "net_liquidation_value_usd": str(snap.net_liquidation_value_usd) if snap.net_liquidation_value_usd is not None else None,
            "unrealized_pnl_usd": str(snap.unrealized_pnl_usd) if snap.unrealized_pnl_usd is not None else None,
            "realized_pnl_usd": str(snap.realized_pnl_usd),
            "position_count": snap.position_count, "unvalued_position_count": snap.unvalued_position_count,
            "stale_position_count": snap.stale_position_count,
        }


def paper_book_reconcile_cli(db_path: Path, book_id: str, as_of: datetime | None = None) -> dict:
    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    if not cfg.enabled:
        return {"error": "paper_books.enabled is false — mutating commands require paper configuration enabled"}
    if book_id not in (cfg.baseline.book_id, cfg.enhanced.book_id):
        return {"error": f"unknown book_id {book_id!r} — fails closed"}
    with session(db_path) as conn:
        try:
            return reconciliation.reconcile_book(conn, book_id, as_of or _utc_now())
        except ValueError as exc:
            return {"error": str(exc)}


def paper_book_run_cycle_cli(
    db_path: Path, *, cycle_id: str, experiment_policy: str, symbol: str, quantity_hint: str,
    reference_price: str, bid: str, ask: str, recommendation_id_baseline: str | None = None,
    recommendation_id_enhanced: str | None = None,
) -> dict:
    """Fixture-mode, single-symbol, book-aware cycle: builds a snapshot,
    evaluates deterministic risk, builds/persists an order intent, and
    submits it to the local-simulated fill engine for every book the
    experiment policy (and its own explicit enablement) allows. Never
    invokes Claude, never fetches real evidence — this is the offline,
    provider-mode=fixture path only."""
    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    if not cfg.enabled:
        return {"error": "paper_books.enabled is false — mutating commands require paper configuration enabled"}

    baseline_enabled = cfg.is_book_enabled(cfg.baseline.book_id)
    enhanced_enabled = cfg.is_book_enabled(cfg.enhanced.book_id)
    try:
        may_baseline = ep.may_submit_baseline_to_paper_book(
            experiment_policy, baseline_book_enabled=baseline_enabled, enhanced_book_enabled=enhanced_enabled,
        )
        may_enhanced = ep.may_submit_enhanced_to_paper_book(
            experiment_policy, baseline_book_enabled=baseline_enabled, enhanced_book_enabled=enhanced_enabled,
        )
    except (ep.UnknownExperimentPolicyError, ep.UnsupportedExperimentPolicyError) as exc:
        return {"error": str(exc)}

    now = _utc_now()
    plan = []
    if may_baseline and recommendation_id_baseline:
        plan.append(("BASELINE", recommendation_id_baseline))
    if may_enhanced and recommendation_id_enhanced:
        plan.append(("ENHANCED", recommendation_id_enhanced))
    if not plan:
        return {
            "error": "no book is both policy-permitted and supplied a recommendation_id — nothing to submit",
            "may_submit_baseline": may_baseline, "may_submit_enhanced": may_enhanced,
        }

    results = {}
    with session(db_path) as conn:
        for book_id, recommendation_id in plan:
            book_def = cfg.book(book_id)
            book = cash_ledger.open_book(
                conn, book_id=book_id, starting_cash_usd=book_def.starting_cash_usd,
                config_hash=cfg.config_hash, clock=lambda: now,
            )
            snap = valuation.build_portfolio_snapshot(
                conn, book_id, now, maximum_price_age_seconds=cfg.valuation.maximum_price_age_seconds,
            )
            context = risk_module.build_portfolio_context(conn, book_id, now, snap, symbol, Decimal("0"))
            decision = risk_module.evaluate_paper_risk(
                book_status=book.status, experiment_arm=book.experiment_arm, expected_arm=book.experiment_arm,
                context=context, requested_quantity_hint=Decimal(quantity_hint),
                reference_price=Decimal(reference_price), reference_price_age_seconds=0,
                reference_price_point_in_time_safe=True, risk_config=cfg.risk,
            )
            risk_decision_id = order_intent.persist_risk_decision(
                conn, book_id, cycle_id, recommendation_id, symbol, decision, snap.snapshot_id, lambda: now,
            )
            intent = order_intent.build_order_intent(
                book_id=book_id, experiment_arm=book.experiment_arm, cycle_id=cycle_id,
                recommendation_id=recommendation_id, symbol=symbol, risk_decision=decision,
                risk_decision_id=risk_decision_id, portfolio_snapshot_id=snap.snapshot_id,
                config_hash=cfg.config_hash, as_of=now, clock=lambda: now,
            )
            if intent is None:
                results[book_id] = {"risk_decision": decision.decision, "reasons": list(decision.reasons), "intent": None}
                continue
            market = execution.MarketSimulationInput(bid=Decimal(bid), ask=Decimal(ask))
            outcome = execution.submit_and_simulate(conn, intent, market, now)
            results[book_id] = {
                "risk_decision": decision.decision, "paper_order_intent_id": intent.paper_order_intent_id,
                "status": outcome["status"],
                "fill": {k: str(v) if not isinstance(v, (str, int)) else v for k, v in outcome["fill"].items()} if outcome["fill"] else None,
            }
    return {"cycle_id": cycle_id, "experiment_policy": experiment_policy, "symbol": symbol, "results": results}


def paper_experiment_compare_cli(
    db_path: Path, *, experiment_id: str, window_start: datetime, window_end: datetime,
    min_comparable_cycles: int = 1,
) -> dict:
    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    with session(db_path) as conn:
        cmp = comparison_module.build_comparison(
            conn, experiment_id, cfg.baseline.book_id, cfg.enhanced.book_id, window_start, window_end,
            min_comparable_cycles=min_comparable_cycles, clock=_utc_now,
        )
        return {
            "comparison_id": cmp.comparison_id, "experiment_id": cmp.experiment_id,
            "baseline_book_id": cmp.baseline_book_id, "enhanced_book_id": cmp.enhanced_book_id,
            "comparable": cmp.comparable, "comparability_reasons": list(cmp.comparability_reasons),
            "metric_deltas": {k: (str(v) if v is not None else None) for k, v in cmp.metric_deltas.items()},
        }


def paper_promotion_status_cli(
    db_path: Path, *, experiment_id: str, min_comparable_cycles: int = 1, min_trading_days: int = 1,
    min_closed_trades: int = 1, operational_health_ok: bool = True, reconciliation_ok: bool = True,
) -> dict:
    with session(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM paper_book_experiment_comparisons WHERE experiment_id = ? ORDER BY created_at DESC LIMIT 1",
            (experiment_id,),
        ).fetchone()
        if row is None:
            return {"error": f"no comparison exists yet for experiment_id {experiment_id!r} — run paper-experiment-compare first"}
        cmp_dict = pb_repo.load_experiment_comparison(conn, row["comparison_id"])
        cmp = comparison_module.PaperExperimentComparison(
            comparison_id=cmp_dict["comparison_id"], experiment_id=cmp_dict["experiment_id"],
            baseline_book_id=cmp_dict["baseline_book_id"], enhanced_book_id=cmp_dict["enhanced_book_id"],
            window_start=datetime.fromisoformat(cmp_dict["window_start"]), window_end=datetime.fromisoformat(cmp_dict["window_end"]),
            baseline_metrics_id=cmp_dict["baseline_metrics_id"], enhanced_metrics_id=cmp_dict["enhanced_metrics_id"],
            comparable=bool(cmp_dict["comparable"]), comparability_reasons=cmp_dict["comparability_reasons"],
            metric_deltas=cmp_dict["metric_deltas"],
        )
        enhanced_metrics = pb_repo.load_daily_metrics(conn, cmp.enhanced_book_id, cmp.enhanced_metrics_id)["metrics"]
        assignments = pb_repo.list_experiment_assignments(conn, experiment_id)
        cycle_count = len({a["cycle_id"] for a in assignments})

        result, reasons = promotion_evidence.evaluate_promotion_evidence(
            cmp, enhanced_metrics, cycle_count=cycle_count, min_comparable_cycles=min_comparable_cycles,
            min_trading_days=min_trading_days, min_closed_trades=min_closed_trades,
            operational_health_ok=operational_health_ok, reconciliation_ok=reconciliation_ok,
        )
        promotion_evidence_id = promotion_evidence.save_promotion_evidence(conn, cmp, result, reasons, clock=_utc_now)
        return {
            "experiment_id": experiment_id, "comparison_id": cmp.comparison_id,
            "promotion_evidence_id": promotion_evidence_id, "result": result, "reasons": list(reasons),
        }


def paper_book_integrate_cycle_cli(db_path: Path, *, cycle_id: str, experiment_policy: str) -> dict:
    """`paper-book-integrate-cycle` (docs/milestone-8.1.md Step 11): loads an
    ACTUAL persisted scheduled-research-cycle (never a fixture recommendation)
    and drives it through the isolated paper books. Fails closed with an
    `"error"` key + non-zero exit whenever `paper_books.enabled` or
    `paper_books.scheduled_integration.enabled` is false, or the cycle_id is
    unknown. Returns sanitized, deterministic, structured JSON only — no raw
    Claude prompt/response content ever appears here (this function never
    touches `research_committee_reports`/model request-response tables)."""
    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    if not cfg.enabled:
        return {"error": "paper_books.enabled is false — scheduled integration requires paper configuration enabled"}
    if not cfg.scheduled_integration.enabled:
        return {"error": "paper_books.scheduled_integration.enabled is false — scheduled integration fails closed"}

    with session(db_path) as conn:
        try:
            result = integrate_scheduled_cycle_into_paper_books(
                conn, cycle_id=cycle_id, experiment_policy=experiment_policy, paper_books_config=cfg,
                clock=_utc_now,
            )
        except ScheduledIntegrationError as exc:
            return {"error": str(exc)}

        return {
            "cycle_id": result.cycle_id, "experiment_policy": result.experiment_policy,
            "as_of": result.as_of.isoformat(),
            "symbol_outcomes": [
                {
                    "symbol": o.symbol, "arm": o.arm, "book_id": o.book_id, "recommendation_id": o.recommendation_id,
                    "outcome": o.outcome, "reasons": list(o.reasons), "risk_decision_id": o.risk_decision_id,
                    "paper_order_intent_id": o.paper_order_intent_id, "fill_id": o.fill_id,
                    "market_simulation_input_source": o.market_simulation_input_source,
                }
                for o in result.symbol_outcomes
            ],
            "reconciliations": {
                book_id: {"reconciliation_id": r["reconciliation_id"], "status": r["status"], "mismatch_count": len(r["mismatches"])}
                for book_id, r in result.reconciliations.items()
            },
        }


def paper_book_lifecycle_run_cli(
    db_path: Path, *, as_of: datetime, integrate_cycle_ids: tuple[str, ...] = (), audit_time_now: bool = False,
) -> dict:
    """`paper-book-lifecycle-run` (docs/milestone-9.md Section 11; clock
    semantics corrected in Milestone 9.1 Section 5). Fails closed with an
    `"error"` key + non-zero exit whenever `paper_books.enabled` or
    `paper_books.lifecycle.enabled` is false. Returns sanitized,
    deterministic JSON only.

    By default (`audit_time_now=False`), no explicit `clock` is passed to
    `run_paper_book_lifecycle` — its own default anchors every timestamp it
    stamps (including order/decision `created_at`) to `as_of`, never
    wall-clock `now()`. This CLI previously always injected wall-clock time,
    which silently corrupts a LATER lifecycle run's own
    `market_days_held(created_at, as_of)` calculation whenever `--as-of` is a
    historical replay date (an order created "now" reads as created in the
    future relative to a subsequent historical `as_of`). `--audit-time-now`
    opts back into a real "actually invoked at" audit timestamp for a human
    operator whose `--as-of` is close to today — it affects only
    `created_at` audit metadata, never market-day calculations, order
    eligibility, price selection, holding-period calculation, snapshot
    `as_of`, or exit-decision effective date, all of which remain keyed to
    `as_of` unconditionally inside `run_paper_book_lifecycle` itself."""
    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    if not cfg.enabled:
        return {"error": "paper_books.enabled is false — lifecycle run fails closed"}
    if not cfg.lifecycle.enabled:
        return {"error": "paper_books.lifecycle.enabled is false — lifecycle run fails closed"}

    with session(db_path) as conn:
        try:
            result = lifecycle.run_paper_book_lifecycle(
                conn, as_of=as_of, paper_books_config=cfg, integrate_cycle_ids=tuple(integrate_cycle_ids),
                clock=_utc_now if audit_time_now else None,
            )
        except lifecycle.LifecycleError as exc:
            return {"error": str(exc)}

        return _lifecycle_result_to_json(result)


def _lifecycle_result_to_json(result) -> dict:
    return {
        "lifecycle_run_id": result.lifecycle_run_id, "as_of": result.as_of.isoformat(),
        "processed_cycle_ids": list(result.processed_cycle_ids), "books_processed": list(result.books_processed),
        "pending_orders_filled": result.pending_orders_filled,
        "pending_orders_expired": result.pending_orders_expired,
        "exit_decisions": list(result.exit_decisions), "exit_orders_created": result.exit_orders_created,
        "exit_orders_filled": result.exit_orders_filled, "snapshot_ids": dict(result.snapshot_ids),
        "reconciliation_statuses": dict(result.reconciliation_statuses), "metrics_ids": dict(result.metrics_ids),
        "failure_reasons": list(result.failure_reasons),
    }


def paper_book_exit_request_cli(
    db_path: Path, *, book_id: str, symbol: str, operator: str, reason: str,
    requested_at: datetime | None = None, idempotency_key: str | None = None,
) -> dict:
    """`paper-book-exit-request` (docs/milestone-9.md Section 11): creates an
    explicit, audited manual exit request. Never mutates a position or
    submits an order directly — the request is only consumed the next time
    `paper-book-lifecycle-run` evaluates exits for this book/symbol
    (docs/milestone-9.md Section 3 "Manual exit")."""
    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    if not cfg.enabled:
        return {"error": "paper_books.enabled is false — manual exit request fails closed"}
    if book_id not in (cfg.baseline.book_id, cfg.enhanced.book_id):
        return {"error": f"unknown book_id {book_id!r} — fails closed"}
    if not symbol:
        return {"error": "symbol is required"}
    if not operator:
        return {"error": "operator is required for an audited manual exit request"}
    if not reason:
        return {"error": "reason is required for an audited manual exit request"}

    now = _utc_now()
    requested_at = requested_at or now
    idempotency_key = idempotency_key or hashlib.sha256(
        f"{book_id}:{symbol}:{operator}:{reason}:{requested_at.isoformat()}".encode()
    ).hexdigest()[:32]
    manual_exit_request_id = f"pb-manual-exit-{hashlib.sha256(f'{book_id}:{idempotency_key}'.encode()).hexdigest()[:32]}"

    with session(db_path) as conn:
        if pb_repo.load_book(conn, book_id) is None:
            return {"error": f"book {book_id!r} has not been opened yet — fails closed"}
        created = pb_repo.save_manual_exit_request(
            conn, manual_exit_request_id=manual_exit_request_id, book_id=book_id, symbol=symbol, operator=operator,
            reason=reason, requested_at=requested_at, idempotency_key=idempotency_key, created_at=now,
        )
        return {
            "manual_exit_request_id": manual_exit_request_id, "book_id": book_id, "symbol": symbol,
            "operator": operator, "reason": reason, "requested_at": requested_at.isoformat(),
            "idempotency_key": idempotency_key, "created": created,
        }


def _lifecycle_runs_upto(conn, as_of: datetime) -> list[dict]:
    return pb_repo.list_lifecycle_runs(conn, upto_as_of=as_of.isoformat())


def _report_for_book(conn, book_id: str, as_of: datetime, cfg) -> dict:
    snap = valuation.build_portfolio_snapshot(
        conn, book_id, as_of, price_provider=None, maximum_price_age_seconds=cfg.valuation.maximum_price_age_seconds,
    )
    positions = pb_repo.list_positions(conn, book_id, open_only=True)
    orders = pb_repo.list_order_intents(conn, book_id)
    pending_orders = [o for o in orders if o["status"] == "PENDING_SUBMISSION"]
    fills = pb_repo.list_fills(conn, book_id)
    today = as_of.date().isoformat()
    orders_filled_today = len([f for f in fills if f["fill_timestamp"][:10] == today])
    exit_decisions = pb_repo.list_exit_decisions(conn, book_id)
    exits_triggered_today = len([
        d for d in exit_decisions if d["as_of"][:10] == today and d["decision"] in EXIT_DECISIONS
    ])
    reconciliations = pb_repo.list_reconciliations(conn, book_id)
    recon_upto = [r for r in reconciliations if r["as_of"] <= as_of.isoformat()]
    reconciliation_status = recon_upto[-1]["status"] if recon_upto else None
    snap_positions = pb_repo.list_snapshot_positions(conn, book_id, snap.snapshot_id)
    max_concentration = None
    if snap.net_liquidation_value_usd:
        weights = [
            Decimal(p["market_value_usd"]) / snap.net_liquidation_value_usd
            for p in snap_positions if p["market_value_usd"] is not None
        ]
        max_concentration = max(weights) if weights else Decimal("0")
    completed_cycles = len({o["cycle_id"] for o in orders if not str(o["cycle_id"]).startswith("lifecycle:")})

    return {
        "book_id": book_id, "enabled": True,
        "cash_available_usd": str(cash_ledger.available_cash(conn, book_id)),
        "cash_reserved_usd": str(cash_ledger.reserved_cash(conn, book_id)),
        "net_liquidation_value_usd": str(snap.net_liquidation_value_usd) if snap.net_liquidation_value_usd is not None else None,
        "realized_pnl_usd": str(snap.realized_pnl_usd),
        "unrealized_pnl_usd": str(snap.unrealized_pnl_usd) if snap.unrealized_pnl_usd is not None else None,
        "open_positions": len(positions), "pending_orders": len(pending_orders),
        "orders_filled_today": orders_filled_today, "exits_triggered_today": exits_triggered_today,
        "reconciliation_status": reconciliation_status, "valuation_status": snap.valuation_status,
        "unvalued_positions": snap.unvalued_position_count,
        "maximum_position_concentration": str(max_concentration) if max_concentration is not None else None,
        "completed_experiment_cycles": completed_cycles,
    }


def _build_soak_report(conn, as_of: datetime, cfg) -> dict:
    """Milestone 9's `paper-book-soak-report` body, extracted (Milestone 9.1)
    so `paper_soak_run_cli` can call it against a connection it already
    holds open, instead of opening a second, nested `session(db_path)`.
    Never declares a winner — `promotion_evidence_status` here is only a
    pointer to the existing, authoritative `paper-promotion-status` command
    (Milestone 8), never recomputed/duplicated here."""
    lifecycle_runs = _lifecycle_runs_upto(conn, as_of)
    market_days_covered = len({r["as_of"][:10] for r in lifecycle_runs})
    completed_experiment_cycles = len(lifecycle_runs)

    books = {}
    for name, book_id in (("baseline", cfg.baseline.book_id), ("enhanced", cfg.enhanced.book_id)):
        if not cfg.is_book_enabled(book_id) or pb_repo.load_book(conn, book_id) is None:
            books[name] = {"book_id": book_id, "enabled": False}
            continue
        books[name] = _report_for_book(conn, book_id, as_of, cfg)

    enabled_reports = [b for b in books.values() if b.get("enabled")]
    if (
        completed_experiment_cycles < cfg.lifecycle.soak.minimum_completed_cycles
        or market_days_covered < cfg.lifecycle.soak.minimum_market_days
    ):
        status = "NOT_ENOUGH_HISTORY"
    elif any(
        b.get("reconciliation_status") not in (None, "MATCHED") or b.get("valuation_status") != VALUATION_COMPLETE
        for b in enabled_reports
    ):
        status = "ATTENTION_REQUIRED"
    else:
        status = "READY_FOR_ACTIVATION_REVIEW"

    comparison = None
    if books["baseline"].get("enabled") and books["enhanced"].get("enabled"):
        comparable_cycles = min(
            books["baseline"]["completed_experiment_cycles"], books["enhanced"]["completed_experiment_cycles"]
        )
        deltas = {}
        for key in ("net_liquidation_value_usd", "realized_pnl_usd", "unrealized_pnl_usd"):
            b_val, e_val = books["baseline"].get(key), books["enhanced"].get(key)
            deltas[key] = str(Decimal(e_val) - Decimal(b_val)) if b_val is not None and e_val is not None else None
        comparison = {
            "comparable_cycles": comparable_cycles, "metric_deltas": deltas,
            "promotion_evidence_status": "run paper-promotion-status for an authoritative, evidence-only result",
        }

    return {
        "as_of": as_of.isoformat(), "status": status,
        "completed_experiment_cycles": completed_experiment_cycles, "market_days_covered": market_days_covered,
        "books": books, "baseline_vs_enhanced": comparison,
    }


def paper_book_soak_report_cli(db_path: Path, *, as_of: datetime) -> dict:
    """`paper-book-soak-report` (docs/milestone-9.md Section 9): read-only.
    Thin config-load/error-wrap/session shell around `_build_soak_report`."""
    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    if not cfg.enabled:
        return {"error": "paper_books.enabled is false — no books exist to report on"}
    with session(db_path) as conn:
        return _build_soak_report(conn, as_of, cfg)


def evaluate_paper_soak_readiness(conn, as_of: datetime, cfg) -> dict:
    """Milestone 9's `paper-book-soak-readiness` body, extracted (Milestone
    9.1) so `controlled_soak_readiness.py`/`paper_soak_run_cli` can reuse it
    directly against an already-open connection instead of re-deriving the
    same completed-cycle/market-day/lifecycle-failure/reconciliation/
    valuation logic a second time. Deterministic, advisory-only. Never
    enables recurring processing — `READY_FOR_RECURRING_ACTIVATION_REVIEW`
    means "a human may now review activation," nothing here activates
    anything."""
    lifecycle_runs = _lifecycle_runs_upto(conn, as_of)
    market_days_covered = len({r["as_of"][:10] for r in lifecycle_runs})
    completed_cycles = len(lifecycle_runs)
    both_enabled = cfg.is_book_enabled(cfg.baseline.book_id) and cfg.is_book_enabled(cfg.enhanced.book_id)

    failed_checks: list[dict] = []

    def fail(name: str, result: str, reasons: list[str]) -> None:
        failed_checks.append({"name": name, "result": result, "reasons": reasons})

    if not both_enabled:
        fail("both_books_enabled", "NOT_READY_INSUFFICIENT_CYCLES", ["both baseline and enhanced books must be enabled for a comparable soak"])
    if completed_cycles < cfg.lifecycle.soak.minimum_completed_cycles:
        fail(
            "minimum_completed_cycles", "NOT_READY_INSUFFICIENT_CYCLES",
            [f"completed_cycles {completed_cycles} < minimum_completed_cycles {cfg.lifecycle.soak.minimum_completed_cycles}"],
        )
    if market_days_covered < cfg.lifecycle.soak.minimum_market_days:
        fail(
            "minimum_market_days", "NOT_READY_INSUFFICIENT_MARKET_DAYS",
            [f"market_days_covered {market_days_covered} < minimum_market_days {cfg.lifecycle.soak.minimum_market_days}"],
        )

    lifecycle_failures = [reason for run in lifecycle_runs for reason in run["failure_reasons"]]
    if lifecycle_failures:
        fail("lifecycle_failures", "NOT_READY_LIFECYCLE_FAILURES", lifecycle_failures[:10])

    recon_bad = []
    valuation_bad = []
    for book_id in (cfg.baseline.book_id, cfg.enhanced.book_id):
        reconciliations = pb_repo.list_reconciliations(conn, book_id)
        recon_upto = [r for r in reconciliations if r["as_of"] <= as_of.isoformat()]
        if recon_upto and recon_upto[-1]["status"] != "MATCHED":
            recon_bad.append(f"{book_id}: {recon_upto[-1]['status']}")
        snap = valuation.build_portfolio_snapshot(
            conn, book_id, as_of, price_provider=None, maximum_price_age_seconds=cfg.valuation.maximum_price_age_seconds,
            persist=False,
        )
        if snap.valuation_status != VALUATION_COMPLETE:
            valuation_bad.append(f"{book_id}: {snap.valuation_status}")
    if recon_bad:
        fail("paper_reconciliation", "NOT_READY_RECONCILIATION", recon_bad)
    if valuation_bad:
        fail("paper_valuation", "NOT_READY_VALUATION", valuation_bad)

    # Preserve the original deterministic primary order while returning all
    # failures to controlled readiness.
    primary_order = (
        "both_books_enabled", "minimum_completed_cycles", "minimum_market_days", "lifecycle_failures",
        "paper_reconciliation", "paper_valuation",
    )
    primary = next((f for name in primary_order for f in failed_checks if f["name"] == name), None)

    def outcome(result: str, reasons: list[str]) -> dict:
        return {
            "result": result, "reasons": reasons, "as_of": as_of.isoformat(),
            "completed_cycles": completed_cycles, "market_days_covered": market_days_covered,
            "both_books_enabled": both_enabled, "failed_checks": failed_checks,
        }
    if primary:
        return outcome(primary["result"], primary["reasons"])

    result = (
        "READY_FOR_RECURRING_ACTIVATION_REVIEW"
        if market_days_covered >= cfg.lifecycle.soak.minimum_market_days * 2
        else "READY_FOR_MORE_MANUAL_SOAK"
    )
    return outcome(result, [])


def paper_book_soak_readiness_cli(db_path: Path, *, as_of: datetime) -> dict:
    """`paper-book-soak-readiness` (docs/milestone-9.md Section 10): thin
    config-load/error-wrap/session shell around `evaluate_paper_soak_readiness`."""
    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    if not cfg.enabled:
        return {"error": "paper_books.enabled is false"}
    with session(db_path) as conn:
        return evaluate_paper_soak_readiness(conn, as_of, cfg)


def _operator_run_id(as_of: datetime, cycle_ids: tuple[str, ...]) -> str:
    """Deterministic on `(as_of, sorted cycle_ids)` (Milestone 9.1 Section 7)
    — mirrors `lifecycle.py::_lifecycle_run_id`'s own hashing convention, so
    replaying `paper-soak-run` for the identical date/cycle set always
    resolves to the same operator-run row."""
    from .soak_campaign import operator_run_id
    return operator_run_id(as_of, cycle_ids)


def _operator_run_to_json(run: dict) -> dict:
    return {
        "operator_run_id": run["operator_run_id"], "as_of": run["as_of"],
        "requested_cycle_ids": list(run["requested_cycle_ids"]), "lifecycle_run_id": run["lifecycle_run_id"],
        "baseline_reconciliation_status": run["baseline_reconciliation_status"],
        "enhanced_reconciliation_status": run["enhanced_reconciliation_status"],
        "soak_report_status": run["soak_report_status"], "controlled_readiness_status": run["controlled_readiness_status"],
        "failure_reasons": list(run["failure_reasons"]), "policy_version": run["policy_version"],
        "created_at": run["created_at"],
        "cross_book_verification_id": run.get("cross_book_verification_id"),
        "cross_book_verification_status": run.get("cross_book_verification_status"),
    }


def paper_soak_run_cli(
    db_path: Path, *, as_of: datetime, integrate_cycle_ids: tuple[str, ...] = (), audit_time_now: bool = False,
) -> dict:
    """`paper-soak-run` (Milestone 9.1 Section 6): the single manual,
    end-to-end operator command — validate config, validate shadow
    pause/kill state, optionally integrate explicitly supplied cycle IDs,
    run the lifecycle (which already reconciles every enabled book itself —
    no second reconciliation pass here), build the soak report, evaluate
    combined controlled-soak readiness, and persist a bounded operator-run
    summary. Never runs research, never calls Claude, never discovers
    cycles implicitly (the operator must supply `integrate_cycle_ids`
    explicitly; an empty tuple is a valid "lifecycle-only day"), never
    activates scheduling, never clears pause state, never hides a lifecycle
    failure (`failure_reasons` is always returned verbatim). Fails closed
    with an `"error"` key whenever `paper_books.enabled`/
    `paper_books.lifecycle.enabled` is false, or the shadow system is
    PAUSED/KILLED. Idempotent: every sub-step is independently idempotent
    (see `lifecycle.py`), and this function always recomputes fresh —
    `save_operator_run`'s insert-or-ignore on the deterministic
    `operator_run_id` means a replay never creates a second summary row,
    but the returned JSON always reflects the current, freshly recomputed
    state (matching `paper_book_lifecycle_run_cli`'s own convention)."""
    from ..shadow.config import load_shadow_operations_config
    from .soak_campaign import run_controlled_soak_day

    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    if not cfg.enabled:
        return {"error": "paper_books.enabled is false — paper-soak-run fails closed"}
    if not cfg.lifecycle.enabled:
        return {"error": "paper_books.lifecycle.enabled is false — paper-soak-run fails closed"}

    integrate_cycle_ids = tuple(integrate_cycle_ids)
    with session(db_path) as conn:
        shadow_cfg = load_shadow_operations_config()
        try:
            day = run_controlled_soak_day(
                conn, as_of=as_of, cycle_ids=integrate_cycle_ids, paper_books_config=cfg,
                shadow_config=shadow_cfg, audit_clock=_utc_now if audit_time_now else (lambda: as_of),
            )
        except (lifecycle.LifecycleError, RuntimeError) as exc:
            return {"error": str(exc)}
        if day["blocked_before_lifecycle"]:
            state = pause_mod.current_state(conn)
            return {"error": f"shadow pause state is {state.state} ({state.reason}) — paper-soak-run fails closed"}
        result = _operator_run_to_json(day["operator_run"])
        result["lifecycle_result"] = _lifecycle_result_to_json(day["lifecycle_result"])
        result["cross_book_verification"] = _cross_book_result_to_json(day["verification"])
        result["soak_report"] = day["soak_report"]
        result["controlled_readiness"] = _controlled_readiness_to_json(day["controlled_readiness"])
        return result


def paper_soak_readiness_cli(db_path: Path, *, as_of: datetime) -> dict:
    """`paper-soak-readiness` (Milestone 9.1 Section 10): combined
    paper-soak + shadow-operational readiness, read-only, advisory-only.
    Never activates or schedules anything — see
    `controlled_soak_readiness.py` for the full rule set."""
    from ..shadow.config import load_shadow_operations_config
    from .controlled_soak_readiness import evaluate_controlled_soak_readiness

    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    if not cfg.enabled:
        return {"error": "paper_books.enabled is false"}

    shadow_cfg = load_shadow_operations_config()
    with session(db_path) as conn:
        combined = evaluate_controlled_soak_readiness(conn, as_of, cfg, shadow_cfg)
        return _controlled_readiness_to_json(combined)


def _controlled_readiness_to_json(combined) -> dict:
    return {
        "status": combined.status, "reasons": list(combined.reasons),
        "paper_soak_status": combined.paper_soak_status, "shadow_activation_status": combined.shadow_activation_status,
        "policy_version": combined.policy_version,
        "checks": [
            {
                "name": c.name, "classification": c.classification, "passed": c.passed,
                "observed_value": c.observed_value, "threshold_value": c.threshold_value,
                "source": c.source, "reason": c.reason,
            }
            for c in combined.checks
        ],
        "all_failed_checks": [c.name for c in combined.checks if c.passed is False],
        "blocking_checks": [c.name for c in combined.checks if c.passed is False and c.classification != "MISSING"],
        "advisory_checks": [c.name for c in combined.checks if c.classification == "DERIVED"],
        "missing_checks": [c.name for c in combined.checks if c.classification == "MISSING"],
    }


def _cross_book_check_to_json(check) -> dict:
    return {
        "name": check.name, "status": check.status, "observed": check.observed, "expected": check.expected,
        "source": check.source, "reason": check.reason,
    }


def _cross_book_result_to_json(result) -> dict:
    return {
        "verification_id": result.verification_id, "as_of": result.as_of.isoformat(), "status": result.status,
        "violation_count": result.violation_count, "policy_version": result.policy_version,
        "checks": [_cross_book_check_to_json(c) for c in result.checks],
    }


def paper_book_cross_check_cli(
    db_path: Path, *, as_of: datetime, operator_run_id: str | None = None, lifecycle_run_id: str | None = None,
) -> dict:
    """`paper-book-cross-check` (Milestone 9.2 Section 13): read-only,
    deterministic, no network call. Persists the verification (mirroring
    `paper_book_lifecycle_run_cli`'s own convention: every sub-operation is
    independently idempotent, and this function always recomputes fresh) so
    `controlled_soak_readiness.py` has an authoritative row to read even
    when this command is invoked standalone, outside `paper-soak-run`. Fails
    closed with an `"error"` key when `paper_books.enabled` is false."""
    from .cross_book_verification import persist_verification, verify_cross_book_integrity

    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    if not cfg.enabled:
        return {"error": "paper_books.enabled is false — paper-book-cross-check fails closed"}

    with session(db_path) as conn:
        result = verify_cross_book_integrity(
            conn, as_of=as_of, paper_books_config=cfg, operator_run_id=operator_run_id,
            lifecycle_run_id=lifecycle_run_id,
        )
        persist_verification(
            conn, result, operator_run_id=operator_run_id, lifecycle_run_id=lifecycle_run_id, created_at=_utc_now(),
        )
        return _cross_book_result_to_json(result)


def paper_soak_campaign_validate_cli(manifest_path: Path) -> dict:
    from .soak_campaign import SoakCampaignError, load_campaign_manifest
    try:
        manifest = load_campaign_manifest(manifest_path)
    except SoakCampaignError as exc:
        return {"error": str(exc), "valid": False}
    return {
        "valid": True, "campaign_id": manifest.campaign_id, "manifest_hash": manifest.manifest_hash,
        "date_count": len(manifest.dates), "cycle_count": sum(len(day.cycle_ids) for day in manifest.dates),
        "dates": [{"as_of": day.as_of.isoformat(), "cycle_ids": list(day.cycle_ids)} for day in manifest.dates],
    }


def paper_soak_campaign_run_cli(
    db_path: Path, *, manifest_path: Path, continue_on_blocker: bool = False,
) -> dict:
    from ..shadow.config import load_shadow_operations_config
    from .soak_campaign import SoakCampaignError, load_campaign_manifest, run_soak_campaign
    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    if not cfg.soak_campaign.enabled:
        return {"error": "paper_books.soak_campaign.enabled is false — campaign run fails closed"}
    try:
        manifest = load_campaign_manifest(manifest_path)
        with session(db_path) as conn:
            return run_soak_campaign(
                conn, manifest=manifest, paper_books_config=cfg,
                shadow_config=load_shadow_operations_config(),
                stop_on_blocker=False if continue_on_blocker else cfg.soak_campaign.stop_on_blocker,
                audit_clock=_utc_now,
            )
    except SoakCampaignError as exc:
        return {"error": str(exc)}


def paper_soak_campaign_show_cli(db_path: Path, *, campaign_id: str) -> dict:
    from .soak_campaign import SoakCampaignError, show_soak_campaign
    try:
        with session(db_path) as conn:
            return show_soak_campaign(conn, campaign_id)
    except SoakCampaignError as exc:
        return {"error": str(exc)}


def paper_soak_activation_review_cli(db_path: Path, *, campaign_id: str) -> dict:
    with session(db_path) as conn:
        campaign = pb_repo.load_soak_campaign(conn, campaign_id)
        if campaign is None:
            return {"error": f"unknown campaign_id {campaign_id!r}"}
        review = pb_repo.load_soak_activation_review_for_campaign(conn, campaign_id)
        if review is None:
            return {"error": f"campaign {campaign_id!r} has no persisted activation review"}
        return review


def paper_recurring_request_activation_cli(
    db_path: Path, *, activation_review_id: str, operator: str, reason: str,
) -> dict:
    from .recurring_scheduler import RecurringPaperError, request_recurring_activation
    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    try:
        with session(db_path) as conn:
            return request_recurring_activation(
                conn, activation_review_id=activation_review_id, operator=operator, reason=reason,
                paper_books_config=cfg, now=_utc_now(),
            )
    except RecurringPaperError as exc:
        return {"error": str(exc)}


def paper_recurring_activate_cli(db_path: Path, *, request_event_id: str, operator: str) -> dict:
    from .recurring_scheduler import RecurringPaperError, activate_recurring
    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    try:
        with session(db_path) as conn:
            return activate_recurring(
                conn, request_event_id=request_event_id, operator=operator,
                paper_books_config=cfg, now=_utc_now(),
            )
    except RecurringPaperError as exc:
        return {"error": str(exc)}


def paper_recurring_deactivate_cli(db_path: Path, *, operator: str, reason: str) -> dict:
    from .recurring_scheduler import RecurringPaperError, deactivate_recurring
    try:
        with session(db_path) as conn:
            return deactivate_recurring(conn, operator=operator, reason=reason, now=_utc_now())
    except RecurringPaperError as exc:
        return {"error": str(exc)}


def paper_recurring_enqueue_cycle_cli(db_path: Path, *, cycle_id: str, operator: str, reason: str) -> dict:
    from .recurring_scheduler import RecurringPaperError, enqueue_recurring_cycle
    try:
        with session(db_path) as conn:
            return enqueue_recurring_cycle(conn, cycle_id=cycle_id, operator=operator, reason=reason, now=_utc_now())
    except RecurringPaperError as exc:
        return {"error": str(exc)}


def paper_recurring_cancel_cycle_cli(
    db_path: Path, *, queue_item_id: str, operator: str, reason: str,
) -> dict:
    from .recurring_scheduler import RecurringPaperError, cancel_recurring_cycle
    try:
        with session(db_path) as conn:
            return cancel_recurring_cycle(
                conn, queue_item_id=queue_item_id, operator=operator, reason=reason, now=_utc_now(),
            )
    except RecurringPaperError as exc:
        return {"error": str(exc)}


def paper_recurring_run_once_cli(db_path: Path, *, now: datetime, owner_id: str) -> dict:
    from dataclasses import asdict
    from ..shadow.config import load_shadow_operations_config
    from .recurring_scheduler import RecurringPaperError, run_recurring_paper_scheduler
    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    try:
        with session(db_path) as conn:
            result = run_recurring_paper_scheduler(
                conn, now=now, paper_books_config=cfg, shadow_config=load_shadow_operations_config(),
                owner_id=owner_id, audit_clock=_utc_now,
            )
        return asdict(result)
    except RecurringPaperError as exc:
        return {"error": str(exc)}


def paper_recurring_status_cli(db_path: Path) -> dict:
    from .recurring_scheduler import current_activation_state, recurring_config_hash
    cfg, error = _load_config_or_error()
    if error:
        return {"error": error}
    with session(db_path) as conn:
        state = current_activation_state(conn)
        queue = pb_repo.list_recurring_queue_items(conn)
        runs = pb_repo.list_recurring_scheduler_runs(conn, limit=10)
        lease = conn.execute(
            "SELECT * FROM paper_recurring_scheduler_leases WHERE lease_name = 'paper-recurring-local'"
        ).fetchone()
    return {
        "configuration_enabled": cfg.recurring.enabled,
        "activation_state": state.state,
        "activation_event": state.event,
        "recurring_config_hash": recurring_config_hash(cfg),
        "queue_counts": {status: sum(item["status"] == status for item in queue) for status in (
            "QUEUED", "CLAIMED", "PROCESSED", "FAILED", "CANCELLED",
        )},
        "lease": dict(lease) if lease else None,
        "recent_scheduler_runs": runs,
    }


def paper_recurring_queue_list_cli(db_path: Path, *, status: str | None = None) -> dict:
    from .recurring_scheduler import QUEUE_STATUSES
    if status is not None and status not in QUEUE_STATUSES:
        return {"error": f"unknown queue status {status!r}"}
    with session(db_path) as conn:
        items = pb_repo.list_recurring_queue_items(conn, status=status)
    return {"status_filter": status, "count": len(items), "items": items}
