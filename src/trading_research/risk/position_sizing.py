"""Deterministic, fail-closed position sizing and trade risk math.

Every number a trade plan needs is computed here from explicit inputs. The
LLM may explain these values; it must never supply, adjust, or override them
(architecture §16). Any missing or stale critical input raises
IncompleteStateError — callers convert that to an ANALYSIS_INCOMPLETE
recommendation, never a default.

Used only for paper trading in the current phase.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


class IncompleteStateError(RuntimeError):
    """Critical state is unknown or stale — no plan may be produced."""


@dataclass(frozen=True)
class RiskInputs:
    account_equity: float | None
    settled_cash: float | None
    entry_price: float | None
    stop_price: float | None
    price_as_of_epoch: float | None
    now_epoch: float | None

    # Policy (defaults per architecture §16; all overridable via config)
    risk_fraction: float = 0.01           # max fraction of equity risked per trade
    max_position_fraction: float = 0.05   # max fraction of equity in one position
    min_reward_risk: float = 2.0          # required R:R floor
    max_price_staleness_seconds: float = 900.0

    # Liquidity guard (optional but recommended)
    avg_daily_dollar_volume: float | None = None
    max_adv_fraction: float = 0.01        # position ≤ 1% of avg daily dollar volume

    # Earnings guard: days until next confirmed earnings; None = unknown.
    days_to_earnings: float | None = None
    min_days_to_earnings: float = 3.0
    earnings_date_known: bool = False

    # Concentration guard
    sector: str = ""
    sector_exposure_fraction: float = 0.0  # current fraction of equity in this sector
    max_sector_fraction: float = 0.25


@dataclass(frozen=True)
class PositionPlan:
    symbol_agnostic: bool = field(default=True, repr=False)
    shares: int = 0
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    risk_per_share: float = 0.0
    dollars_at_risk: float = 0.0
    position_value: float = 0.0
    reward_risk: float = 0.0
    warnings: tuple[str, ...] = ()

    @property
    def actionable(self) -> bool:
        return self.shares > 0


def _require(value: float | None, name: str) -> float:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        raise IncompleteStateError(f"{name} is unknown — fail closed (NO_ACTION)")
    return float(value)


def compute_position_plan(inputs: RiskInputs) -> PositionPlan:
    """Compute a long-side plan. Raises IncompleteStateError on unknown state.

    Returns a plan with shares == 0 (not an exception) when state is fully
    known but policy forbids entry — that is a valid, auditable NO_ACTION.
    """
    equity = _require(inputs.account_equity, "account_equity")
    settled = _require(inputs.settled_cash, "settled_cash")
    entry = _require(inputs.entry_price, "entry_price")
    stop = _require(inputs.stop_price, "stop_price")
    as_of = _require(inputs.price_as_of_epoch, "price_as_of_epoch")
    now = _require(inputs.now_epoch, "now_epoch")

    if equity <= 0:
        raise IncompleteStateError("account_equity is non-positive")
    if entry <= 0:
        raise IncompleteStateError("entry_price is non-positive")
    if now - as_of > inputs.max_price_staleness_seconds:
        raise IncompleteStateError(
            f"market data stale: {now - as_of:.0f}s old exceeds "
            f"{inputs.max_price_staleness_seconds:.0f}s limit"
        )
    if not inputs.earnings_date_known:
        raise IncompleteStateError("earnings date unknown — cannot assess earnings risk")

    warnings: list[str] = []

    # Policy gates that legitimately produce a zero-share NO_ACTION plan.
    if stop >= entry:
        return PositionPlan(warnings=("stop at or above entry — no valid long setup",))
    if inputs.days_to_earnings is not None and inputs.days_to_earnings < inputs.min_days_to_earnings:
        return PositionPlan(
            warnings=(f"earnings in {inputs.days_to_earnings:.1f}d < {inputs.min_days_to_earnings}d minimum",)
        )
    if inputs.sector_exposure_fraction >= inputs.max_sector_fraction:
        return PositionPlan(
            warnings=(
                f"sector exposure {inputs.sector_exposure_fraction:.0%} ≥ cap "
                f"{inputs.max_sector_fraction:.0%}" + (f" ({inputs.sector})" if inputs.sector else ""),
            )
        )

    risk_per_share = entry - stop
    max_risk_dollars = equity * inputs.risk_fraction

    shares = math.floor(max_risk_dollars / risk_per_share)

    # Cap by max position value.
    max_position_value = equity * inputs.max_position_fraction
    if shares * entry > max_position_value:
        shares = math.floor(max_position_value / entry)
        warnings.append("size capped by max position fraction")

    # Cap by settled cash (T+1 discipline: unsettled cash is not spendable).
    if shares * entry > settled:
        shares = math.floor(settled / entry)
        warnings.append("size capped by settled cash")

    # Cap by liquidity.
    if inputs.avg_daily_dollar_volume is not None:
        max_liquidity_value = inputs.avg_daily_dollar_volume * inputs.max_adv_fraction
        if shares * entry > max_liquidity_value:
            shares = math.floor(max_liquidity_value / entry)
            warnings.append("size capped by liquidity (fraction of avg daily dollar volume)")

    if shares <= 0:
        return PositionPlan(warnings=tuple(warnings) or ("no affordable size at policy limits",))

    target = entry + inputs.min_reward_risk * risk_per_share
    dollars_at_risk = shares * risk_per_share
    reward_risk = (target - entry) / risk_per_share

    return PositionPlan(
        shares=shares,
        entry_price=entry,
        stop_price=stop,
        target_price=round(target, 4),
        risk_per_share=round(risk_per_share, 4),
        dollars_at_risk=round(dollars_at_risk, 2),
        position_value=round(shares * entry, 2),
        reward_risk=round(reward_risk, 2),
        warnings=tuple(warnings),
    )
