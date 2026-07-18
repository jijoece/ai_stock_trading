"""Milestone 8.1: closes the integration gap between the real scheduled
research cycle (`research/scheduled_cycle.py`) and the isolated paper-book
subsystem (Milestone 8, `paper_books/`):

    real scheduled research cycle
    -> frozen baseline/enhanced recommendations
    -> shared EvidenceSnapshot and as_of
    -> isolated per-book portfolio valuation
    -> deterministic risk decisions
    -> book-aware paper intents
    -> local simulated fills
    -> book-specific reconciliation

This module never constructs a recommendation, never fetches evidence, and
never re-derives Claude output. It only reads already-persisted, already-
frozen scheduled-cycle records (via `SQLiteResearchCycleRepository`,
`storage/trading_repositories.py::load_recommendation`, and
`storage/research_repositories.py::load_evidence_snapshot`) and drives them
through the existing Milestone 8 paper-book primitives (`valuation`/`risk`/
`order_intent`/`execution`/`reconciliation`) exactly the way
`paper_books/cli_support.py::paper_book_run_cycle_cli` already does for its
fixture-mode, CLI-supplied inputs (docs/milestone8-progress.md "CLI design").

Disabled by default at two independent levels
(`paper_books.enabled`/`paper_books.scheduled_integration.enabled`), both
checked here directly (defense in depth, regardless of caller diligence).
The routing `experiment_policy` is an explicit parameter, not read from the
cycle's own recorded `research_cycles.experiment_policy` column — that
column is always drawn from the *legacy* supported set
(`OBSERVE_ONLY`/`BASELINE_ONLY`/`SHADOW_ENHANCED`, see
`research/scheduled_cycle.py::ScheduledResearchConfiguration.__post_init__`)
because `ENHANCED_ONLY`/`BOTH_SEPARATE_PAPER_BOOKS` are structurally
rejected at cycle-creation time. Paper-book routing is a separate policy
surface entirely (`research/experiment_policy.py`'s additive
`may_submit_*_to_paper_book` functions), so the caller supplies it here
explicitly — see `.claude/scratchpads/milestone8-1-progress.md` "Scheduled-
cycle output mapping" for the full field-provenance table.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
from typing import Any, Callable

from ..recommendations.builder import SIDE_BUY_CANDIDATE, STATUS_ACTIVE
from ..research import experiment_policy as ep
from ..research.evidence_completeness import STATUS_COMPLETE_FOR_SCREENING
from ..research.models import EvidenceSnapshot
from ..storage.research_cycle_repositories import SQLiteResearchCycleRepository, load_symbol_evidence_status
from ..storage.research_repositories import load_evidence_snapshot
from ..storage import paper_books_repositories as pb_repo
from ..storage.trading_repositories import load_recommendation
from ..evidence_providers.economic_calendar import evaluate_economic_event_blackout
from ..analysis.indicators import OHLCBar, average_true_range
from ..risk.position_sizing import IncompleteStateError, compute_atr_position_plan
from . import cash_ledger, execution, order_intent, reconciliation
from . import risk as risk_module
from . import valuation
from . import daily_risk as daily_risk_module
from . import safety_pause
from . import lifecycle_state as lifecycle_state_module
from .config import PaperBooksConfiguration
from .experiment_assignment import PaperBookExperimentAssignment, save_assignment
from .models import (
    APPROVED_RISK_DECISIONS,
    RISK_REJECTED_DAILY_LOSS_LIMIT,
    RISK_REJECTED_DRAWDOWN_LIMIT,
    VALUATION_POINT_IN_TIME_UNSAFE,
    VALUATION_SOURCE_UNAVAILABLE,
    derive_paper_order_intent_id,
)

MARKET_SIMULATION_SOURCE_OBSERVED = "OBSERVED"
MARKET_SIMULATION_SOURCE_SIMULATED = "SIMULATED"
MARKET_SIMULATION_INPUT_UNAVAILABLE = "MARKET_SIMULATION_INPUT_UNAVAILABLE"
MARKET_SIMULATION_POLICY_VERSION = "paper-books-scheduled-market-sim-v1"

OUTCOME_EXECUTED = "EXECUTED"
OUTCOME_INTENT_CREATED_PENDING_FILL = "INTENT_CREATED_PENDING_FILL"
OUTCOME_AWAITING_OPERATOR_EXTERNAL_SUBMISSION = "AWAITING_OPERATOR_EXTERNAL_SUBMISSION"
OUTCOME_SKIPPED_BOOK_DISABLED = "SKIPPED_BOOK_DISABLED"
OUTCOME_SKIPPED_POLICY = "SKIPPED_POLICY"
OUTCOME_SKIPPED_RECOMMENDATION_MISSING = "SKIPPED_RECOMMENDATION_MISSING"
OUTCOME_SKIPPED_RECOMMENDATION_INVALID = "SKIPPED_RECOMMENDATION_INVALID"
OUTCOME_SKIPPED_EVIDENCE_INCOMPLETE = "SKIPPED_EVIDENCE_INCOMPLETE"
OUTCOME_SKIPPED_SNAPSHOT_MISMATCH = "SKIPPED_SNAPSHOT_MISMATCH"
OUTCOME_SKIPPED_VALUATION_UNAVAILABLE = "SKIPPED_VALUATION_UNAVAILABLE"
OUTCOME_REJECTED_BY_RISK = "REJECTED_BY_RISK"
OUTCOME_FAILED = "FAILED"
KNOWN_SYMBOL_ARM_OUTCOMES = (
    OUTCOME_EXECUTED, OUTCOME_INTENT_CREATED_PENDING_FILL, OUTCOME_AWAITING_OPERATOR_EXTERNAL_SUBMISSION,
    OUTCOME_SKIPPED_BOOK_DISABLED, OUTCOME_SKIPPED_POLICY,
    OUTCOME_SKIPPED_RECOMMENDATION_MISSING, OUTCOME_SKIPPED_RECOMMENDATION_INVALID,
    OUTCOME_SKIPPED_EVIDENCE_INCOMPLETE, OUTCOME_SKIPPED_SNAPSHOT_MISMATCH, OUTCOME_SKIPPED_VALUATION_UNAVAILABLE,
    OUTCOME_REJECTED_BY_RISK, OUTCOME_FAILED,
)

# The only outcomes that can be reached after `cash_ledger.open_book` has
# actually been called for this arm's book (see `_process_arm`) — every
# other outcome returns before the book is ever opened, so reconciling that
# book_id would raise `ValueError: unknown book_id` (no `paper_books` row
# exists yet). Reconciliation is only attempted for books this invocation
# actually touched.
_BOOK_OPENED_OUTCOMES = (
    OUTCOME_EXECUTED, OUTCOME_INTENT_CREATED_PENDING_FILL,
    OUTCOME_AWAITING_OPERATOR_EXTERNAL_SUBMISSION, OUTCOME_REJECTED_BY_RISK,
)


class ScheduledIntegrationError(RuntimeError):
    """Fail-closed error for the scheduled-cycle-to-paper-books integration
    entry point itself (disabled config, unknown cycle_id) — never raised
    for a single symbol/arm's own eligibility failure, which is always
    recorded as a bounded `SymbolArmOutcome` instead."""


def _atr_for_entry(price_provider: Any, symbol: str, as_of: datetime, period: int):
    if price_provider is None or not hasattr(price_provider, "get_price_history"):
        return None, None
    bars = price_provider.get_price_history(
        symbol, start=as_of.date() - timedelta(days=period * 4), end=as_of.date(), as_of=as_of,
    )
    if not bars or bars[-1].session_date != as_of.date():
        return None, None
    atr = average_true_range(tuple(
        OHLCBar(bar.session_date, bar.high, bar.low, bar.close) for bar in bars
    ), period=period)
    source_id = f"{bars[-1].provider}:{symbol}:{bars[-1].session_date.isoformat()}"
    return atr, source_id


@dataclass(frozen=True)
class SymbolArmOutcome:
    symbol: str
    arm: str
    book_id: str
    recommendation_id: str | None
    outcome: str
    reasons: tuple[str, ...] = ()
    risk_decision_id: str | None = None
    paper_order_intent_id: str | None = None
    fill_id: str | None = None
    market_simulation_input_source: str | None = None


@dataclass(frozen=True)
class PaperBookCycleIntegrationResult:
    cycle_id: str
    experiment_policy: str
    as_of: datetime
    symbol_outcomes: tuple[SymbolArmOutcome, ...]
    reconciliations: dict = field(default_factory=dict)  # book_id -> reconcile_book() dict


def _resolve_may_submit(
    policy_fn: Callable[..., bool], experiment_policy: str, baseline_enabled: bool, enhanced_enabled: bool,
) -> tuple[bool, str | None]:
    try:
        return policy_fn(
            experiment_policy, baseline_book_enabled=baseline_enabled, enhanced_book_enabled=enhanced_enabled,
        ), None
    except (ep.UnknownExperimentPolicyError, ep.UnsupportedExperimentPolicyError) as exc:
        return False, str(exc)


def _build_market_simulation_input(
    symbol: str, as_of: datetime, evidence_snapshot: EvidenceSnapshot | None, price_selection,
) -> tuple[execution.MarketSimulationInput | None, str | None]:
    """Deterministic, versioned local-simulation-input builder (docs/milestone-8.1.md
    Step 6). Never calls a live quote endpoint; never uses a price dated after
    `as_of`; never labels a modeled bid/ask as `OBSERVED`.

    Tier 1: a bid/ask pair already present in the shared, point-in-time
    `EvidenceSnapshot`'s own "market" evidence item (not populated by any
    existing evidence provider today, but checked generically/future-proof —
    never fabricated).

    Tier 2: the same point-in-time reference price
    `valuation.select_valuation_price` already selected for risk sizing,
    converted into a synthetic symmetric bid/ask using the existing
    `execution.py::DEFAULT_SLIPPAGE_BPS` as the half-spread proxy — the only
    existing configured spread/slippage numeric model in the paper-books
    execution module (no dedicated "spread" config field exists anywhere in
    this repository). Labeled `SIMULATED`, never `OBSERVED`.

    Tier 3: `(None, None)` — the caller creates the order intent but leaves
    it `PENDING_SUBMISSION`, never fabricating a fill.
    """
    if evidence_snapshot is not None:
        market_item = next(
            (
                item for item in evidence_snapshot.evidence_items
                if item.category == "market" and "bid" in item.normalized_values and "ask" in item.normalized_values
            ),
            None,
        )
        if market_item is not None and market_item.as_of <= as_of:
            bid = Decimal(str(market_item.normalized_values["bid"]))
            ask = Decimal(str(market_item.normalized_values["ask"]))
            if bid > 0 and ask >= bid:
                return execution.MarketSimulationInput(bid=bid, ask=ask), MARKET_SIMULATION_SOURCE_OBSERVED

    if (
        price_selection is not None and price_selection.price is not None
        and price_selection.status not in (VALUATION_POINT_IN_TIME_UNSAFE, VALUATION_SOURCE_UNAVAILABLE)
        and (price_selection.price_timestamp is None or price_selection.price_timestamp <= as_of)
    ):
        reference = price_selection.price
        half_spread = reference * execution.DEFAULT_SLIPPAGE_BPS / Decimal("20000")
        bid = reference - half_spread
        ask = reference + half_spread
        if bid > 0:
            return execution.MarketSimulationInput(bid=bid, ask=ask), MARKET_SIMULATION_SOURCE_SIMULATED

    return None, None


def _process_arm(
    conn, *, book_id: str, arm: str, cycle_id: str, symbol: str, as_of: datetime, recommendation_id: str | None,
    evidence_snapshot: EvidenceSnapshot | None, evidence_status: dict | None, cfg: PaperBooksConfiguration,
    may_submit: bool, policy_reason: str | None, price_provider: Any, clock: Callable[[], datetime],
    economic_events=None,
) -> SymbolArmOutcome:
    def outcome(code: str, *reasons: str, **extra) -> SymbolArmOutcome:
        return SymbolArmOutcome(
            symbol=symbol, arm=arm, book_id=book_id, recommendation_id=recommendation_id, outcome=code,
            reasons=tuple(reasons), **extra,
        )

    if not cfg.is_book_enabled(book_id):
        return outcome(OUTCOME_SKIPPED_BOOK_DISABLED, f"paper book {book_id!r} is not enabled in config/paper_books.yaml")
    if not may_submit:
        return outcome(OUTCOME_SKIPPED_POLICY, policy_reason or f"experiment policy does not permit the {arm} arm's isolated-book submission")
    if recommendation_id is None:
        return outcome(OUTCOME_SKIPPED_RECOMMENDATION_MISSING, f"no {arm.lower()} recommendation_id was recorded for this cycle/symbol")

    payload = load_recommendation(conn, recommendation_id)
    if payload is None:
        return outcome(OUTCOME_SKIPPED_RECOMMENDATION_MISSING, f"recommendation {recommendation_id!r} does not exist or is not frozen")
    if payload["symbol"] != symbol:
        return outcome(OUTCOME_SKIPPED_SNAPSHOT_MISMATCH, f"recommendation symbol {payload['symbol']!r} != cycle symbol {symbol!r}")
    rec_ts = datetime.fromisoformat(payload["ts"])
    if rec_ts > as_of:
        return outcome(OUTCOME_SKIPPED_SNAPSHOT_MISMATCH, f"recommendation ts {rec_ts.isoformat()} is after cycle as_of {as_of.isoformat()} — no future data")
    if payload["status"] != STATUS_ACTIVE or payload["side"] != SIDE_BUY_CANDIDATE:
        return outcome(OUTCOME_SKIPPED_RECOMMENDATION_INVALID, f"status={payload['status']!r} side={payload['side']!r} is not an actionable active buy_candidate")
    risk_plan = payload.get("risk_plan")
    if not risk_plan or not risk_plan.get("shares"):
        return outcome(OUTCOME_SKIPPED_RECOMMENDATION_INVALID, "recommendation has no positive risk_plan.shares")

    if evidence_snapshot is None:
        return outcome(OUTCOME_SKIPPED_SNAPSHOT_MISMATCH, "evidence snapshot for this cycle/symbol was not found")
    if evidence_snapshot.symbol != symbol:
        return outcome(OUTCOME_SKIPPED_SNAPSHOT_MISMATCH, f"evidence snapshot symbol {evidence_snapshot.symbol!r} != cycle symbol {symbol!r}")

    if evidence_status is None or evidence_status["screening_completeness"] != STATUS_COMPLETE_FOR_SCREENING:
        reason = (
            f"evidence screening_completeness={evidence_status['screening_completeness']!r} blocks paper-book submission"
            if evidence_status is not None else "no evidence-completeness record found for this cycle/symbol"
        )
        return outcome(OUTCOME_SKIPPED_EVIDENCE_INCOMPLETE, reason)

    book_def = cfg.book(book_id)
    book = cash_ledger.open_book(
        conn, book_id=book_id, starting_cash_usd=book_def.starting_cash_usd, config_hash=cfg.config_hash, clock=clock,
    )

    try:
        snap = valuation.build_portfolio_snapshot(
            conn, book_id, as_of, evidence_snapshots_by_symbol={symbol: evidence_snapshot}, price_provider=price_provider,
            maximum_price_age_seconds=cfg.valuation.maximum_price_age_seconds,
        )
    except Exception as exc:  # pragma: no cover - defensive; valuation.py does not currently raise
        return outcome(OUTCOME_SKIPPED_VALUATION_UNAVAILABLE, f"portfolio valuation raised: {exc}")

    context = risk_module.build_portfolio_context(conn, book_id, as_of, snap, symbol, Decimal("0"))
    price_selection = valuation.select_valuation_price(
        symbol, as_of, evidence_snapshot=evidence_snapshot, price_provider=price_provider,
        maximum_price_age_seconds=cfg.valuation.maximum_price_age_seconds,
    )
    entry_atr = None
    atr_source_id = None
    requested_quantity = Decimal(str(risk_plan["shares"]))
    if cfg.lifecycle.atr.enabled:
        entry_atr, atr_source_id = _atr_for_entry(
            price_provider, symbol, as_of, cfg.lifecycle.atr.period,
        )
        if entry_atr is None or price_selection.price is None or entry_atr <= 0:
            return outcome(OUTCOME_REJECTED_BY_RISK, "ATR_UNAVAILABLE_OR_STALE")
        atr_percent = entry_atr / price_selection.price
        if not (cfg.lifecycle.atr.minimum_atr_percent <= atr_percent <= cfg.lifecycle.atr.maximum_atr_percent):
            return outcome(
                OUTCOME_REJECTED_BY_RISK,
                f"ATR_PERCENT_OUT_OF_BOUNDS:{atr_percent}",
            )
        if context.net_liquidation_value_usd is None:
            return outcome(OUTCOME_REJECTED_BY_RISK, "ATR_RISK_PLAN_REQUIRES_COMPLETE_EQUITY")
        try:
            atr_plan = compute_atr_position_plan(
                account_equity=context.net_liquidation_value_usd,
                settled_cash=context.available_cash_usd, entry_price=price_selection.price,
                atr=entry_atr, risk_fraction=Decimal("0.01"),
                max_position_fraction=cfg.risk.max_position_weight,
                initial_stop_multiple=cfg.lifecycle.atr.initial_stop_multiple,
                initial_target_multiple=cfg.lifecycle.atr.initial_target_multiple,
                minimum_atr_percent=cfg.lifecycle.atr.minimum_atr_percent,
                maximum_atr_percent=cfg.lifecycle.atr.maximum_atr_percent,
            )
            requested_quantity = min(requested_quantity, atr_plan.shares)
        except IncompleteStateError as exc:
            return outcome(OUTCOME_REJECTED_BY_RISK, f"ATR_RISK_PLAN_REJECTED:{exc}")

    authoritative_enabled = "max_daily_loss_fraction" in cfg.raw.get("paper_books", {}).get("risk", {})
    daily_state = None
    if authoritative_enabled:
        try:
            reconciliation.reconcile_book(conn, book_id, as_of)
            daily_state = daily_risk_module.calculate_and_persist_daily_risk_state(
                conn, book_id=book_id, market_date=as_of.date(), as_of=as_of,
                config_hash=cfg.config_hash,
                require_reconciled=cfg.risk.require_reconciled_risk_state,
            )
        except daily_risk_module.DailyRiskStateError:
            daily_state = None
    blackout_decision = evaluate_economic_event_blackout(
        as_of=as_of, events=economic_events,
        configuration=cfg.lifecycle.economic_event_blackout,
    )
    if authoritative_enabled:
        pb_repo.save_economic_blackout_decision(
            conn, book_id=book_id, order_evaluation_id=recommendation_id,
            as_of=as_of, decision=blackout_decision, created_at=clock(),
        )

    decision = risk_module.evaluate_paper_risk(
        book_status="PAUSED" if safety_pause.is_paused(conn, book_id) else book.status,
        experiment_arm=book.experiment_arm, expected_arm=arm, context=context,
        requested_quantity_hint=requested_quantity, reference_price=price_selection.price,
        reference_price_age_seconds=price_selection.staleness_seconds,
        reference_price_point_in_time_safe=price_selection.point_in_time_safe, risk_config=cfg.risk,
        daily_risk_state=daily_state, economic_blackout_decision=blackout_decision,
        enforce_authoritative_state=authoritative_enabled,
    )
    risk_decision_id = order_intent.persist_risk_decision(
        conn, book_id, cycle_id, recommendation_id, symbol, decision, snap.snapshot_id, clock,
    )

    if daily_state is not None and decision.decision in (
        RISK_REJECTED_DAILY_LOSS_LIMIT, RISK_REJECTED_DRAWDOWN_LIMIT,
    ):
        safety_pause.pause_for_risk_state(
            conn, book_id=book_id, reason_code=decision.decision,
            risk_state_id=daily_state.risk_state_id,
            reason="; ".join(decision.reasons), at=clock(),
        )

    if decision.decision not in APPROVED_RISK_DECISIONS:
        return outcome(OUTCOME_REJECTED_BY_RISK, *decision.reasons, risk_decision_id=risk_decision_id)

    intent = order_intent.build_order_intent(
        book_id=book_id, experiment_arm=arm, cycle_id=cycle_id, recommendation_id=recommendation_id, symbol=symbol,
        risk_decision=decision, risk_decision_id=risk_decision_id, portfolio_snapshot_id=snap.snapshot_id,
        config_hash=cfg.config_hash, as_of=as_of, clock=clock,
    )
    assert intent is not None  # decision.decision is one of APPROVED_RISK_DECISIONS

    if cfg.external_broker.enabled and book_id in cfg.external_broker.enabled_book_ids:
        order_intent.persist_order_intent(conn, intent)
        queue_id = "peqs_" + hashlib.sha256(
            f"{book_id}:{intent.paper_order_intent_id}:scheduled".encode()
        ).hexdigest()[:40]
        pb_repo.enqueue_external_submission(
            conn, queue_id=queue_id, book_id=book_id,
            paper_order_intent_id=intent.paper_order_intent_id, source="RECURRING_LOCAL_PAPER",
            created_at=clock().isoformat(),
        )
        return outcome(
            OUTCOME_AWAITING_OPERATOR_EXTERNAL_SUBMISSION,
            "external-enabled intent awaits explicit operator preview and submission; scheduler made no broker call",
            risk_decision_id=risk_decision_id, paper_order_intent_id=intent.paper_order_intent_id,
        )

    market_input, source = _build_market_simulation_input(symbol, as_of, evidence_snapshot, price_selection)
    if market_input is None:
        order_intent.persist_order_intent(conn, intent)
        return outcome(
            OUTCOME_INTENT_CREATED_PENDING_FILL, MARKET_SIMULATION_INPUT_UNAVAILABLE, risk_decision_id=risk_decision_id,
            paper_order_intent_id=intent.paper_order_intent_id,
        )

    submit_result = execution.submit_and_simulate(conn, intent, market_input, clock())
    fill = submit_result.get("fill")
    if fill and entry_atr is not None and atr_source_id is not None:
        state = lifecycle_state_module.create_entry_lifecycle_state(
            book_id=book_id, symbol=symbol, originating_intent_id=intent.paper_order_intent_id,
            entry_fill_id=fill["fill_id"], opened_at=fill["fill_timestamp"],
            quantity=fill["fill_quantity"], average_entry_price=fill["fill_price"],
            entry_atr=entry_atr, atr_period=cfg.lifecycle.atr.period,
            initial_stop_multiple=cfg.lifecycle.atr.initial_stop_multiple,
            initial_target_multiple=cfg.lifecycle.atr.initial_target_multiple,
            policy_version=lifecycle_state_module.LIFECYCLE_POLICY_VERSION,
            config_hash=cfg.config_hash, source_market_data_id=atr_source_id,
        )
        pb_repo.save_position_lifecycle_state(conn, state)
    final_outcome = OUTCOME_EXECUTED if submit_result["status"] == execution.INTENT_STATUS_FILLED else OUTCOME_INTENT_CREATED_PENDING_FILL
    return outcome(
        final_outcome, risk_decision_id=risk_decision_id, paper_order_intent_id=intent.paper_order_intent_id,
        fill_id=fill["fill_id"] if fill else None, market_simulation_input_source=source,
    )


def integrate_scheduled_cycle_into_paper_books(
    conn, *, cycle_id: str, experiment_policy: str, paper_books_config: PaperBooksConfiguration,
    clock: Callable[[], datetime], price_provider: Any = None, economic_events=None,
) -> PaperBookCycleIntegrationResult:
    """Deterministic entry point closing the Milestone 8.1 integration gap.

    Reads only already-persisted scheduled-cycle output (never constructs a
    recommendation or evidence). Fails closed with `ScheduledIntegrationError`
    when `paper_books_config.enabled` or
    `paper_books_config.scheduled_integration.enabled` is false, or when
    `cycle_id` has no persisted `research_cycles` row — every other failure
    mode is a per-symbol/per-arm bounded `SymbolArmOutcome`, never an
    unclassified exception.
    """
    if not paper_books_config.enabled:
        raise ScheduledIntegrationError("paper_books.enabled is false — scheduled integration fails closed")
    if not paper_books_config.scheduled_integration.enabled:
        raise ScheduledIntegrationError(
            "paper_books.scheduled_integration.enabled is false — scheduled integration fails closed"
        )

    cycle_repo = SQLiteResearchCycleRepository(conn)
    cycle = cycle_repo.get_cycle(cycle_id)
    if cycle is None:
        raise ScheduledIntegrationError(f"unknown cycle_id {cycle_id!r} — no persisted scheduled-cycle record exists")
    as_of = datetime.fromisoformat(cycle["as_of"])

    baseline_book_id = paper_books_config.baseline.book_id
    enhanced_book_id = paper_books_config.enhanced.book_id
    baseline_enabled = paper_books_config.is_book_enabled(baseline_book_id)
    enhanced_enabled = paper_books_config.is_book_enabled(enhanced_book_id)

    may_baseline, baseline_policy_reason = _resolve_may_submit(
        ep.may_submit_baseline_to_paper_book, experiment_policy, baseline_enabled, enhanced_enabled,
    )
    may_enhanced, enhanced_policy_reason = _resolve_may_submit(
        ep.may_submit_enhanced_to_paper_book, experiment_policy, baseline_enabled, enhanced_enabled,
    )

    outcomes: list[SymbolArmOutcome] = []
    touched_books: set[str] = set()

    for row in cycle_repo.list_symbol_results(cycle_id):
        symbol = row["symbol"]
        baseline_rec_id = row["baseline_recommendation_id"]
        enhanced_rec_id = row["enhanced_recommendation_id"]
        snapshot_id = row["snapshot_id"]

        if row["status"] != "COMPLETED":
            reason = f"symbol cycle status is {row['status']!r}, not COMPLETED"
            outcomes.append(SymbolArmOutcome(symbol, "BASELINE", baseline_book_id, baseline_rec_id, OUTCOME_SKIPPED_RECOMMENDATION_MISSING, (reason,)))
            outcomes.append(SymbolArmOutcome(symbol, "ENHANCED", enhanced_book_id, enhanced_rec_id, OUTCOME_SKIPPED_RECOMMENDATION_MISSING, (reason,)))
            continue

        # Deterministic intent IDs are precomputed here — before any risk
        # evaluation or execution — so the immutable assignment row can
        # carry both arms' intent identity from the moment it is first
        # written (docs/milestone-8.1.md Step 4's "smallest safe approach").
        baseline_intent_id = (
            derive_paper_order_intent_id(baseline_rec_id, baseline_book_id, order_intent.EXECUTION_VERSION)
            if baseline_rec_id else None
        )
        enhanced_intent_id = (
            derive_paper_order_intent_id(enhanced_rec_id, enhanced_book_id, order_intent.EXECUTION_VERSION)
            if enhanced_rec_id else None
        )
        experiment_id = row["experiment_id"] or f"cycle-only:{cycle_id}:{symbol}"
        save_assignment(
            conn,
            PaperBookExperimentAssignment(
                experiment_id=experiment_id, cycle_id=cycle_id, symbol=symbol, as_of=as_of,
                evidence_snapshot_id=snapshot_id, baseline_recommendation_id=baseline_rec_id,
                enhanced_recommendation_id=enhanced_rec_id, baseline_book_id=baseline_book_id,
                enhanced_book_id=enhanced_book_id, baseline_intent_id=baseline_intent_id,
                enhanced_intent_id=enhanced_intent_id,
            ),
            clock=clock,
        )

        evidence_snapshot = load_evidence_snapshot(conn, snapshot_id) if snapshot_id else None
        evidence_status = load_symbol_evidence_status(conn, cycle_id, symbol)

        for arm, book_id, rec_id, may_submit, policy_reason in (
            ("BASELINE", baseline_book_id, baseline_rec_id, may_baseline, baseline_policy_reason),
            ("ENHANCED", enhanced_book_id, enhanced_rec_id, may_enhanced, enhanced_policy_reason),
        ):
            try:
                symbol_outcome = _process_arm(
                    conn, book_id=book_id, arm=arm, cycle_id=cycle_id, symbol=symbol, as_of=as_of,
                    recommendation_id=rec_id, evidence_snapshot=evidence_snapshot, evidence_status=evidence_status,
                    cfg=paper_books_config, may_submit=may_submit, policy_reason=policy_reason,
                    price_provider=price_provider, clock=clock, economic_events=economic_events,
                )
            except Exception as exc:  # per-arm failure isolation — never loses the whole cycle's other arms/symbols
                symbol_outcome = SymbolArmOutcome(
                    symbol=symbol, arm=arm, book_id=book_id, recommendation_id=rec_id, outcome=OUTCOME_FAILED,
                    reasons=(f"paper-book integration exception: {exc}",),
                )
            outcomes.append(symbol_outcome)
            if symbol_outcome.outcome in _BOOK_OPENED_OUTCOMES:
                touched_books.add(book_id)

    reconciliations = {
        book_id: reconciliation.reconcile_book(conn, book_id, as_of) for book_id in sorted(touched_books)
    }

    return PaperBookCycleIntegrationResult(
        cycle_id=cycle_id, experiment_policy=experiment_policy, as_of=as_of, symbol_outcomes=tuple(outcomes),
        reconciliations=reconciliations,
    )
