import pytest

from trading_research.risk.position_sizing import (
    IncompleteStateError,
    RiskInputs,
    compute_position_plan,
)

NOW = 1_800_000_000.0


def base_inputs(**overrides) -> RiskInputs:
    defaults = dict(
        account_equity=100_000.0,
        settled_cash=100_000.0,
        entry_price=20.0,
        stop_price=18.0,
        price_as_of_epoch=NOW - 10,
        now_epoch=NOW,
        earnings_date_known=True,
        days_to_earnings=30.0,
    )
    defaults.update(overrides)
    return RiskInputs(**defaults)


def test_basic_sizing():
    plan = compute_position_plan(base_inputs())
    # risk budget 1% of 100k = $1000; risk/share $2 → 500 shares,
    # but max position 5% of 100k = $5000 → capped at 250 shares.
    assert plan.shares == 250
    assert plan.dollars_at_risk == 500.0
    assert plan.target_price == 24.0  # 2R
    assert plan.reward_risk == 2.0
    assert "size capped by max position fraction" in plan.warnings


def test_property_risk_never_exceeds_budget():
    for equity in (5_000, 50_000, 500_000):
        for entry, stop in ((10.0, 9.5), (25.0, 20.0), (3.5, 3.4)):
            plan = compute_position_plan(
                base_inputs(account_equity=float(equity), settled_cash=float(equity),
                            entry_price=entry, stop_price=stop)
            )
            assert plan.shares * (entry - stop) <= equity * 0.01 + 1e-9


def test_missing_equity_fails_closed():
    with pytest.raises(IncompleteStateError):
        compute_position_plan(base_inputs(account_equity=None))


def test_missing_settled_cash_fails_closed():
    with pytest.raises(IncompleteStateError):
        compute_position_plan(base_inputs(settled_cash=None))


def test_stale_price_fails_closed():
    with pytest.raises(IncompleteStateError, match="stale"):
        compute_position_plan(base_inputs(price_as_of_epoch=NOW - 3600))


def test_unknown_earnings_date_fails_closed():
    with pytest.raises(IncompleteStateError, match="earnings"):
        compute_position_plan(base_inputs(earnings_date_known=False))


def test_imminent_earnings_is_no_action_not_error():
    plan = compute_position_plan(base_inputs(days_to_earnings=1.0))
    assert plan.shares == 0 and not plan.actionable
    assert any("earnings" in w for w in plan.warnings)


def test_stop_above_entry_is_no_action():
    plan = compute_position_plan(base_inputs(stop_price=21.0))
    assert plan.shares == 0


def test_settled_cash_caps_size():
    plan = compute_position_plan(base_inputs(settled_cash=1_000.0))
    assert plan.shares == 50  # 1000 / 20
    assert "size capped by settled cash" in plan.warnings


def test_liquidity_cap():
    plan = compute_position_plan(
        base_inputs(avg_daily_dollar_volume=100_000.0)  # 1% → $1000 → 50 shares
    )
    assert plan.shares == 50
    assert any("liquidity" in w for w in plan.warnings)


def test_sector_concentration_blocks_entry():
    plan = compute_position_plan(base_inputs(sector_exposure_fraction=0.30))
    assert plan.shares == 0


def test_zero_settled_cash_no_shares():
    plan = compute_position_plan(base_inputs(settled_cash=0.0))
    assert plan.shares == 0 and not plan.actionable
