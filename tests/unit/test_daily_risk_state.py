from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from trading_research.paper_books.daily_risk import (
    DailyRiskState, DailyRiskStateError, calculate_daily_risk_values,
)
from trading_research.paper_books.models import VALUATION_COMPLETE


NOW = datetime(2026, 7, 18, 20, tzinfo=timezone.utc)


def _state(**overrides):
    values = dict(
        risk_state_id="r", book_id="BASELINE", market_date=date(2026, 7, 18), as_of=NOW,
        start_of_day_equity=Decimal("100000"), current_equity=Decimal("97000"),
        realized_pnl_today=Decimal("-1000"), unrealized_pnl_today=Decimal("-2000"),
        total_pnl_today=Decimal("-3000"), net_external_cash_flow=Decimal("0"),
        daily_loss_fraction=Decimal("-0.03"), historical_peak_equity=Decimal("110000"),
        current_drawdown_fraction=Decimal("-13000") / Decimal("110000"),
        valuation_status=VALUATION_COMPLETE, source_snapshot_ids=("s0", "s1"),
        reconciliation_status="MATCHED", calculation_policy_version="v", config_hash="cfg", created_at=NOW,
    )
    values.update(overrides)
    return DailyRiskState(**values)


def test_daily_loss_and_drawdown_formulas_exact_decimal():
    result = calculate_daily_risk_values(
        start_of_day_equity=Decimal("100"), current_equity=Decimal("91"),
        realized_pnl_today=Decimal("-4"), unrealized_pnl_today=Decimal("-5"),
        net_external_cash_flow=Decimal("0"), historical_peak_equity=Decimal("130"),
    )
    assert result["daily_loss_fraction"] == Decimal("-0.09")
    assert result["current_drawdown_fraction"] == Decimal("-39") / Decimal("130")


def test_external_cash_flow_is_not_trading_profit():
    result = calculate_daily_risk_values(
        start_of_day_equity=Decimal("100"), current_equity=Decimal("120"),
        realized_pnl_today=Decimal("0"), unrealized_pnl_today=Decimal("0"),
        net_external_cash_flow=Decimal("20"), historical_peak_equity=Decimal("120"),
    )
    assert result["total_pnl_today"] == 0
    assert result["daily_loss_fraction"] == 0


def test_missing_baseline_and_inconsistent_components_fail_closed():
    with pytest.raises(DailyRiskStateError):
        calculate_daily_risk_values(
            start_of_day_equity=Decimal("0"), current_equity=Decimal("1"),
            realized_pnl_today=Decimal("0"), unrealized_pnl_today=Decimal("1"),
            net_external_cash_flow=Decimal("0"), historical_peak_equity=Decimal("1"),
        )
    with pytest.raises(DailyRiskStateError):
        _state(unrealized_pnl_today=Decimal("0"))
