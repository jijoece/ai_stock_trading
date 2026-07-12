"""Frozen recommendation builder (architecture, story 1C.4).

Combines a screening result, a composite score, and a risk plan into a
single record that validates against schemas/recommendation.schema.json —
never emitted otherwise. The builder writes a recommendation exactly once:
there is no update method, and re-invoking it with the same
`idempotency_key` reproduces the same `rec_id` byte-for-byte (see
`derive_rec_id`), so persistence (trading_repositories.py) can no-op a
retried creation instead of conflicting.

Status/side mapping used by this slice (schema allows other combinations;
this is the policy this builder implements):

| Situation                                   | side               | status               |
|----------------------------------------------|--------------------|-----------------------|
| symbol invalid / data missing / score or risk incomplete | analysis_incomplete | analysis_incomplete |
| screener rejected the candidate               | screened_out       | active                |
| risk engine returned shares == 0 (NO_ACTION)  | no_action          | active                |
| risk engine returned shares > 0               | buy_candidate      | active                |

`side="watch"` and `status="expired"` remain valid per the schema for other
callers (e.g. a future evaluator) but are not produced by this slice.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime

from jsonschema import Draft7Validator

from ..analysis.scorer import CompositeScore
from ..analysis.screener import ScreeningResult
from ..config import REPO_ROOT
from ..risk.position_sizing import PositionPlan

DISCLAIMER = "Research output only. Not financial advice. Not an instruction to trade."
REDDIT_WEIGHT_CAP = 0.10

STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_ANALYSIS_INCOMPLETE = "analysis_incomplete"

SIDE_BUY_CANDIDATE = "buy_candidate"
SIDE_WATCH = "watch"
SIDE_NO_ACTION = "no_action"
SIDE_ANALYSIS_INCOMPLETE = "analysis_incomplete"
SIDE_SCREENED_OUT = "screened_out"

_SCHEMA_PATH = REPO_ROOT / "schemas" / "recommendation.schema.json"


class RecommendationBuildError(RuntimeError):
    """A recommendation failed typed-domain or JSON-schema validation."""


def derive_rec_id(idempotency_key: str) -> str:
    """Deterministic rec_id from an idempotency key — same key, same id,
    every time, on every machine. Never random."""
    return "rec-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]


def git_sha() -> str:
    """Current commit for reproducibility; 'unknown' when git is unavailable."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
        )
        sha = proc.stdout.strip()
        if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{7,40}", sha):
            return sha
    except OSError:
        pass
    return "unknown"


def _load_validator() -> Draft7Validator:
    return Draft7Validator(json.loads(_SCHEMA_PATH.read_text()))


@dataclass(frozen=True)
class FrozenRecommendation:
    """Immutable wrapper over an already schema-validated recommendation
    payload. There is deliberately no setter/update method — freezing is a
    one-time event at construction (build_recommendation), not a lifecycle
    the object supports afterward.
    """

    payload: dict

    @property
    def rec_id(self) -> str:
        return self.payload["rec_id"]

    @property
    def side(self) -> str:
        return self.payload["side"]

    @property
    def status(self) -> str:
        return self.payload["status"]

    def to_dict(self) -> dict:
        return dict(self.payload)  # defensive copy of the frozen snapshot


def _score_to_factors(score: CompositeScore | None) -> list[dict]:
    if score is None:
        return []
    return [
        {
            "factor": f.factor,
            "raw_value": f.raw_value,
            "normalized": f.normalized_value,
            "weight": f.weight,
            "contribution": f.contribution,
        }
        for f in score.factors
    ]


def _risk_plan_dict(plan: PositionPlan | None) -> dict | None:
    if plan is None or not plan.actionable:
        return None
    return {
        "shares": plan.shares,
        "entry_price": plan.entry_price,
        "stop_price": plan.stop_price,
        "target_price": plan.target_price,
        "risk_per_share": plan.risk_per_share,
        "dollars_at_risk": plan.dollars_at_risk,
        "position_value": plan.position_value,
        "reward_risk": plan.reward_risk,
        "warnings": list(plan.warnings),
    }


def build_recommendation(
    *,
    symbol: str,
    idempotency_key: str,
    run_id: str | None,
    screening_result: ScreeningResult | None,
    composite_score: CompositeScore | None,
    position_plan: PositionPlan | None,
    price_at_rec: float | None,
    data_timestamps: dict[str, str],
    warnings: list[str],
    missing_data_reasons: list[str],
    reddit_component: dict | None,
    model_version: str,
    prompt_version: str,
    config_hash: str,
    now: datetime,
    rationale_text: str = "",
) -> FrozenRecommendation:
    """Build, typed-validate, and schema-validate one frozen recommendation.

    Raises RecommendationBuildError (never a bare ValueError) if the
    combination of inputs cannot produce a schema-valid record — this is a
    programming-error signal, distinct from ANALYSIS_INCOMPLETE, which is a
    normal, expected *output* of this function for genuinely incomplete
    market/fundamental data.
    """
    rec_id = derive_rec_id(idempotency_key)
    warnings = list(warnings)
    missing_data_reasons = list(missing_data_reasons)

    side: str
    status: str
    score_value: float | None = None
    confidence: str | None = None
    risk_plan: dict | None = None
    factors: list[dict] = []

    if missing_data_reasons:
        side = SIDE_ANALYSIS_INCOMPLETE
        status = STATUS_ANALYSIS_INCOMPLETE
    elif screening_result is not None and not screening_result.passed:
        side = SIDE_SCREENED_OUT
        status = STATUS_ACTIVE
        factors = _score_to_factors(composite_score)
        score_value = composite_score.total_score if composite_score is not None else None
    elif composite_score is None or position_plan is None:
        side = SIDE_ANALYSIS_INCOMPLETE
        status = STATUS_ANALYSIS_INCOMPLETE
        if composite_score is None:
            missing_data_reasons.append("composite score unavailable")
        if position_plan is None:
            missing_data_reasons.append("risk plan unavailable")
    else:
        factors = _score_to_factors(composite_score)
        score_value = composite_score.total_score
        confidence = "high" if score_value >= 70 else "medium" if score_value >= 50 else "low"
        if position_plan.actionable:
            side = SIDE_BUY_CANDIDATE
            status = STATUS_ACTIVE
            risk_plan = _risk_plan_dict(position_plan)
        else:
            side = SIDE_NO_ACTION
            status = STATUS_ACTIVE
        warnings.extend(position_plan.warnings)

    if reddit_component is not None and reddit_component.get("weight", 0.0) > REDDIT_WEIGHT_CAP + 1e-12:
        raise RecommendationBuildError(
            f"reddit_component weight {reddit_component['weight']} exceeds hard cap {REDDIT_WEIGHT_CAP}"
        )

    payload = {
        "rec_id": rec_id,
        "run_id": run_id,
        "symbol": symbol,
        "side": side,
        "ts": now.isoformat(),
        "price_at_rec": price_at_rec,
        "score": score_value,
        "confidence": confidence,
        "status": status,
        "acted": False,
        "rationale_text": rationale_text,
        "factors": factors,
        "risk_plan": risk_plan,
        "warnings": warnings,
        "missing_data_reasons": missing_data_reasons,
        "data_timestamps": data_timestamps,
        "reddit_component": reddit_component,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "config_hash": config_hash,
        "git_sha": git_sha(),
        "frozen": True,
        "disclaimer": DISCLAIMER,
    }

    _validate_domain(payload)
    validator = _load_validator()
    errors = sorted(validator.iter_errors(payload), key=lambda e: e.json_path)
    if errors:
        raise RecommendationBuildError(
            "recommendation failed schema validation: " + "; ".join(f"{e.json_path}: {e.message}" for e in errors)
        )

    return FrozenRecommendation(payload=payload)


def _validate_domain(payload: dict) -> None:
    """Typed-domain checks performed before (and in addition to) JSON-schema
    validation — catches structural mistakes with a specific message rather
    than relying solely on generic schema error text."""
    if payload["status"] == STATUS_ANALYSIS_INCOMPLETE and not payload["missing_data_reasons"]:
        raise RecommendationBuildError("analysis_incomplete status requires at least one missing_data_reason")
    if payload["side"] in (SIDE_NO_ACTION, SIDE_ANALYSIS_INCOMPLETE, SIDE_SCREENED_OUT) and payload["risk_plan"] is not None:
        raise RecommendationBuildError(f"side={payload['side']!r} must not carry a risk_plan")
    if payload["status"] == STATUS_ANALYSIS_INCOMPLETE and payload["risk_plan"] is not None:
        raise RecommendationBuildError("analysis_incomplete status must not carry a risk_plan")
    if "account_id" in payload or any("account" in str(k).lower() and "id" in str(k).lower() for k in payload):
        raise RecommendationBuildError("recommendation payload must never carry a broker account identifier")
    if not re.fullmatch(r"[0-9a-f]{64}", payload["config_hash"]):
        raise RecommendationBuildError("config_hash must be a 64-hex-char sha256 digest")
