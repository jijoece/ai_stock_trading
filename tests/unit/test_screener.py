from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_research.analysis.screener import (
    ScreeningCandidate,
    ScreeningConfigError,
    load_screening_config,
    screen_candidate,
)
from trading_research.models.trading_models import (
    CatalystRiskFlags,
    DataFreshness,
    FundamentalSnapshot,
    MarketDataSnapshot,
    SecuritySnapshot,
    TechnicalFactorInput,
)

NOW = datetime(2026, 7, 11, 14, 0, 0, tzinfo=timezone.utc)
FRESH = DataFreshness(source="fixture", as_of=NOW - timedelta(minutes=1))
CONFIG = load_screening_config()


def good_candidate() -> ScreeningCandidate:
    security = SecuritySnapshot(
        symbol="SOFI", name="SoFi Technologies Inc", exchange="NASDAQ",
        is_otc=False, is_active=True, freshness=FRESH,
    )
    market = MarketDataSnapshot(
        symbol="SOFI", price=Decimal("14.92"), bid=Decimal("14.90"), ask=Decimal("14.94"),
        avg_daily_dollar_volume=Decimal("350000000"), market_cap=Decimal("15800000000"),
        recent_halt=False, recent_reverse_split=False, realized_volatility=0.03, freshness=FRESH,
    )
    fundamentals = FundamentalSnapshot(
        symbol="SOFI", operating_history_years=5.0,
        going_concern_warning=False, bankruptcy_or_distress=False, shell_company_flag=False,
        recent_reverse_split=False,
        shares_outstanding=Decimal("1050000000"), shares_outstanding_prior_year=Decimal("1000000000"),
        cash=Decimal("2900000000"), quarterly_cash_burn=Decimal("100000000"),
        freshness=FRESH,
    )
    catalyst = CatalystRiskFlags(symbol="SOFI", earnings_date_known=True, days_to_earnings=30.0, freshness=FRESH)
    technical = TechnicalFactorInput(symbol="SOFI", relative_strength=0.5, momentum_score=1.0)
    return ScreeningCandidate(security=security, market=market, fundamentals=fundamentals,
                               technical=technical, catalyst=catalyst)


def test_fully_passing_candidate():
    result = screen_candidate(good_candidate(), CONFIG, NOW)
    assert result.passed is True
    assert all(g.passed for g in result.gate_results)
    assert result.config_hash == CONFIG.config_hash


def test_all_gate_outcomes_preserved_not_just_first_failure():
    candidate = good_candidate()
    candidate = replace(
        candidate,
        market=replace(candidate.market, price=Decimal("30.00"), recent_halt=True),
    )
    result = screen_candidate(candidate, CONFIG, NOW)
    assert result.passed is False
    failed_gates = {g.gate for g in result.gate_results if not g.passed}
    assert "max_share_price" in failed_gates
    assert "exclude_recent_halt" in failed_gates
    # every configured gate is still present in the result, not just the failures
    assert len(result.gate_results) == len(good_candidate_gate_names())


def good_candidate_gate_names() -> set[str]:
    return {g.gate for g in screen_candidate(good_candidate(), CONFIG, NOW).gate_results}


def test_price_above_limit_fails():
    c = good_candidate()
    c = replace(c, market=replace(c.market, price=Decimal("30.00")))
    result = screen_candidate(c, CONFIG, NOW)
    assert not result.passed
    gate = next(g for g in result.gate_results if g.gate == "max_share_price")
    assert not gate.passed and gate.hard_failure


def test_insufficient_market_cap_fails():
    c = good_candidate()
    c = replace(c, market=replace(c.market, market_cap=Decimal("50000000")))
    result = screen_candidate(c, CONFIG, NOW)
    assert not result.passed
    assert not next(g for g in result.gate_results if g.gate == "min_market_cap").passed


def test_insufficient_dollar_volume_fails():
    c = good_candidate()
    c = replace(c, market=replace(c.market, avg_daily_dollar_volume=Decimal("100000")))
    result = screen_candidate(c, CONFIG, NOW)
    assert not next(g for g in result.gate_results if g.gate == "min_avg_daily_dollar_volume").passed


def test_otc_stock_fails():
    c = good_candidate()
    c = replace(c, security=replace(c.security, is_otc=True))
    result = screen_candidate(c, CONFIG, NOW)
    assert not next(g for g in result.gate_results if g.gate == "exclude_otc").passed


def test_going_concern_warning_fails():
    c = good_candidate()
    c = replace(c, fundamentals=replace(c.fundamentals, going_concern_warning=True))
    result = screen_candidate(c, CONFIG, NOW)
    assert not next(g for g in result.gate_results if g.gate == "exclude_going_concern_warning").passed


def test_recent_reverse_split_fails():
    c = good_candidate()
    c = replace(c, fundamentals=replace(c.fundamentals, recent_reverse_split=True))
    result = screen_candidate(c, CONFIG, NOW)
    assert not next(g for g in result.gate_results if g.gate == "exclude_recent_reverse_split").passed


def test_high_dilution_fails():
    c = good_candidate()
    c = replace(
        c,
        fundamentals=replace(
            c.fundamentals,
            shares_outstanding=Decimal("1500000000"),
            shares_outstanding_prior_year=Decimal("1000000000"),
        ),
    )
    result = screen_candidate(c, CONFIG, NOW)
    assert not next(g for g in result.gate_results if g.gate == "max_dilution_share_growth_yoy").passed


def test_insufficient_cash_runway_fails():
    c = good_candidate()
    c = replace(
        c,
        fundamentals=replace(c.fundamentals, cash=Decimal("100000000"), quarterly_cash_burn=Decimal("100000000")),
    )
    result = screen_candidate(c, CONFIG, NOW)
    assert not next(g for g in result.gate_results if g.gate == "min_cash_runway_quarters").passed


def test_earnings_inside_restricted_window_fails():
    c = good_candidate()
    c = replace(c, catalyst=replace(c.catalyst, days_to_earnings=1.0))
    result = screen_candidate(c, CONFIG, NOW)
    assert not next(g for g in result.gate_results if g.gate == "earnings_blackout_days").passed


def test_wide_spread_fails():
    c = good_candidate()
    c = replace(c, market=replace(c.market, bid=Decimal("10.00"), ask=Decimal("11.50")))
    result = screen_candidate(c, CONFIG, NOW)
    assert not next(g for g in result.gate_results if g.gate == "max_bid_ask_spread_bps").passed


def test_abnormal_volatility_fails():
    c = good_candidate()
    c = replace(c, market=replace(c.market, realized_volatility=0.5))
    result = screen_candidate(c, CONFIG, NOW)
    assert not next(g for g in result.gate_results if g.gate == "max_realized_volatility").passed


def test_recent_halt_fails():
    c = good_candidate()
    c = replace(c, market=replace(c.market, recent_halt=True))
    result = screen_candidate(c, CONFIG, NOW)
    assert not next(g for g in result.gate_results if g.gate == "exclude_recent_halt").passed


def test_missing_market_cap_fails_closed():
    c = good_candidate()
    c = replace(c, market=replace(c.market, market_cap=None))
    result = screen_candidate(c, CONFIG, NOW)
    gate = next(g for g in result.gate_results if g.gate == "min_market_cap")
    assert not gate.passed and gate.hard_failure and "unknown" in gate.reason


def test_stale_market_price_fails_closed():
    c = good_candidate()
    stale_freshness = DataFreshness(source="fixture", as_of=NOW - timedelta(hours=1))
    c = replace(c, market=replace(c.market, freshness=stale_freshness))
    result = screen_candidate(c, CONFIG, NOW)
    gate = next(g for g in result.gate_results if g.gate == "max_data_staleness_seconds")
    assert not gate.passed


def test_gate_order_independence():
    """Constructing the candidate with fields in a different assignment order
    (equivalently: shuffling which fields are set first) must not change the
    final result — gates are independent pure functions of the inputs."""
    c1 = good_candidate()
    c2 = ScreeningCandidate(
        catalyst=c1.catalyst, technical=c1.technical, fundamentals=c1.fundamentals,
        market=c1.market, security=c1.security,
    )
    r1 = screen_candidate(c1, CONFIG, NOW)
    r2 = screen_candidate(c2, CONFIG, NOW)
    assert r1.passed == r2.passed
    assert {g.gate: g.passed for g in r1.gate_results} == {g.gate: g.passed for g in r2.gate_results}


def test_screening_config_hash_reproducible():
    c1 = load_screening_config()
    c2 = load_screening_config()
    assert c1.config_hash == c2.config_hash


def test_screening_config_missing_key_fails_fast(tmp_path):
    bad = tmp_path / "bad_screening.yaml"
    bad.write_text("version: 1\nmax_share_price: 25.0\n")
    with pytest.raises(ScreeningConfigError):
        load_screening_config(bad)


def test_screening_config_negative_threshold_rejected(tmp_path):
    import yaml
    raw = dict(CONFIG.raw)
    raw["max_share_price"] = -5.0
    bad = tmp_path / "bad_screening2.yaml"
    bad.write_text(yaml.safe_dump(raw))
    with pytest.raises(ScreeningConfigError):
        load_screening_config(bad)
