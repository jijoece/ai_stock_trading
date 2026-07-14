"""Deterministic, per-book paper risk policy (docs/milestone-8.md Steps 10-11).

Pure functions over typed, already-computed inputs — never touches Claude
output, never selects a book, never overrides itself. The same recommendation
fed to two different `PaperPortfolioContext` values (one per book) can
legitimately produce two different approved quantities; the reason is always
a persisted, deterministic `PaperRiskDecision.reasons` tuple (ADR 0006
Decision 7).
"""
from __future__ import annotations

from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from ..storage import paper_books_repositories as repo
from .config import RiskSection
from .models import (
    POLICY_VERSION,
    RISK_APPROVED,
    RISK_APPROVED_REDUCED,
    RISK_REJECTED_ARM_MISMATCH,
    RISK_REJECTED_BOOK_PAUSED,
    RISK_REJECTED_DAILY_NOTIONAL_LIMIT,
    RISK_REJECTED_INSUFFICIENT_CASH,
    RISK_REJECTED_INVALID_RECOMMENDATION,
    RISK_REJECTED_MAX_OPEN_POSITIONS,
    RISK_REJECTED_MAX_POSITION_WEIGHT,
    RISK_REJECTED_MAX_SYMBOL_CONCENTRATION,
    RISK_REJECTED_MISSING_PRICE,
    RISK_REJECTED_STALE_PRICE,
    VALUATION_COMPLETE,
    VALUATION_PARTIAL_STALE_PRICE,
    BOOK_STATUS_ACTIVE,
    PaperPortfolioContext,
    PaperRiskDecision,
)


def build_portfolio_context(
    conn, book_id: str, as_of: datetime, snapshot, symbol: str, daily_new_notional_usd: Decimal
) -> PaperPortfolioContext:
    position = repo.load_position(conn, book_id, symbol)
    quantity = Decimal(position["quantity"]) if position else Decimal("0")
    market_value = None
    weight = None
    if snapshot.net_liquidation_value_usd is not None:
        snap_positions = repo.list_snapshot_positions(conn, book_id, snapshot.snapshot_id)
        row = next((r for r in snap_positions if r["symbol"] == symbol), None)
        if row is not None and row["market_value_usd"] is not None:
            market_value = Decimal(row["market_value_usd"])
            if snapshot.net_liquidation_value_usd != 0:
                weight = market_value / snapshot.net_liquidation_value_usd

    return PaperPortfolioContext(
        book_id=book_id, as_of=as_of, available_cash_usd=snapshot.cash_available_usd,
        reserved_cash_usd=snapshot.cash_reserved_usd, net_liquidation_value_usd=snapshot.net_liquidation_value_usd,
        current_position_quantity=quantity, current_position_market_value_usd=market_value,
        current_position_weight=weight, open_position_count=snapshot.position_count,
        daily_new_notional_usd=daily_new_notional_usd, valuation_status=snapshot.valuation_status,
    )


def evaluate_paper_risk(
    *, book_status: str, experiment_arm: str, expected_arm: str, context: PaperPortfolioContext,
    requested_quantity_hint: Decimal | None, reference_price: Decimal | None,
    reference_price_age_seconds: int | None, reference_price_point_in_time_safe: bool | None,
    risk_config: RiskSection, policy_version: str = POLICY_VERSION,
) -> PaperRiskDecision:
    def rejected(decision: str, *reasons: str) -> PaperRiskDecision:
        return PaperRiskDecision(
            decision=decision, requested_notional_usd=None, approved_notional_usd=None,
            approved_quantity=None, reasons=tuple(reasons), policy_version=policy_version,
        )

    if experiment_arm != expected_arm:
        return rejected(
            RISK_REJECTED_ARM_MISMATCH,
            f"experiment_arm {experiment_arm!r} does not match this book's expected arm {expected_arm!r}",
        )
    if book_status != BOOK_STATUS_ACTIVE:
        return rejected(RISK_REJECTED_BOOK_PAUSED, f"book status is {book_status!r}, not ACTIVE")
    if reference_price is None or reference_price <= 0:
        return rejected(RISK_REJECTED_MISSING_PRICE, "reference_price is unavailable")
    if reference_price_point_in_time_safe is False:
        return rejected(RISK_REJECTED_STALE_PRICE, "reference price is not point-in-time safe")
    if reference_price_age_seconds is not None and reference_price_age_seconds > risk_config.reject_stale_market_price_seconds:
        return rejected(
            RISK_REJECTED_STALE_PRICE,
            f"reference price age {reference_price_age_seconds}s exceeds "
            f"reject_stale_market_price_seconds={risk_config.reject_stale_market_price_seconds}s",
        )
    if requested_quantity_hint is None or requested_quantity_hint <= 0:
        return rejected(RISK_REJECTED_INVALID_RECOMMENDATION, "requested quantity hint is missing or non-positive")
    if context.valuation_status not in (VALUATION_COMPLETE, VALUATION_PARTIAL_STALE_PRICE) \
            or context.net_liquidation_value_usd is None:
        return rejected(
            RISK_REJECTED_MISSING_PRICE,
            f"book portfolio valuation_status={context.valuation_status} — cannot safely size an order",
        )

    is_new_position = context.current_position_quantity == 0
    if is_new_position and context.open_position_count >= risk_config.max_open_positions:
        return rejected(
            RISK_REJECTED_MAX_OPEN_POSITIONS,
            f"open_position_count {context.open_position_count} >= max_open_positions {risk_config.max_open_positions}",
        )

    requested_notional = requested_quantity_hint * reference_price
    current_position_value = context.current_position_market_value_usd or Decimal("0")

    max_position_notional = context.net_liquidation_value_usd * risk_config.max_position_weight
    remaining_position_capacity = max(Decimal("0"), max_position_notional - current_position_value)

    max_symbol_notional = context.net_liquidation_value_usd * risk_config.max_symbol_concentration_weight
    remaining_symbol_capacity = max(Decimal("0"), max_symbol_notional - current_position_value)

    remaining_daily_capacity = max(Decimal("0"), risk_config.max_daily_new_notional_usd - context.daily_new_notional_usd)

    min_cash_buffer = context.net_liquidation_value_usd * risk_config.minimum_cash_buffer_weight
    cash_capacity = max(Decimal("0"), context.available_cash_usd - min_cash_buffer)

    caps = (
        (cash_capacity, RISK_REJECTED_INSUFFICIENT_CASH, f"available cash capacity {cash_capacity} (after minimum_cash_buffer_weight)"),
        (remaining_position_capacity, RISK_REJECTED_MAX_POSITION_WEIGHT, f"remaining max_position_weight capacity {remaining_position_capacity}"),
        (remaining_symbol_capacity, RISK_REJECTED_MAX_SYMBOL_CONCENTRATION, f"remaining max_symbol_concentration_weight capacity {remaining_symbol_capacity}"),
        (remaining_daily_capacity, RISK_REJECTED_DAILY_NOTIONAL_LIMIT, f"remaining max_daily_new_notional_usd capacity {remaining_daily_capacity}"),
        (risk_config.max_order_notional_usd, None, f"max_order_notional_usd cap {risk_config.max_order_notional_usd}"),
    )

    approved_notional = min(requested_notional, *(cap for cap, _, _ in caps))
    approved_quantity = (approved_notional / reference_price).to_integral_value(rounding=ROUND_FLOOR)

    if approved_quantity <= 0:
        binding = min(caps, key=lambda c: c[0])
        binding_reason_code = binding[1] or RISK_REJECTED_INSUFFICIENT_CASH
        return rejected(binding_reason_code, binding[2], "approved_quantity rounds down to zero whole shares")

    final_notional = approved_quantity * reference_price
    binding_reasons = tuple(text for cap, _, text in caps if cap <= requested_notional)
    requested_quantity_floor = requested_quantity_hint.to_integral_value(rounding=ROUND_FLOOR)
    decision = RISK_APPROVED if approved_quantity == requested_quantity_floor else RISK_APPROVED_REDUCED

    return PaperRiskDecision(
        decision=decision, requested_notional_usd=requested_notional, approved_notional_usd=final_notional,
        approved_quantity=approved_quantity, reasons=binding_reasons, policy_version=policy_version,
    )
