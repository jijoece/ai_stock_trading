"""Manual daily paper-book lifecycle service (Milestone 9,
docs/milestone-9.md Section 7).

`run_paper_book_lifecycle` is the single entry point for a controlled,
persistent, multi-day paper-trading soak. It is:

    OFFLINE            — never calls a live quote/broker endpoint.
    DETERMINISTIC       — same `as_of` + same persisted state -> same result.
    POINT-IN-TIME SAFE  — reuses `valuation.select_valuation_price`, never a
                           future/current price for a historical `as_of`.
    BOOK ISOLATED        — every operation is scoped to exactly one book_id.
    IDEMPOTENT          — retrying the same lifecycle `as_of` never double-
                           applies a fill, reservation, or exit decision.
    MANUALLY INVOKED     — never called by launchd or a recurring scheduler;
                           `cli.py`'s `paper-book-lifecycle-run` is the
                           intended caller.

Processing order (fixed, matches docs/milestone-9.md Section 7):

    1. Validate lifecycle configuration (fails closed if disabled)
    2. Optionally integrate explicitly supplied cycle IDs (reuses Milestone
       8.1's `scheduled_integration.integrate_scheduled_cycle_into_paper_books`
       unchanged — this module never re-implements entry sizing)
    3. Process existing pending orders (fill / expire / remain pending)
    4. Evaluate exits for open positions (deterministic `exit_policy`)
    5. Persist exit decisions
    6. Create eligible SELL intents
    7. Simulate eligible fills
    8. Create one snapshot per enabled book
    9. Reconcile each enabled book
    10. Compute metrics
    11. Persist lifecycle-run summary

One book's failure is caught and recorded in `failure_reasons`; it never
prevents the other book from being processed, and never leaves partial
writes for the failed book beyond whatever each already-idempotent
sub-operation itself committed.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from ..storage import paper_books_repositories as repo
from ..storage import trading_repositories as trading_repo
from ..storage.transactions import transaction
from ..analysis.indicators import OHLCBar, average_true_range
from . import cash_ledger, execution, metrics as metrics_module, positions, reconciliation, valuation
from .config import PaperBooksConfiguration
from .exit_policy import (
    DECISION_EXIT_MANUAL_REQUEST,
    DECISION_EXIT_PARTIAL_PROFIT,
    DECISION_HOLD,
    EXIT_DECISIONS,
    evaluate_exit_decision,
    is_reversal_recommendation,
    market_days_held,
)
from .models import (
    INTENT_STATUS_FILLED,
    INTENT_STATUS_PENDING_SUBMISSION,
    INTENT_STATUS_REJECTED,
    ORDER_SIDE_SELL,
    ORDER_TYPE_LIMIT,
    VALUATION_PARTIAL_STALE_PRICE,
    PaperBookOrderIntent,
    derive_paper_order_intent_id,
)
from .scheduled_integration import ScheduledIntegrationError, integrate_scheduled_cycle_into_paper_books
from . import lifecycle_state as lifecycle_state_module
from . import daily_risk as daily_risk_module, safety_pause

LIFECYCLE_EXECUTION_VERSION = "paper-books-lifecycle-execution-v1"
LIFECYCLE_RUN_VERSION = "paper-books-lifecycle-run-v1"

STAGE_PENDING_ORDER = "PENDING_ORDER"
STAGE_EXIT = "EXIT"

PENDING_OUTCOME_FILLED = "FILLED"
PENDING_OUTCOME_EXPIRED = "EXPIRED"
PENDING_OUTCOME_STILL_PENDING = "STILL_PENDING"
PENDING_OUTCOME_REJECTED = "REJECTED"

EXIT_OUTCOME_ORDER_CREATED = "SELL_ORDER_CREATED"
EXIT_OUTCOME_ORDER_FILLED = "SELL_ORDER_FILLED"
EXIT_OUTCOME_NO_ACTION = "NO_ACTION"
EXIT_OUTCOME_SKIPPED = "SKIPPED"


class LifecycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaperBookLifecycleResult:
    lifecycle_run_id: str
    as_of: datetime
    processed_cycle_ids: tuple[str, ...]
    books_processed: tuple[str, ...]
    pending_orders_filled: int
    pending_orders_expired: int
    exit_decisions: tuple[dict, ...]
    exit_orders_created: int
    exit_orders_filled: int
    snapshot_ids: dict[str, str]
    reconciliation_statuses: dict[str, str]
    metrics_ids: dict[str, str]
    failure_reasons: tuple[str, ...]


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _lifecycle_run_id(as_of: datetime, config_hash: str) -> str:
    digest = hashlib.sha256(f"{as_of.isoformat()}:{config_hash}".encode()).hexdigest()[:32]
    return f"pb-lifecycle-{digest}"


def _exit_decision_id(book_id: str, symbol: str, as_of: datetime, policy_version: str) -> str:
    digest = hashlib.sha256(f"{book_id}:{symbol}:{as_of.isoformat()}:{policy_version}".encode()).hexdigest()[:32]
    return f"pb-exit-{digest}"


def _build_market_simulation_input(price_selection) -> execution.MarketSimulationInput | None:
    """Same tier-2 synthetic bid/ask construction as
    `scheduled_integration.py::_build_market_simulation_input` (reused, not
    reinvented): the point-in-time-safe reference price, converted to a
    symmetric bid/ask using `execution.DEFAULT_SLIPPAGE_BPS` as the
    half-spread proxy. `None` when no safe reference price exists — a
    pending/exit order is left `PENDING_SUBMISSION` rather than ever
    fabricating a fill."""
    if price_selection is None or price_selection.price is None:
        return None
    if price_selection.point_in_time_safe is False:
        return None
    reference = price_selection.price
    half_spread = reference * execution.DEFAULT_SLIPPAGE_BPS / Decimal("20000")
    bid = reference - half_spread
    ask = reference + half_spread
    if bid <= 0:
        return None
    return execution.MarketSimulationInput(bid=bid, ask=ask)


def _row_to_intent(row: dict) -> PaperBookOrderIntent:
    return PaperBookOrderIntent(
        paper_order_intent_id=row["paper_order_intent_id"], book_id=row["book_id"],
        experiment_arm=row["experiment_arm"], cycle_id=row["cycle_id"], recommendation_id=row["recommendation_id"],
        symbol=row["symbol"], side=row["side"], order_type=row["order_type"], quantity=Decimal(row["quantity"]),
        limit_price=Decimal(row["limit_price"]), notional_usd=Decimal(row["notional_usd"]),
        time_in_force=row["time_in_force"], as_of=_parse_iso(row["as_of"]), risk_decision_id=row["risk_decision_id"],
        portfolio_snapshot_id=row["portfolio_snapshot_id"], config_hash=row["config_hash"],
        created_at=_parse_iso(row["created_at"]), status=row["status"],
    )


def _save_symbol_result(
    conn, *, lifecycle_run_id: str, book_id: str, symbol: str, stage: str, outcome: str,
    reasons: tuple[str, ...] = (), exit_decision_id: str | None = None,
    paper_order_intent_id: str | None = None, fill_id: str | None = None, clock: Callable[[], datetime],
) -> None:
    repo.save_lifecycle_symbol_result(
        conn, lifecycle_run_id=lifecycle_run_id, book_id=book_id, symbol=symbol, stage=stage, outcome=outcome,
        reasons=reasons, exit_decision_id=exit_decision_id, paper_order_intent_id=paper_order_intent_id,
        fill_id=fill_id, created_at=clock(),
    )


def _process_pending_orders(
    conn, *, book_id: str, as_of: datetime, cfg: PaperBooksConfiguration, price_provider: Any,
    clock: Callable[[], datetime], lifecycle_run_id: str,
) -> tuple[int, int]:
    """docs/milestone-9.md Section 6: for every `PENDING_SUBMISSION` order,
    verify it, obtain a fresh point-in-time-safe market-simulation input for
    `as_of`, simulate a fill via the existing engine, or expire it once its
    configured market-day age is exceeded. Idempotent: `execution.py`'s
    `save_order_intent`/`fill_exists` guards mean re-running this against an
    already-resolved order is always a safe no-op."""
    filled = 0
    expired = 0
    pending = [o for o in repo.list_order_intents(conn, book_id) if o["status"] == INTENT_STATUS_PENDING_SUBMISSION]
    for row in pending:
        intent = _row_to_intent(row)
        if cfg.external_broker.enabled and book_id in cfg.external_broker.enabled_book_ids:
            repo.enqueue_external_submission(
                conn,
                queue_id="peqs_" + hashlib.sha256(
                    f"{book_id}:{intent.paper_order_intent_id}:lifecycle".encode()
                ).hexdigest()[:40],
                book_id=book_id, paper_order_intent_id=intent.paper_order_intent_id,
                source="RECURRING_LOCAL_PAPER", created_at=clock().isoformat(),
            )
            _save_symbol_result(
                conn, lifecycle_run_id=lifecycle_run_id, book_id=book_id, symbol=intent.symbol,
                stage=STAGE_PENDING_ORDER, outcome=PENDING_OUTCOME_STILL_PENDING,
                reasons=("AWAITING_OPERATOR_EXTERNAL_SUBMISSION; lifecycle made no broker mutation",),
                paper_order_intent_id=intent.paper_order_intent_id, clock=clock,
            )
            continue
        age_market_days = market_days_held(intent.created_at.date(), as_of.date())
        if age_market_days >= cfg.lifecycle.pending_orders.expire_after_market_days:
            execution.expire_pending_intent(conn, intent, clock())
            expired += 1
            _save_symbol_result(
                conn, lifecycle_run_id=lifecycle_run_id, book_id=book_id, symbol=intent.symbol,
                stage=STAGE_PENDING_ORDER, outcome=PENDING_OUTCOME_EXPIRED,
                reasons=(f"age {age_market_days} market days >= expire_after_market_days "
                         f"{cfg.lifecycle.pending_orders.expire_after_market_days}",),
                paper_order_intent_id=intent.paper_order_intent_id, clock=clock,
            )
            continue

        price_selection = valuation.select_valuation_price(
            intent.symbol, as_of, price_provider=price_provider,
            maximum_price_age_seconds=cfg.valuation.maximum_price_age_seconds,
        )
        market_input = _build_market_simulation_input(price_selection)
        if market_input is None:
            _save_symbol_result(
                conn, lifecycle_run_id=lifecycle_run_id, book_id=book_id, symbol=intent.symbol,
                stage=STAGE_PENDING_ORDER, outcome=PENDING_OUTCOME_STILL_PENDING,
                reasons=("no point-in-time-safe market-simulation input available for this as_of",),
                paper_order_intent_id=intent.paper_order_intent_id, clock=clock,
            )
            continue

        submit_result = execution.submit_and_simulate(conn, intent, market_input, clock())
        fill = submit_result.get("fill")
        if submit_result["status"] == INTENT_STATUS_FILLED and fill:
            filled += 1
            _save_symbol_result(
                conn, lifecycle_run_id=lifecycle_run_id, book_id=book_id, symbol=intent.symbol,
                stage=STAGE_PENDING_ORDER, outcome=PENDING_OUTCOME_FILLED,
                paper_order_intent_id=intent.paper_order_intent_id, fill_id=fill["fill_id"], clock=clock,
            )
        elif submit_result["status"] == INTENT_STATUS_REJECTED:
            _save_symbol_result(
                conn, lifecycle_run_id=lifecycle_run_id, book_id=book_id, symbol=intent.symbol,
                stage=STAGE_PENDING_ORDER, outcome=PENDING_OUTCOME_REJECTED,
                reasons=(submit_result.get("reason", "rejected"),),
                paper_order_intent_id=intent.paper_order_intent_id, clock=clock,
            )
        else:
            _save_symbol_result(
                conn, lifecycle_run_id=lifecycle_run_id, book_id=book_id, symbol=intent.symbol,
                stage=STAGE_PENDING_ORDER, outcome=PENDING_OUTCOME_STILL_PENDING,
                paper_order_intent_id=intent.paper_order_intent_id, clock=clock,
            )
    return filled, expired


_UNRESOLVED_EXTERNAL_SELL_STATES = frozenset({
    "SUBMISSION_REQUESTED", "SUBMITTED", "PARTIALLY_FILLED", "CANCEL_REQUESTED",
    "UNKNOWN_REQUIRES_RECONCILIATION",
})


def _has_unresolved_pending_sell(conn, book_id: str, symbol: str) -> bool:
    """True if a local pending-submission SELL exists, or an external order for
    this book/symbol is in a state that has not yet resolved to a terminal
    outcome with its reservation released. A terminal FILLED/CANCELLED/
    REJECTED/EXPIRED order no longer blocks once its share reservation is
    resolved (Part 3/4): checking the reservation directly, rather than only
    the broker state, keeps this in sync even if release lags a state
    transition by one reconciliation cycle."""
    for o in repo.list_order_intents(conn, book_id):
        if o["symbol"] != symbol or o["side"] != ORDER_SIDE_SELL:
            continue
        if o["status"] == INTENT_STATUS_PENDING_SUBMISSION:
            return True
        event = repo.load_latest_external_order_event_for_intent(conn, book_id, o["paper_order_intent_id"])
        if event is None:
            continue
        state = event["new_state"]
        if state in _UNRESOLVED_EXTERNAL_SELL_STATES:
            return True
        if state == "PREVIEWED" or state in ("FILLED", "CANCELLED", "REJECTED", "EXPIRED"):
            if positions.remaining_share_reservation(conn, book_id, o["paper_order_intent_id"]) > 0:
                return True
    return False


def _current_atr(price_provider: Any, symbol: str, as_of: datetime, period: int) -> tuple[Decimal | None, str]:
    source_id = f"{symbol}:{as_of.isoformat()}"
    if price_provider is None or not hasattr(price_provider, "get_price_history"):
        return None, source_id
    bars = price_provider.get_price_history(
        symbol, start=as_of.date() - timedelta(days=period * 4), end=as_of.date(), as_of=as_of,
    )
    if not bars:
        return None, source_id
    atr = average_true_range(tuple(
        OHLCBar(bar.session_date, Decimal(bar.high), Decimal(bar.low), Decimal(bar.close)) for bar in bars
    ), period=period)
    provider = getattr(bars[-1], "provider", "persisted-market-bar")
    return atr, f"{provider}:{symbol}:{bars[-1].session_date.isoformat()}"


def _evaluate_exits(
    conn, *, book_id: str, as_of: datetime, cfg: PaperBooksConfiguration, price_provider: Any,
    clock: Callable[[], datetime], lifecycle_run_id: str,
) -> tuple[list[dict], int, int]:
    """docs/milestone-9.md Sections 2-5: deterministic exit evaluation for
    every open long position, persisted exit decisions, and eligible SELL
    intents reusing the Milestone 8 execution/position modules — no second
    fill simulator."""
    decisions: list[dict] = []
    orders_created = 0
    orders_filled = 0
    book = repo.load_book(conn, book_id)
    if book is None:
        raise LifecycleError(f"unknown book_id {book_id!r}")

    for pos in repo.list_positions(conn, book_id, open_only=True):
        symbol = pos["symbol"]

        if _has_unresolved_pending_sell(conn, book_id, symbol):
            _save_symbol_result(
                conn, lifecycle_run_id=lifecycle_run_id, book_id=book_id, symbol=symbol, stage=STAGE_EXIT,
                outcome=EXIT_OUTCOME_SKIPPED, reasons=("an unresolved pending exit order already exists",),
                clock=clock,
            )
            continue

        price_selection = valuation.select_valuation_price(
            symbol, as_of, price_provider=price_provider, maximum_price_age_seconds=cfg.valuation.maximum_price_age_seconds,
        )
        price_is_stale = price_selection.status == VALUATION_PARTIAL_STALE_PRICE
        cost_basis_per_share = Decimal(pos["average_cost_usd"]) if pos.get("average_cost_usd") is not None else None

        open_lots = repo.list_open_lots(conn, book_id, symbol)
        position_opened_at = _parse_iso(open_lots[0]["opened_at"]) if open_lots else None

        manual_requests = repo.list_unconsumed_manual_exit_requests(conn, book_id, symbol)
        manual_request = manual_requests[0] if manual_requests else None

        reversal_recommendation = None
        if cfg.lifecycle.exits.exit_on_recommendation_reversal and position_opened_at is not None:
            candidates = trading_repo.list_recommendations_by_symbol_since(
                conn, symbol, position_opened_at.isoformat(), as_of.isoformat(),
            )
            reversal_recommendation = next((r for r in candidates if is_reversal_recommendation(r)), None)

        lifecycle_state = None
        partial_stage_id = None
        partial_quantity = None
        lifecycle_row = repo.latest_position_lifecycle_state(conn, book_id, symbol)
        if lifecycle_row is not None:
            lifecycle_state = lifecycle_state_module.lifecycle_state_from_row(lifecycle_row)
            current_atr, source_id = _current_atr(
                price_provider, symbol, as_of, lifecycle_state.atr_period,
            )
            transition = lifecycle_state_module.advance_lifecycle_state(
                replace(lifecycle_state, remaining_quantity=Decimal(pos["quantity"])),
                as_of=as_of, reference_price=price_selection.price,
                price_is_stale=price_is_stale,
                price_point_in_time_safe=price_selection.point_in_time_safe,
                current_atr=current_atr, source_market_data_id=source_id,
                breakeven_enabled=cfg.lifecycle.breakeven.enabled,
                breakeven_activation_r_multiple=cfg.lifecycle.breakeven.activation_r_multiple,
                breakeven_offset_bps=cfg.lifecycle.breakeven.offset_bps,
                trailing_enabled=cfg.lifecycle.trailing_stop.enabled,
                trailing_activation_r_multiple=cfg.lifecycle.trailing_stop.activation_r_multiple,
                trailing_atr_multiple=cfg.lifecycle.trailing_stop.atr_multiple,
            )
            lifecycle_state = transition.state
            event_id = "pb-lifecycle-event-" + hashlib.sha256(
                f"{transition.previous_state_id}:{transition.state.lifecycle_state_id}".encode()
            ).hexdigest()[:40]
            with transaction(conn):
                repo.save_position_lifecycle_state(conn, transition.state, commit=False)
                repo.save_lifecycle_state_event(
                    conn, lifecycle_event_id=event_id, book_id=book_id, symbol=symbol,
                    previous_state_id=transition.previous_state_id,
                    resulting_state_id=transition.state.lifecycle_state_id,
                    event_type="STOP_EVALUATION", complete=transition.complete,
                    reasons=transition.reasons, created_at=clock(), commit=False,
                )
            if cfg.lifecycle.partial_profit.enabled:
                eligible = lifecycle_state_module.next_partial_stage(
                    state=lifecycle_state, current_r_multiple=transition.current_r_multiple,
                    stages=cfg.lifecycle.partial_profit.stages,
                    available_unreserved_quantity=Decimal(pos["available_quantity"]),
                    minimum_remaining_quantity=cfg.lifecycle.partial_profit.minimum_remaining_quantity,
                )
                if eligible is not None:
                    partial_stage_id, partial_quantity = eligible

        decision = evaluate_exit_decision(
            book_id=book_id, symbol=symbol, position_quantity=Decimal(pos["quantity"]),
            cost_basis_per_share=cost_basis_per_share, position_opened_at=position_opened_at, as_of=as_of,
            reference_price=price_selection.price, price_point_in_time_safe=price_selection.point_in_time_safe,
            price_is_stale=price_is_stale, stop_loss_percent=cfg.lifecycle.exits.stop_loss_percent,
            profit_target_percent=cfg.lifecycle.exits.profit_target_percent,
            maximum_holding_market_days=cfg.lifecycle.exits.maximum_holding_market_days,
            exit_on_recommendation_reversal=cfg.lifecycle.exits.exit_on_recommendation_reversal,
            reversal_recommendation=reversal_recommendation, manual_request=manual_request,
            current_stop_price=lifecycle_state.current_stop_price if lifecycle_state else None,
            initial_target_price=lifecycle_state.initial_target_price if lifecycle_state else None,
            trailing_stop_active=lifecycle_state.trailing_stop_active if lifecycle_state else False,
            breakeven_active=lifecycle_state.breakeven_active if lifecycle_state else False,
            partial_stage_id=partial_stage_id, partial_close_quantity=partial_quantity,
        )
        exit_decision_id = _exit_decision_id(book_id, symbol, as_of, decision.policy_version)
        manual_request_id = (
            manual_request["manual_exit_request_id"]
            if manual_request is not None and decision.decision == DECISION_EXIT_MANUAL_REQUEST else None
        )
        repo.save_exit_decision(
            conn, exit_decision_id=exit_decision_id, book_id=book_id, symbol=symbol, as_of=as_of, decision=decision,
            manual_exit_request_id=manual_request_id, created_at=clock(),
        )
        decisions.append({
            "exit_decision_id": exit_decision_id, "book_id": book_id, "symbol": symbol,
            "decision": decision.decision, "reasons": list(decision.reasons),
        })

        if decision.decision not in EXIT_DECISIONS:
            outcome = EXIT_OUTCOME_NO_ACTION if decision.decision == DECISION_HOLD else EXIT_OUTCOME_SKIPPED
            _save_symbol_result(
                conn, lifecycle_run_id=lifecycle_run_id, book_id=book_id, symbol=symbol, stage=STAGE_EXIT,
                outcome=outcome, reasons=decision.reasons, exit_decision_id=exit_decision_id, clock=clock,
            )
            continue

        available_qty = Decimal(pos["available_quantity"])
        quantity = min(decision.quantity, available_qty)
        if quantity <= 0:
            _save_symbol_result(
                conn, lifecycle_run_id=lifecycle_run_id, book_id=book_id, symbol=symbol, stage=STAGE_EXIT,
                outcome=EXIT_OUTCOME_SKIPPED, reasons=("no available (unreserved) quantity to sell",),
                exit_decision_id=exit_decision_id, clock=clock,
            )
            continue

        limit_price = decision.reference_price
        if limit_price is None:
            raise LifecycleError("an exit decision cannot create an order without a reference price")
        notional = quantity * limit_price
        intent_id = derive_paper_order_intent_id(exit_decision_id, book_id, LIFECYCLE_EXECUTION_VERSION)
        snap = valuation.build_portfolio_snapshot(
            conn, book_id, as_of, price_provider=price_provider, maximum_price_age_seconds=cfg.valuation.maximum_price_age_seconds,
        )
        now = clock()
        intent = PaperBookOrderIntent(
            paper_order_intent_id=intent_id, book_id=book_id, experiment_arm=book.experiment_arm,
            cycle_id=f"lifecycle:{as_of.date().isoformat()}", recommendation_id=exit_decision_id, symbol=symbol,
            side=ORDER_SIDE_SELL, order_type=ORDER_TYPE_LIMIT, quantity=quantity, limit_price=limit_price,
            notional_usd=notional, time_in_force="DAY", as_of=as_of, risk_decision_id=exit_decision_id,
            portfolio_snapshot_id=snap.snapshot_id, config_hash=cfg.config_hash, created_at=now,
        )
        if cfg.external_broker.enabled and book_id in cfg.external_broker.enabled_book_ids:
            repo.save_order_intent(conn, intent)
            repo.enqueue_external_submission(
                conn,
                queue_id="peqs_" + hashlib.sha256(f"{book_id}:{intent_id}:exit".encode()).hexdigest()[:40],
                book_id=book_id, paper_order_intent_id=intent_id,
                source="RECURRING_LOCAL_PAPER", created_at=now.isoformat(),
            )
            orders_created += 1
            _save_symbol_result(
                conn, lifecycle_run_id=lifecycle_run_id, book_id=book_id, symbol=symbol, stage=STAGE_EXIT,
                outcome=EXIT_OUTCOME_ORDER_CREATED,
                reasons=("AWAITING_OPERATOR_EXTERNAL_SUBMISSION; lifecycle made no broker mutation",),
                exit_decision_id=exit_decision_id, paper_order_intent_id=intent_id, clock=clock,
            )
            continue
        market_input = _build_market_simulation_input(price_selection)
        if market_input is None:
            repo.save_order_intent(conn, intent)
            orders_created += 1
            _save_symbol_result(
                conn, lifecycle_run_id=lifecycle_run_id, book_id=book_id, symbol=symbol, stage=STAGE_EXIT,
                outcome=EXIT_OUTCOME_ORDER_CREATED, reasons=("no market-simulation input available yet",),
                exit_decision_id=exit_decision_id, paper_order_intent_id=intent_id, clock=clock,
            )
            continue

        submit_result = execution.submit_and_simulate(conn, intent, market_input, now)
        orders_created += 1
        fill = submit_result.get("fill")
        if submit_result["status"] == INTENT_STATUS_FILLED and fill:
            orders_filled += 1
            if (
                decision.decision == DECISION_EXIT_PARTIAL_PROFIT
                and decision.partial_stage_id is not None and lifecycle_state is not None
            ):
                completed_state = lifecycle_state_module.apply_completed_partial_stage(
                    lifecycle_state, stage_id=decision.partial_stage_id,
                    filled_quantity=Decimal(fill["fill_quantity"]), as_of=now,
                    source_market_data_id=lifecycle_state.source_market_data_id,
                )
                stage_cfg = next(
                    stage for stage in cfg.lifecycle.partial_profit.stages
                    if stage.stage == decision.partial_stage_id
                )
                stage_event_id = "pb-partial-stage-" + hashlib.sha256(
                    f"{exit_decision_id}:{decision.partial_stage_id}".encode()
                ).hexdigest()[:40]
                with transaction(conn):
                    repo.save_position_lifecycle_state(conn, completed_state, commit=False)
                    repo.save_partial_exit_stage_event(conn, {
                        "partial_stage_event_id": stage_event_id, "book_id": book_id,
                        "symbol": symbol, "stage_id": decision.partial_stage_id,
                        "trigger_r_multiple": stage_cfg.trigger_r_multiple,
                        "evaluated_price": decision.reference_price,
                        "quantity_before": lifecycle_state.remaining_quantity,
                        "quantity_requested": decision.quantity,
                        "quantity_approved": decision.quantity,
                        "quantity_filled": Decimal(fill["fill_quantity"]),
                        "quantity_remaining": completed_state.remaining_quantity,
                        "resulting_stop_state_id": completed_state.lifecycle_state_id,
                        "decision_id": exit_decision_id,
                        "lifecycle_evaluation_id": lifecycle_run_id,
                        "status": "COMPLETED", "created_at": now,
                    }, commit=False)
            _save_symbol_result(
                conn, lifecycle_run_id=lifecycle_run_id, book_id=book_id, symbol=symbol, stage=STAGE_EXIT,
                outcome=EXIT_OUTCOME_ORDER_FILLED, exit_decision_id=exit_decision_id,
                paper_order_intent_id=intent_id, fill_id=fill["fill_id"], clock=clock,
            )
        else:
            _save_symbol_result(
                conn, lifecycle_run_id=lifecycle_run_id, book_id=book_id, symbol=symbol, stage=STAGE_EXIT,
                outcome=EXIT_OUTCOME_ORDER_CREATED, exit_decision_id=exit_decision_id,
                paper_order_intent_id=intent_id, clock=clock,
            )
    return decisions, orders_created, orders_filled


def run_paper_book_lifecycle(
    conn, *, as_of: datetime, paper_books_config: PaperBooksConfiguration, price_provider: Any = None,
    integrate_cycle_ids: tuple[str, ...] = (), experiment_policy: str = "BOTH_SEPARATE_PAPER_BOOKS",
    clock: Callable[[], datetime] | None = None,
) -> PaperBookLifecycleResult:
    if clock is None:
        # Anchored to `as_of`, never wall-clock `now()` — this module's own
        # "no implicit current-time" / "explicit as_of" contract applies to
        # every timestamp it stamps by default, including order/decision
        # `created_at`, not only price lookups. A caller that genuinely wants
        # a distinct "actually run at" audit timestamp may still inject one.
        clock = lambda: as_of
    cfg = paper_books_config
    if not cfg.enabled:
        raise LifecycleError("paper_books.enabled is false — lifecycle processing fails closed")
    if not cfg.lifecycle.enabled:
        raise LifecycleError("paper_books.lifecycle.enabled is false — lifecycle processing fails closed")
    if as_of.tzinfo is None:
        raise LifecycleError("as_of must be timezone-aware")

    lifecycle_run_id = _lifecycle_run_id(as_of, cfg.config_hash)
    created_at = clock()

    processed_cycle_ids: list[str] = []
    failure_reasons: list[str] = []
    for cycle_id in integrate_cycle_ids:
        try:
            integrate_scheduled_cycle_into_paper_books(
                conn, cycle_id=cycle_id, experiment_policy=experiment_policy, paper_books_config=cfg,
                clock=clock, price_provider=price_provider,
            )
            processed_cycle_ids.append(cycle_id)
        except ScheduledIntegrationError as exc:
            failure_reasons.append(f"cycle {cycle_id!r} integration failed: {exc}")

    books_processed: list[str] = []
    pending_filled_total = 0
    pending_expired_total = 0
    exit_decisions_total: list[dict] = []
    exit_orders_created_total = 0
    exit_orders_filled_total = 0
    snapshot_ids: dict[str, str] = {}
    reconciliation_statuses: dict[str, str] = {}
    metrics_ids: dict[str, str] = {}

    for book_id in (cfg.baseline.book_id, cfg.enhanced.book_id):
        if not cfg.is_book_enabled(book_id):
            continue
        try:
            book_def = cfg.book(book_id)
            cash_ledger.open_book(
                conn, book_id=book_id, starting_cash_usd=book_def.starting_cash_usd,
                config_hash=cfg.config_hash, clock=clock,
            )

            filled, expired = _process_pending_orders(
                conn, book_id=book_id, as_of=as_of, cfg=cfg, price_provider=price_provider, clock=clock,
                lifecycle_run_id=lifecycle_run_id,
            )
            pending_filled_total += filled
            pending_expired_total += expired

            if cfg.lifecycle.exits.enabled:
                decisions, created, filled_exits = _evaluate_exits(
                    conn, book_id=book_id, as_of=as_of, cfg=cfg, price_provider=price_provider, clock=clock,
                    lifecycle_run_id=lifecycle_run_id,
                )
                exit_decisions_total.extend(decisions)
                exit_orders_created_total += created
                exit_orders_filled_total += filled_exits

            snap = valuation.build_portfolio_snapshot(
                conn, book_id, as_of, price_provider=price_provider,
                maximum_price_age_seconds=cfg.valuation.maximum_price_age_seconds,
            )
            snapshot_ids[book_id] = snap.snapshot_id

            recon = reconciliation.reconcile_book(conn, book_id, as_of)
            reconciliation_statuses[book_id] = recon["status"]

            if "max_daily_loss_fraction" in cfg.raw.get("paper_books", {}).get("risk", {}):
                try:
                    risk_state = daily_risk_module.calculate_and_persist_daily_risk_state(
                        conn, book_id=book_id, market_date=as_of.date(), as_of=as_of,
                        config_hash=cfg.config_hash,
                        require_reconciled=cfg.risk.require_reconciled_risk_state,
                    )
                    if risk_state.daily_loss_fraction <= -cfg.risk.max_daily_loss_fraction:
                        safety_pause.pause_for_risk_state(
                            conn, book_id=book_id, reason_code="RISK_REJECTED_DAILY_LOSS_LIMIT",
                            risk_state_id=risk_state.risk_state_id,
                            reason="daily loss limit breached", at=clock(),
                        )
                    elif risk_state.current_drawdown_fraction <= -cfg.risk.max_drawdown_fraction:
                        safety_pause.pause_for_risk_state(
                            conn, book_id=book_id, reason_code="RISK_REJECTED_DRAWDOWN_LIMIT",
                            risk_state_id=risk_state.risk_state_id,
                            reason="drawdown limit breached", at=clock(),
                        )
                except daily_risk_module.DailyRiskStateError as exc:
                    failure_reasons.append(f"book {book_id!r} daily risk state incomplete: {exc}")

            window_start = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
            metrics_id = metrics_module.save_book_metrics(conn, book_id, window_start, as_of, clock=clock)
            metrics_ids[book_id] = metrics_id

            books_processed.append(book_id)
        except Exception as exc:  # one book's failure never mutates the other
            failure_reasons.append(f"book {book_id!r} lifecycle processing failed: {exc}")
            continue

    result = PaperBookLifecycleResult(
        lifecycle_run_id=lifecycle_run_id, as_of=as_of, processed_cycle_ids=tuple(processed_cycle_ids),
        books_processed=tuple(books_processed), pending_orders_filled=pending_filled_total,
        pending_orders_expired=pending_expired_total, exit_decisions=tuple(exit_decisions_total),
        exit_orders_created=exit_orders_created_total, exit_orders_filled=exit_orders_filled_total,
        snapshot_ids=snapshot_ids, reconciliation_statuses=reconciliation_statuses, metrics_ids=metrics_ids,
        failure_reasons=tuple(failure_reasons),
    )
    repo.save_lifecycle_run(conn, {
        "lifecycle_run_id": lifecycle_run_id, "as_of": as_of, "processed_cycle_ids": processed_cycle_ids,
        "books_processed": books_processed, "pending_orders_filled": pending_filled_total,
        "pending_orders_expired": pending_expired_total, "exit_decisions": exit_decisions_total,
        "exit_orders_created": exit_orders_created_total, "exit_orders_filled": exit_orders_filled_total,
        "snapshot_ids": snapshot_ids, "reconciliation_statuses": reconciliation_statuses, "metrics_ids": metrics_ids,
        "failure_reasons": failure_reasons, "config_hash": cfg.config_hash, "created_at": created_at,
    })
    return result
