from datetime import datetime, timezone

import pytest
import yaml

from trading_research.analysis.scorer import (
    ScoringConfigError,
    ScoringIncompleteError,
    compute_composite_score,
    load_scoring_config,
    reconstruct_total_score,
    reconstruct_total_score_from_factors,
)
from trading_research.models.trading_models import CatalystRiskFlags, FundamentalSnapshot, TechnicalFactorInput

NOW = datetime(2026, 7, 11, 14, 0, 0, tzinfo=timezone.utc)
CONFIG = load_scoring_config()

GOOD_FUNDAMENTALS = FundamentalSnapshot(
    symbol="SOFI", revenue_growth_yoy=0.25, earnings_trend=0.4, gross_margin=0.4, operating_margin=0.15,
    free_cash_flow=100.0, shares_outstanding=1000.0, shares_outstanding_prior_year=1000.0,
)
GOOD_TECHNICAL = TechnicalFactorInput(symbol="SOFI", relative_strength=0.3, momentum_score=1.0,
                                       trend_score=1.0, price_volume_trend=0.2)
GOOD_CATALYST = CatalystRiskFlags(symbol="SOFI", macro_score=1.0, analyst_estimate_change=0.05)


def score_good(reddit=0.2):
    return compute_composite_score(
        "SOFI", CONFIG, GOOD_FUNDAMENTALS, GOOD_TECHNICAL, GOOD_CATALYST, reddit, NOW,
        data_timestamp=NOW.isoformat(),
    )


def test_weight_total_validation(tmp_path):
    raw = dict(CONFIG.raw)
    raw["weights"] = {"fundamentals": 0.40, "technicals": 0.30, "catalysts": 0.25, "reddit": 0.10}
    bad = tmp_path / "bad_scoring.yaml"
    bad.write_text(yaml.safe_dump(raw))
    with pytest.raises(ScoringConfigError, match="sum to 1.0"):
        load_scoring_config(bad)


def test_reddit_cap_enforcement_config_rejected(tmp_path):
    raw = dict(CONFIG.raw)
    raw["weights"] = {"fundamentals": 0.30, "technicals": 0.30, "catalysts": 0.20, "reddit": 0.20}
    bad = tmp_path / "bad_scoring2.yaml"
    bad.write_text(yaml.safe_dump(raw))
    with pytest.raises(ScoringConfigError, match="reddit_weight_cap"):
        load_scoring_config(bad)


def test_reddit_cap_never_exceeds_architectural_hard_limit(tmp_path):
    raw = dict(CONFIG.raw)
    raw["weights"] = {"fundamentals": 0.25, "technicals": 0.25, "catalysts": 0.30, "reddit": 0.20}
    raw["reddit_weight_cap"] = 0.20  # even if the config's OWN cap is raised, 0.10 is architectural
    bad = tmp_path / "bad_scoring3.yaml"
    bad.write_text(yaml.safe_dump(raw))
    with pytest.raises(ScoringConfigError, match="architectural hard cap"):
        load_scoring_config(bad)


def test_exact_factor_contribution_reconstruction():
    result = score_good()
    reconstructed = reconstruct_total_score(result.pillars)
    assert reconstructed == result.total_score
    # Also reconstructible from the flat factor list alone (no pillar
    # grouping) — this is the path the persisted recommendation_factors
    # table exercises in the offline integration test.
    assert reconstruct_total_score_from_factors(result.factors) == result.total_score
    # And from plain dicts, matching what a DB read-back would look like.
    as_dicts = [{"contribution": f.contribution} for f in result.factors]
    assert reconstruct_total_score_from_factors(as_dicts) == result.total_score


def test_missing_critical_pillar_raises_incomplete():
    with pytest.raises(ScoringIncompleteError):
        compute_composite_score(
            "SOFI", CONFIG, FundamentalSnapshot(symbol="SOFI"), GOOD_TECHNICAL, GOOD_CATALYST, 0.2, NOW,
        )


def test_missing_optional_factor_excluded_not_zeroed():
    partial = FundamentalSnapshot(symbol="SOFI", revenue_growth_yoy=0.5)  # only one factor known
    result = compute_composite_score("SOFI", CONFIG, partial, GOOD_TECHNICAL, GOOD_CATALYST, 0.2, NOW)
    fnd_pillar = next(p for p in result.pillars if p.pillar == "fundamentals")
    assert fnd_pillar.available_factor_count == 1
    assert fnd_pillar.missing_factor_count == 5
    # revenue_growth_yoy=0.5 -> normalized clamp(0.5/0.5)=1.0 -> pillar_score = 50+50*1.0 = 100
    assert fnd_pillar.pillar_score == 100.0
    assert reconstruct_total_score_from_factors(result.factors) == result.total_score


def test_score_boundaries_clamped_0_100():
    extreme_fundamentals = FundamentalSnapshot(
        symbol="X", revenue_growth_yoy=10.0, earnings_trend=10.0, gross_margin=1.0, operating_margin=1.0,
        free_cash_flow=1.0, shares_outstanding=1.0, shares_outstanding_prior_year=100.0,
    )
    extreme_technical = TechnicalFactorInput(symbol="X", relative_strength=10.0, momentum_score=10.0,
                                              trend_score=10.0, price_volume_trend=10.0)
    extreme_catalyst = CatalystRiskFlags(symbol="X", macro_score=10.0, analyst_estimate_change=10.0)
    result = compute_composite_score("X", CONFIG, extreme_fundamentals, extreme_technical, extreme_catalyst, 10.0, NOW)
    assert 0.0 <= result.total_score <= 100.0


def test_negative_factors_reduce_score_below_neutral():
    bad_fundamentals = FundamentalSnapshot(
        symbol="X", revenue_growth_yoy=-0.5, earnings_trend=-1.0, gross_margin=0.0, operating_margin=-0.2,
        free_cash_flow=-1.0, shares_outstanding=1200.0, shares_outstanding_prior_year=1000.0,
    )
    bad_technical = TechnicalFactorInput(symbol="X", relative_strength=-1.0, momentum_score=-2.0,
                                          trend_score=-2.0, price_volume_trend=-1.0)
    bad_catalyst = CatalystRiskFlags(symbol="X", macro_score=-2.0, analyst_estimate_change=-0.2)
    result = compute_composite_score("X", CONFIG, bad_fundamentals, bad_technical, bad_catalyst, -1.0, NOW)
    assert result.total_score < 50.0


def test_configuration_hashing_reproducible():
    c1 = load_scoring_config()
    c2 = load_scoring_config()
    assert c1.config_hash == c2.config_hash


def test_reddit_component_disabled():
    result = compute_composite_score("SOFI", CONFIG, GOOD_FUNDAMENTALS, GOOD_TECHNICAL, GOOD_CATALYST, None, NOW)
    reddit_pillar = next(p for p in result.pillars if p.pillar == "reddit")
    assert reddit_pillar.pillar_score == 50.0
    assert reddit_pillar.available_factor_count == 0
    assert result.reddit_materially_changed_score is False


def test_reddit_component_at_maximum_cap():
    result = score_good(reddit=1.0)  # max bullish -> full +10pt swing at the 10% cap
    reddit_pillar = next(p for p in result.pillars if p.pillar == "reddit")
    assert reddit_pillar.pillar_weight == pytest.approx(0.10)
    assert reddit_pillar.weighted_contribution == pytest.approx(10.0)


def test_same_inputs_produce_identical_output():
    r1 = score_good()
    r2 = score_good()
    assert r1.total_score == r2.total_score
    assert r1.factors == r2.factors
    assert r1.pillars == r2.pillars
