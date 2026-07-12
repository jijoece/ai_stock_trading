"""Deterministic, fail-closed position sizing and trade risk math.

Every number a trade plan needs is computed here from explicit inputs. The
LLM may explain these values; it must never supply, adjust, or override them
(architecture §16). Any missing or stale critical input raises
IncompleteStateError — callers convert that to an ANALYSIS_INCOMPLETE
recommendation, never a default. A fully-known state that fails a policy
gate (earnings window, sector cap, daily-loss breach, ...) is NOT an error —
it returns a zero-share PositionPlan with `no_action_reason` set, which
callers convert to a NO_ACTION recommendation.

Used only for paper trading in the current phase.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


class IncompleteStateError(RuntimeError):
    """Critical state is unknown or stale — no plan may be produced."""


# Machine-readable reasons a fully-known request still produces zero shares.
# Paired with a human-readable string in PositionPlan.warnings.
NO_ACTION_STOP_AT_OR_ABOVE_ENTRY = "stop_at_or_above_entry"
NO_ACTION_EARNINGS_WINDOW = "earnings_window"
NO_ACTION_SECTOR_CONCENTRATION = "sector_concentration"
NO_ACTION_DUPLICATE_POSITION = "duplicate_position"
NO_ACTION_PORTFOLIO_EXPOSURE = "portfolio_exposure_breach"
NO_ACTION_DAILY_LOSS_BREACH = "daily_loss_breach"
NO_ACTION_DRAWDOWN_BREACH = "drawdown_breach"
NO_ACTION_WIDE_SPREAD = "wide_spread"
NO_ACTION_REWARD_RISK_BELOW_FLOOR = "reward_risk_below_floor"
NO_ACTION_ZERO_SHARES_AT_CAPS = "zero_shares_at_caps"


@dataclass(frozen=True)
class RiskInputs:
    account_equity: float | None
    settled_cash: float | None
    entry_price: float | None
    stop_price: float | None
    price_as_of_epoch: float | None
    now_epoch: float | None

    # Portfolio/account state — required (None fails closed), distinct from
    # per-trade market data. `existing_position_shares` is explicitly an int
    # (0 is a valid, known "flat" answer; None means "unknown").
    existing_position_shares: int | None = None
    portfolio_exposure_fraction: float | None = None
    account_state_as_of_epoch: float | None = None

    # Policy (defaults per architecture §16; all overridable via config)
    risk_fraction: float = 0.01           # max fraction of equity risked per trade
    max_position_fraction: float = 0.05   # max fraction of equity in one position
    min_reward_risk: float = 2.0          # required R:R floor
    max_price_staleness_seconds: float = 900.0
    max_account_staleness_seconds: float = 3600.0

    # Liquidity guard (optional but recommended)
    avg_daily_dollar_volume: float | None = None
    max_adv_fraction: float = 0.01        # position ≤ 1% of avg daily dollar volume

    # Earnings guard: days until next confirmed earnings; None = unknown.
    days_to_earnings: float | None = None
    min_days_to_earnings: float = 3.0
    earnings_date_known: bool = False

    # Concentration guards
    sector: str = ""
    sector_exposure_fraction: float = 0.0  # current fraction of equity in this sector
    max_sector_fraction: float = 0.25
    max_portfolio_exposure_fraction: float = 0.90

    # Circuit breakers — permissive defaults (0.0 == "no breach observed"),
    # consistent with sector_exposure_fraction's existing default: these are
    # policy gates fed by the portfolio snapshot, not critical per-trade
    # unknowns, so an unsupplied value does not fail closed.
    current_daily_loss_fraction: float = 0.0   # negative = loss, e.g. -0.02 = -2%
    max_daily_loss_fraction: float = 0.03
    current_drawdown_fraction: float = 0.0     # negative = below peak equity
    max_drawdown_fraction: float = 0.15

    # Spread guard
    current_spread_bps: float = 0.0
    max_spread_bps: float = 100.0

    # Duplicate-entry guard
    allow_averaging_into_existing_position: bool = False

    # Optional externally-supplied target (e.g. technical resistance level).
    # When absent (default), the target is derived from min_reward_risk and
    # always satisfies the floor by construction.
    technical_target_price: float | None = None


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
    no_action_reason: str | None = None

    @property
    def actionable(self) -> bool:
        return self.shares > 0


def _require(value: float | None, name: str) -> float:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        raise IncompleteStateError(f"{name} is unknown — fail closed (NO_ACTION)")
    return float(value)


def _require_int(value: int | None, name: str) -> int:
    if value is None:
        raise IncompleteStateError(f"{name} is unknown — fail closed (NO_ACTION)")
    return int(value)


def compute_position_plan(inputs: RiskInputs) -> PositionPlan:
    """Compute a long-side plan. Raises IncompleteStateError on unknown state.

    Returns a plan with shares == 0 (not an exception) when state is fully
    known but policy forbids entry — that is a valid, auditable NO_ACTION,
    tagged with `no_action_reason`.
    """
    equity = _require(inputs.account_equity, "account_equity")
    settled = _require(inputs.settled_cash, "settled_cash")
    entry = _require(inputs.entry_price, "entry_price")
    stop = _require(inputs.stop_price, "stop_price")
    as_of = _require(inputs.price_as_of_epoch, "price_as_of_epoch")
    now = _require(inputs.now_epoch, "now_epoch")
    existing_shares = _require_int(inputs.existing_position_shares, "existing_position_shares")
    portfolio_exposure = _require(inputs.portfolio_exposure_fraction, "portfolio_exposure_fraction")
    account_as_of = _require(inputs.account_state_as_of_epoch, "account_state_as_of_epoch")

    if equity <= 0:
        raise IncompleteStateError("account_equity is non-positive")
    if entry <= 0:
        raise IncompleteStateError("entry_price is non-positive")
    if now - as_of > inputs.max_price_staleness_seconds:
        raise IncompleteStateError(
            f"market data stale: {now - as_of:.0f}s old exceeds "
            f"{inputs.max_price_staleness_seconds:.0f}s limit"
        )
    if now - account_as_of > inputs.max_account_staleness_seconds:
        raise IncompleteStateError(
            f"account state stale: {now - account_as_of:.0f}s old exceeds "
            f"{inputs.max_account_staleness_seconds:.0f}s limit"
        )
    if not inputs.earnings_date_known:
        raise IncompleteStateError("earnings date unknown — cannot assess earnings risk")

    def no_action(reason: str, message: str) -> PositionPlan:
        return PositionPlan(warnings=(message,), no_action_reason=reason)

    # Policy gates that legitimately produce a zero-share NO_ACTION plan —
    # fully-known state, deterministic rejection, not an error.
    if stop >= entry:
        return no_action(NO_ACTION_STOP_AT_OR_ABOVE_ENTRY, "stop at or above entry — no valid long setup")
    if inputs.days_to_earnings is not None and inputs.days_to_earnings < inputs.min_days_to_earnings:
        return no_action(
            NO_ACTION_EARNINGS_WINDOW,
            f"earnings in {inputs.days_to_earnings:.1f}d < {inputs.min_days_to_earnings}d minimum",
        )
    if inputs.sector_exposure_fraction >= inputs.max_sector_fraction:
        return no_action(
            NO_ACTION_SECTOR_CONCENTRATION,
            f"sector exposure {inputs.sector_exposure_fraction:.0%} ≥ cap "
            f"{inputs.max_sector_fraction:.0%}" + (f" ({inputs.sector})" if inputs.sector else ""),
        )
    if existing_shares > 0 and not inputs.allow_averaging_into_existing_position:
        return no_action(
            NO_ACTION_DUPLICATE_POSITION,
            f"already holding {existing_shares} shares — averaging in is not enabled",
        )
    if portfolio_exposure >= inputs.max_portfolio_exposure_fraction:
        return no_action(
            NO_ACTION_PORTFOLIO_EXPOSURE,
            f"portfolio exposure {portfolio_exposure:.0%} ≥ cap {inputs.max_portfolio_exposure_fraction:.0%}",
        )
    if inputs.current_daily_loss_fraction <= -inputs.max_daily_loss_fraction:
        return no_action(
            NO_ACTION_DAILY_LOSS_BREACH,
            f"daily loss {inputs.current_daily_loss_fraction:.2%} breached "
            f"limit {-inputs.max_daily_loss_fraction:.2%} — new entries halted",
        )
    if inputs.current_drawdown_fraction <= -inputs.max_drawdown_fraction:
        return no_action(
            NO_ACTION_DRAWDOWN_BREACH,
            f"drawdown {inputs.current_drawdown_fraction:.2%} breached "
            f"limit {-inputs.max_drawdown_fraction:.2%} — new entries halted",
        )
    if inputs.current_spread_bps > inputs.max_spread_bps:
        return no_action(
            NO_ACTION_WIDE_SPREAD,
            f"spread {inputs.current_spread_bps:.0f}bps exceeds cap {inputs.max_spread_bps:.0f}bps",
        )

    risk_per_share = entry - stop

    if inputs.technical_target_price is not None:
        implied_rr = (inputs.technical_target_price - entry) / risk_per_share
        if implied_rr < inputs.min_reward_risk:
            return no_action(
                NO_ACTION_REWARD_RISK_BELOW_FLOOR,
                f"technical target implies R:R {implied_rr:.2f} below floor {inputs.min_reward_risk:.2f}",
            )

    max_risk_dollars = equity * inputs.risk_fraction

    shares = math.floor(max_risk_dollars / risk_per_share)

    warnings: list[str] = []

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
        return PositionPlan(
            warnings=tuple(warnings) or ("no affordable size at policy limits",),
            no_action_reason=NO_ACTION_ZERO_SHARES_AT_CAPS,
        )

    target = inputs.technical_target_price if inputs.technical_target_price is not None else (
        entry + inputs.min_reward_risk * risk_per_share
    )
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
