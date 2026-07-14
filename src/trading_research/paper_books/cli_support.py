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

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ..research import experiment_policy as ep
from ..storage import paper_books_repositories as pb_repo
from ..storage.database import session
from . import cash_ledger, comparison as comparison_module, execution, order_intent, promotion_evidence, reconciliation, risk as risk_module, valuation
from .config import PaperBooksConfigError, load_paper_books_config


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
