"""Deterministic construction of a `PaperOrderIntent` from a frozen
recommendation (Milestone 3, Step 3/Step 4).

Never called directly by an adapter or the orchestration service until
`eligibility.evaluate_eligibility` has already returned `eligible=True` —
this module trusts its caller for that gate and only re-validates the
narrower, purely-structural facts (positive price, positive shares) that
`PaperOrderIntent.__post_init__` enforces anyway.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from .config import ExecutionConfig
from .models import ORDER_SIDE_BUY, ORDER_TYPE_MARKET, PaperOrderIntent, derive_intent_id


class IntentBuildError(ValueError):
    """The recommendation cannot produce a deterministic PaperOrderIntent."""


def _parse_ts(value: str, field_name: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise IntentBuildError(f"recommendation {field_name!r} is not a valid ISO timestamp: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def build_paper_order_intent(
    recommendation: dict,
    *,
    config: ExecutionConfig,
    git_sha: str,
) -> PaperOrderIntent:
    """Build a `PaperOrderIntent` from a frozen `buy_candidate` recommendation
    payload (as returned by `storage.trading_repositories.load_recommendation`).

    Deterministic and side-effect-free: identical inputs always produce a
    byte-identical intent (`intent_id` is derived, never random or
    wall-clock-based). Raises `IntentBuildError` — never a bare exception —
    on any structural problem, so callers can distinguish "not eligible"
    (handled earlier, in `eligibility.py`) from "eligible but malformed",
    which should never happen if eligibility ran first.
    """
    risk_plan = recommendation.get("risk_plan")
    if not risk_plan:
        raise IntentBuildError("recommendation has no risk_plan — cannot build an intent")

    shares = risk_plan.get("shares")
    if not isinstance(shares, int) or shares <= 0:
        raise IntentBuildError(f"risk_plan.shares must be a positive int — got {shares!r}")

    price_at_rec = recommendation.get("price_at_rec")
    if price_at_rec is None:
        raise IntentBuildError("recommendation.price_at_rec is missing — no default price is allowed")
    try:
        reference_price = Decimal(str(price_at_rec))
    except InvalidOperation as exc:
        raise IntentBuildError(f"recommendation.price_at_rec is not a valid number: {price_at_rec!r}") from exc
    if reference_price <= 0:
        raise IntentBuildError(f"recommendation.price_at_rec must be positive — got {reference_price}")

    rec_id = recommendation.get("rec_id")
    symbol = recommendation.get("symbol")
    if not rec_id or not symbol:
        raise IntentBuildError("recommendation is missing rec_id or symbol")

    frozen_at = _parse_ts(recommendation["ts"], "ts")
    # This system freezes a recommendation at construction time (see
    # recommendations/builder.py) — there is no separate "created, then
    # later frozen" instant, so both timestamps are the same value.
    created_at = frozen_at
    expires_at = frozen_at + timedelta(minutes=config.recommendation_ttl_minutes)

    rec_config_hash = recommendation.get("config_hash")
    if not rec_config_hash:
        raise IntentBuildError("recommendation.config_hash is missing")

    quantity = shares
    expected_notional = reference_price * quantity

    return PaperOrderIntent(
        intent_id=derive_intent_id(rec_id, config.execution_version),
        recommendation_id=rec_id,
        symbol=symbol,
        side=ORDER_SIDE_BUY,
        quantity=quantity,
        order_type=ORDER_TYPE_MARKET,
        limit_price=None,
        reference_price=reference_price,
        expected_notional=expected_notional,
        recommendation_created_at=created_at,
        recommendation_frozen_at=frozen_at,
        expires_at=expires_at,
        config_hash=rec_config_hash,
        git_sha=git_sha,
        policy_version=config.policy_version,
        execution_version=config.execution_version,
    )
