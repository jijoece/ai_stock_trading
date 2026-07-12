"""Deterministic composite scorer (architecture §14, story 1C.2).

Every factor's raw value, normalized value, weight, and contribution is
computed by Python and stored — an LLM may generate the deterministic-
template explanation text but can never influence the number itself.

Pillar policy for optional-factor absence (documented, not implicit): a
pillar's score is `50 + 50 * mean(normalized values of its AVAILABLE
factors)` — missing factors are excluded from that mean (never treated as
neutral-favorable 0, never fabricated), and every configured factor still
appears in the output with `data_quality_status="missing"` and
`contribution=0.0` for full auditability. If a *critical* pillar
(fundamentals, technicals, catalysts) has zero available factors,
`compute_composite_score` raises `ScoringIncompleteError` — the caller must
turn that into ANALYSIS_INCOMPLETE, never a partial score. Reddit is the one
optional pillar: zero Reddit factors yields a neutral pillar_score of 50
(documented baseline), not incompleteness, since Reddit is supplementary by
design and must never gate the analysis.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import yaml

from ..hashing import hash_config
from ..models.trading_models import CatalystRiskFlags, FundamentalSnapshot, TechnicalFactorInput

DEFAULT_SCORING_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "scoring.yaml"

CRITICAL_PILLARS = ("fundamentals", "technicals", "catalysts")
OPTIONAL_PILLARS = ("reddit",)


class ScoringConfigError(RuntimeError):
    """The scoring configuration is missing, malformed, or out of range."""


class ScoringIncompleteError(RuntimeError):
    """A critical pillar has zero available factors — score cannot be produced."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


@dataclass(frozen=True)
class ScoringConfig:
    version: int
    weights: dict[str, float]
    reddit_weight_cap: float
    reddit_materiality_threshold_points: float
    config_hash: str
    raw: dict


def load_scoring_config(path: str | Path | None = None) -> ScoringConfig:
    """Load and validate config/scoring.yaml. Fails early; never renormalizes."""
    config_path = Path(path) if path else DEFAULT_SCORING_CONFIG_PATH
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except OSError as exc:
        raise ScoringConfigError(f"cannot read scoring config at {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ScoringConfigError(f"invalid YAML in scoring config at {config_path}: {exc}") from exc

    required = {"version", "weights", "reddit_weight_cap", "reddit_materiality_threshold_points"}
    missing = required - raw.keys()
    if missing:
        raise ScoringConfigError(f"scoring config missing keys: {sorted(missing)}")

    weights = {k: float(v) for k, v in raw["weights"].items()}
    expected_pillars = set(CRITICAL_PILLARS) | set(OPTIONAL_PILLARS)
    if set(weights) != expected_pillars:
        raise ScoringConfigError(f"scoring config weights must cover exactly {sorted(expected_pillars)}, got {sorted(weights)}")
    if any(w < 0 for w in weights.values()):
        raise ScoringConfigError("scoring config weights must be >= 0")

    total = sum(weights.values())
    if abs(total - 1.0) > 1e-9:
        raise ScoringConfigError(f"scoring config weights must sum to 1.0, got {total}")

    reddit_cap = float(raw["reddit_weight_cap"])
    if weights["reddit"] > reddit_cap + 1e-12:
        raise ScoringConfigError(
            f"weights.reddit ({weights['reddit']}) exceeds reddit_weight_cap ({reddit_cap}) — "
            "fix the config; this is never silently renormalized"
        )
    if reddit_cap > 0.10 + 1e-12:
        raise ScoringConfigError(f"reddit_weight_cap ({reddit_cap}) exceeds the architectural hard cap of 0.10")

    return ScoringConfig(
        version=raw["version"],
        weights=weights,
        reddit_weight_cap=reddit_cap,
        reddit_materiality_threshold_points=float(raw["reddit_materiality_threshold_points"]),
        config_hash=hash_config(raw),
        raw=raw,
    )


@dataclass(frozen=True)
class FactorScore:
    factor: str
    pillar: str
    raw_value: float | None
    normalized_value: float | None
    weight: float
    contribution: float
    source_timestamp: str | None
    data_quality_status: str  # "ok" | "missing"
    explanation: str


@dataclass(frozen=True)
class PillarScore:
    pillar: str
    pillar_score: float
    pillar_weight: float
    weighted_contribution: float
    available_factor_count: int
    missing_factor_count: int


@dataclass(frozen=True)
class CompositeScore:
    symbol: str
    total_score: float
    pillars: tuple[PillarScore, ...]
    factors: tuple[FactorScore, ...]
    config_hash: str
    config_version: int
    scored_at: str
    warnings: tuple[str, ...]
    reddit_materially_changed_score: bool


def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


# (factor_name, extractor(snapshot) -> raw|None, normalizer(raw) -> [-1,1])
_FactorSpec = tuple[str, Callable[[object], float | None], Callable[[float], float]]

_FUNDAMENTALS_SPECS: tuple[_FactorSpec, ...] = (
    ("revenue_growth_yoy", lambda f: f.revenue_growth_yoy, lambda v: _clamp(v / 0.5)),
    ("earnings_trend", lambda f: f.earnings_trend, lambda v: _clamp(v)),
    ("gross_margin", lambda f: f.gross_margin, lambda v: _clamp((v - 0.30) / 0.30)),
    ("operating_margin", lambda f: f.operating_margin, lambda v: _clamp((v - 0.10) / 0.20)),
    ("free_cash_flow", lambda f: (float(f.free_cash_flow) if f.free_cash_flow is not None else None),
     lambda v: 1.0 if v > 0 else (-1.0 if v < 0 else 0.0)),
    ("dilution", lambda f: f.share_growth_yoy, lambda v: _clamp(-v / 0.20)),
)

_TECHNICALS_SPECS: tuple[_FactorSpec, ...] = (
    ("relative_strength", lambda t: t.relative_strength, lambda v: _clamp(v)),
    ("momentum_score", lambda t: t.momentum_score, lambda v: _clamp(v / 2.0)),
    ("trend_score", lambda t: t.trend_score, lambda v: _clamp(v / 2.0)),
    ("price_volume_trend", lambda t: t.price_volume_trend, lambda v: _clamp(v)),
)

_CATALYSTS_SPECS: tuple[_FactorSpec, ...] = (
    ("macro_score", lambda c: c.macro_score, lambda v: _clamp(v / 2.0)),
    ("analyst_estimate_change", lambda c: c.analyst_estimate_change, lambda v: _clamp(v / 0.20)),
    ("sec_filing_risk", lambda c: float(len(c.sec_filing_risk_flags)) if c is not None else None,
     lambda v: -min(1.0, v * 0.34)),
)


def _score_pillar(
    pillar: str,
    weight: float,
    specs: tuple[_FactorSpec, ...],
    snapshot: object,
    timestamp: str | None,
) -> tuple[PillarScore, list[FactorScore]]:
    # First pass: find which configured factors are actually available. The
    # per-factor slot weight is split across AVAILABLE factors only (never
    # the full configured count) — this is what makes total_score exactly
    # reconstructible from the flat, persisted factor list alone (see
    # reconstruct_total_score_from_factors): missing factors get weight=0
    # and contribution=0, and 50 + 0.5*sum(all contributions) always equals
    # total_score, regardless of which optional factors were missing.
    resolved: dict[str, tuple[float, float]] = {}  # name -> (raw, normalized)
    for name, extractor, normalizer in specs:
        raw = extractor(snapshot) if snapshot is not None else None
        if raw is not None:
            resolved[name] = (raw, normalizer(raw))

    available_count = len(resolved)
    missing_count = len(specs) - available_count
    slot_weight = weight / available_count if available_count else 0.0

    factors: list[FactorScore] = []
    for name, extractor, normalizer in specs:
        if name not in resolved:
            factors.append(FactorScore(
                factor=name, pillar=pillar, raw_value=None, normalized_value=None,
                weight=0.0, contribution=0.0, source_timestamp=timestamp,
                data_quality_status="missing",
                explanation=f"{name}: missing — excluded from the {pillar} pillar average, contributes 0.0 pts",
            ))
            continue
        raw, normalized = resolved[name]
        contribution = round(100 * slot_weight * normalized, 4)
        factors.append(FactorScore(
            factor=name, pillar=pillar, raw_value=round(raw, 6), normalized_value=round(normalized, 6),
            weight=round(slot_weight, 6), contribution=contribution, source_timestamp=timestamp,
            data_quality_status="ok",
            explanation=(
                f"{name}={raw:.4f} normalized to {normalized:+.2f}, weight {slot_weight:.2%}, "
                f"contributes {contribution:+.2f} pts"
            ),
        ))

    # weighted_contribution is derived from the *rounded, stored* factor
    # contributions (not a separately-rounded recomputation from raw
    # normalized values) so that summing it across pillars is bit-for-bit
    # the same arithmetic reconstruct_total_score_from_factors performs from
    # the flat, persisted factor list — see that function's docstring.
    contributions_sum = sum(f.contribution for f in factors)
    weighted_contribution = round(weight * 50.0 + 0.5 * contributions_sum, 6)
    pillar_score = round(weighted_contribution / weight, 4) if weight > 0 else 50.0

    return (
        PillarScore(
            pillar=pillar, pillar_score=pillar_score, pillar_weight=weight,
            weighted_contribution=weighted_contribution,
            available_factor_count=available_count, missing_factor_count=missing_count,
        ),
        factors,
    )


def reconstruct_total_score(pillars: tuple[PillarScore, ...]) -> float:
    """Recompute total_score purely from stored PillarScore records.

    This is the independent-reconstruction path tests use to prove the
    composite score is not a hidden/opaque number: given only the persisted
    per-pillar weighted contributions, the total is just their sum, clamped
    to [0, 100] exactly as compute_composite_score does internally.
    """
    return round(max(0.0, min(100.0, sum(p.weighted_contribution for p in pillars))), 4)


def reconstruct_total_score_from_factors(factors) -> float:
    """Recompute total_score purely from the flat, persisted factor list —
    no pillar grouping, no original snapshots. `factors` may be `FactorScore`
    instances or plain dicts with a `contribution` key (e.g. rows read back
    from the `recommendation_factors` table).

    Holds exactly because every pillar's weighted_contribution equals
    `pillar_weight * 50 + 0.5 * sum(that pillar's factor contributions)`
    (see _score_pillar) and the four pillar weights sum to 1.0, so summing
    across all pillars telescopes to `50 + 0.5 * sum(all contributions)`.
    """
    total = sum((f["contribution"] if isinstance(f, dict) else f.contribution) for f in factors)
    return round(max(0.0, min(100.0, 50.0 + 0.5 * total)), 4)


def compute_composite_score(
    symbol: str,
    config: ScoringConfig,
    fundamentals: FundamentalSnapshot | None,
    technical: TechnicalFactorInput | None,
    catalyst: CatalystRiskFlags | None,
    reddit_net_sentiment: float | None,
    now: datetime,
    data_timestamp: str | None = None,
) -> CompositeScore:
    """Compute the composite score. Raises ScoringIncompleteError if any
    critical pillar (fundamentals/technicals/catalysts) has zero available
    factors — the caller must convert that into ANALYSIS_INCOMPLETE.
    """
    all_factors: list[FactorScore] = []
    pillars: list[PillarScore] = []
    incomplete_reasons: list[str] = []

    pillar_specs = {
        "fundamentals": (fundamentals, _FUNDAMENTALS_SPECS),
        "technicals": (technical, _TECHNICALS_SPECS),
        "catalysts": (catalyst, _CATALYSTS_SPECS),
    }
    for pillar_name, (snapshot, specs) in pillar_specs.items():
        pillar_score, factors = _score_pillar(pillar_name, config.weights[pillar_name], specs, snapshot, data_timestamp)
        pillars.append(pillar_score)
        all_factors.extend(factors)
        if pillar_score.available_factor_count == 0:
            incomplete_reasons.append(f"critical pillar '{pillar_name}' has no available factors")

    # Reddit: the one optional pillar. A single factor (net sentiment);
    # absence yields a neutral pillar_score of 50 (documented), never gating.
    reddit_weight = config.weights["reddit"]
    if reddit_net_sentiment is None:
        reddit_pillar = PillarScore(
            pillar="reddit", pillar_score=50.0, pillar_weight=reddit_weight,
            weighted_contribution=round(reddit_weight * 50.0, 4),
            available_factor_count=0, missing_factor_count=1,
        )
        all_factors.append(FactorScore(
            factor="reddit_net_sentiment", pillar="reddit", raw_value=None, normalized_value=None,
            weight=0.0, contribution=0.0, source_timestamp=data_timestamp,
            data_quality_status="missing",
            explanation="reddit_net_sentiment: missing/disabled — neutral 50 baseline used, not gating",
        ))
    else:
        normalized = _clamp(reddit_net_sentiment)
        contribution = round(100 * reddit_weight * normalized, 4)
        # Same derivation as _score_pillar: weighted_contribution from the
        # rounded, stored contribution — never a separately-rounded path.
        weighted_contribution = round(reddit_weight * 50.0 + 0.5 * contribution, 6)
        reddit_score = round(weighted_contribution / reddit_weight, 4) if reddit_weight > 0 else 50.0
        reddit_pillar = PillarScore(
            pillar="reddit", pillar_score=reddit_score, pillar_weight=reddit_weight,
            weighted_contribution=weighted_contribution,
            available_factor_count=1, missing_factor_count=0,
        )
        all_factors.append(FactorScore(
            factor="reddit_net_sentiment", pillar="reddit", raw_value=round(reddit_net_sentiment, 6),
            normalized_value=round(normalized, 6), weight=round(reddit_weight, 6), contribution=contribution,
            source_timestamp=data_timestamp, data_quality_status="ok",
            explanation=(
                f"reddit_net_sentiment={reddit_net_sentiment:+.2f}, weight {reddit_weight:.2%} "
                f"(capped at {config.reddit_weight_cap:.0%}), contributes {contribution:+.2f} pts"
            ),
        ))
    pillars.append(reddit_pillar)

    if incomplete_reasons:
        raise ScoringIncompleteError(incomplete_reasons)

    total_with_reddit = reconstruct_total_score(tuple(pillars))
    if reddit_net_sentiment is None:
        # No actual Reddit signal was supplied — the neutral 50 baseline
        # cannot have "materially changed" anything, by definition.
        materially_changed = False
    else:
        total_without_reddit = round(max(0.0, min(100.0, total_with_reddit - reddit_pillar.weighted_contribution)), 4)
        materially_changed = abs(total_with_reddit - total_without_reddit) >= config.reddit_materiality_threshold_points

    return CompositeScore(
        symbol=symbol,
        total_score=total_with_reddit,
        pillars=tuple(pillars),
        factors=tuple(all_factors),
        config_hash=config.config_hash,
        config_version=config.version,
        scored_at=now.isoformat(),
        warnings=(),
        reddit_materially_changed_score=materially_changed,
    )
