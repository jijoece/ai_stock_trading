from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from trading_research.analysis.indicators import OHLCBar, average_true_range, true_range
from trading_research.paper_books.lifecycle_state import (
    EVALUATION_INCOMPLETE_ATR, STOP_REASON_BREAKEVEN,
    advance_lifecycle_state, calculate_partial_close_quantity,
    create_entry_lifecycle_state,
)


def test_true_range_and_wilder_atr_hand_fixture():
    assert true_range(high="12", low="9", previous_close="10") == Decimal("3")
    bars = (
        OHLCBar(date(2026, 1, 1), Decimal("10"), Decimal("8"), Decimal("9")),
        OHLCBar(date(2026, 1, 2), Decimal("12"), Decimal("9"), Decimal("11")),  # TR 3
        OHLCBar(date(2026, 1, 3), Decimal("13"), Decimal("10"), Decimal("12")), # TR 3
        OHLCBar(date(2026, 1, 4), Decimal("16"), Decimal("11"), Decimal("15")), # TR 5
        OHLCBar(date(2026, 1, 5), Decimal("17"), Decimal("14"), Decimal("16")), # TR 3
    )
    # initial ATR=(3+3)/2=3; then (3+5)/2=4; then (4+3)/2=3.5
    assert average_true_range(bars, period=2) == Decimal("3.5")
    assert average_true_range(bars[:2], period=2) is None


def test_lifecycle_stop_never_loosens_and_stale_atr_preserves_stop():
    opened = datetime(2026, 1, 1, 21, tzinfo=timezone.utc)
    state = create_entry_lifecycle_state(
        book_id="BASELINE", symbol="AAPL", originating_intent_id="i", entry_fill_id="f",
        opened_at=opened, quantity=Decimal("10"), average_entry_price=Decimal("100"),
        entry_atr=Decimal("5"), atr_period=14, initial_stop_multiple=Decimal("2"),
        initial_target_multiple=Decimal("3"), policy_version="v", config_hash="cfg",
        source_market_data_id="bar-1",
    )
    transition = advance_lifecycle_state(
        state, as_of=datetime(2026, 1, 2, 21, tzinfo=timezone.utc),
        reference_price=Decimal("111"), price_is_stale=False, price_point_in_time_safe=True,
        current_atr=None, source_market_data_id="bar-2", breakeven_enabled=True,
        breakeven_activation_r_multiple=Decimal("1"), breakeven_offset_bps=Decimal("0"),
        trailing_enabled=True, trailing_activation_r_multiple=Decimal("1"),
        trailing_atr_multiple=Decimal("2"),
    )
    assert transition.state.current_stop_price == Decimal("100")
    assert STOP_REASON_BREAKEVEN in transition.reasons
    assert EVALUATION_INCOMPLETE_ATR in transition.reasons
    assert transition.complete is False


def test_partial_close_uses_original_quantity_and_never_fabricates_share():
    assert calculate_partial_close_quantity(
        original_quantity=Decimal("5"), close_fraction=Decimal("0.10"),
        available_unreserved_quantity=Decimal("5"), current_remaining_quantity=Decimal("5"),
        minimum_remaining_quantity=Decimal("1"),
    ) == 0
    assert calculate_partial_close_quantity(
        original_quantity=Decimal("10"), close_fraction=Decimal("0.50"),
        available_unreserved_quantity=Decimal("4"), current_remaining_quantity=Decimal("6"),
        minimum_remaining_quantity=Decimal("1"),
    ) == Decimal("4")
