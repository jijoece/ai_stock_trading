"""Recommendation eligibility for paper execution (Milestone 3, Step 4).

`evaluate_eligibility` is the single gate a recommendation must clear before
`intent_builder.build_paper_order_intent` is ever called. It never mutates
anything and never raises for an ordinary ineligibility — every rejection is
recorded as a reason string on the returned `PaperExecutionEligibility`
rather than reduced to a bare boolean, and evaluation is deterministic for
identical persisted inputs + configuration (no wall-clock reads other than
the caller-supplied `now`, no randomness).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from ..storage import execution_repositories as exec_repo
from ..universe.tickers import TickerUniverse, UnknownSymbolError
from .config import ExecutionConfig
from .models import PaperExecutionEligibility

INELIGIBLE_SIDES = ("watch", "no_action", "analysis_incomplete", "screened_out")

PortfolioGuardrailCheck = Callable[[dict], "list[str]"]


def _null_portfolio_guardrail(_recommendation: dict) -> list[str]:
    """Default guardrail: always passes.

    A full re-check against *live* portfolio state (current sector
    exposure, daily-loss/drawdown circuit breakers, live buying power) would
    require re-fetching Robinhood account state at execution time, which is
    out of scope for this milestone (no live data anywhere in this slice —
    see docs/milestone3-lumibot-paper-integration.md "Known limitations").
    The injection point exists so a future milestone can wire a real check
    in without touching this policy's call sites.
    """
    return []


@dataclass(frozen=True)
class PaperExecutionEligibilityPolicy:
    universe: TickerUniverse
    config: ExecutionConfig
    portfolio_guardrail: PortfolioGuardrailCheck = _null_portfolio_guardrail

    def evaluate(self, recommendation: dict | None, *, conn, now: datetime) -> PaperExecutionEligibility:
        reasons: list[str] = []
        rec_id = (recommendation or {}).get("rec_id", "unknown")

        if recommendation is None:
            reasons.append("recommendation not found or has no persisted payload")
            return self._result(rec_id, reasons, now)

        if self.config.kill_switch_enabled:
            reasons.append("global paper-execution kill switch is enabled")

        if recommendation.get("frozen") is not True:
            reasons.append("recommendation is not frozen")

        status = recommendation.get("status")
        if status == "expired":
            reasons.append("recommendation status is expired")
        elif status != "active":
            reasons.append(f"recommendation status {status!r} is not active")

        side = recommendation.get("side")
        if side in INELIGIBLE_SIDES:
            reasons.append(f"recommendation side {side!r} is not eligible for paper execution")
        elif side != "buy_candidate":
            reasons.append(f"recommendation side {side!r} is not a recognized executable side")

        risk_plan = recommendation.get("risk_plan")
        if not risk_plan:
            reasons.append("recommendation has no risk_plan")
        else:
            shares = risk_plan.get("shares")
            if not isinstance(shares, int) or shares <= 0:
                reasons.append(f"risk_plan.shares is not a positive quantity: {shares!r}")
            entry_price = risk_plan.get("entry_price")
            if not entry_price or entry_price <= 0:
                reasons.append("risk_plan.entry_price is missing or non-positive")

        if recommendation.get("price_at_rec") is None:
            reasons.append("recommendation has no price_at_rec (missing entry/reference price)")

        ts_raw = recommendation.get("ts")
        if ts_raw is None:
            reasons.append("recommendation has no ts — cannot evaluate expiration")
        else:
            frozen_at = _parse_ts(ts_raw)
            ttl = timedelta(minutes=self.config.recommendation_ttl_minutes)
            if now - frozen_at >= ttl:
                reasons.append(
                    f"recommendation expired: frozen at {frozen_at.isoformat()}, "
                    f"ttl {self.config.recommendation_ttl_minutes}m"
                )

        market_ts_raw = (recommendation.get("data_timestamps") or {}).get("market")
        if market_ts_raw is None:
            reasons.append("recommendation has no data_timestamps.market — cannot verify price freshness")
        else:
            market_ts = _parse_ts(market_ts_raw)
            age = (now - market_ts).total_seconds()
            if age > self.config.max_price_staleness_seconds:
                reasons.append(
                    f"market price data is stale: {age:.0f}s old exceeds "
                    f"{self.config.max_price_staleness_seconds:.0f}s limit"
                )

        symbol = recommendation.get("symbol")
        if symbol:
            try:
                self.universe.require(symbol)
            except UnknownSymbolError as exc:
                reasons.append(f"symbol rejected by TickerUniverse: {exc}")
        else:
            reasons.append("recommendation has no symbol")

        config_hash = recommendation.get("config_hash")
        git_sha = recommendation.get("git_sha")
        if not config_hash or not git_sha:
            reasons.append("recommendation provenance (config_hash/git_sha) is incomplete")

        if rec_id and rec_id != "unknown":
            existing = exec_repo.get_intent_by_recommendation(conn, rec_id, self.config.execution_version)
            if existing is not None:
                reasons.append(
                    f"recommendation already has a paper execution intent for "
                    f"execution_version={self.config.execution_version!r} (duplicate execution rejected)"
                )

        reasons.extend(self.portfolio_guardrail(recommendation))

        return self._result(rec_id, reasons, now)

    def _result(self, rec_id: str, reasons: list[str], now: datetime) -> PaperExecutionEligibility:
        return PaperExecutionEligibility(
            recommendation_id=rec_id,
            eligible=not reasons,
            reasons=tuple(reasons),
            evaluated_at=now,
            policy_version=self.config.policy_version,
        )


def _parse_ts(value: str) -> datetime:
    from datetime import timezone

    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
