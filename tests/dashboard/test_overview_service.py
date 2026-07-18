from decimal import Decimal
from pathlib import Path

from dashboard.services.overview_service import OverviewService


def test_overview_renders_latest_persisted_data_without_fabricated_zeroes(dashboard_database: Path):
    overview = OverviewService(dashboard_database).load()

    assert overview.portfolio_value == Decimal("9995")
    assert overview.cash == Decimal("9000")
    assert overview.reserved_cash == Decimal("500")
    assert overview.open_positions == 1
    assert overview.realized_pnl == Decimal("25")
    assert overview.unrealized_pnl == Decimal("5")
    assert overview.candidates_considered == 2
    assert overview.bought_or_submitted == 1
    assert overview.rejected == 1
    assert overview.incomplete == 0
    assert overview.pause_state == "RUNNING"
    assert overview.latest_scheduler_run_id == "scheduler-1"
    assert overview.latest_research_cycle_id == "cycle-1"


def test_overview_preserves_missing_values(empty_dashboard_database: Path):
    overview = OverviewService(empty_dashboard_database).load()

    assert overview.portfolio_value is None
    assert overview.cash is None
    assert overview.reserved_cash is None
    assert overview.open_positions is None
    assert overview.realized_pnl is None
    assert overview.unrealized_pnl is None
    assert overview.candidates_considered is None
    assert overview.bought_or_submitted is None
    assert overview.rejected is None
    assert overview.incomplete is None
