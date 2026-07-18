from pathlib import Path

import pytest

from dashboard.services.cycle_service import CycleService


def test_lists_persisted_cycle_provider_partition_and_scheduler_counts(dashboard_database: Path):
    cycles = CycleService(dashboard_database).list_cycles()

    assert len(cycles) == 1
    cycle = cycles[0]
    assert cycle.scheduler_run_id == "scheduler-1"
    assert cycle.symbols_total == 2
    assert cycle.symbols_completed == 2
    assert cycle.research_provider_partitions == ("fixture / fixture-model / fixture",)


def test_cycle_detail_has_funnel_and_ticker_drill_down(dashboard_database: Path):
    detail = CycleService(dashboard_database).get_cycle_detail("cycle-1")

    assert detail is not None
    assert detail.funnel.selected == 2
    assert detail.funnel.buy_candidates == 2
    assert detail.funnel.policy_rejected == 1
    assert detail.funnel.paper_submitted == 1
    assert detail.funnel.filled == 1
    assert [decision.symbol for decision in detail.decisions] == ["ABC", "XYZ"]


def test_cycle_queries_are_bounded(empty_dashboard_database: Path):
    with pytest.raises(ValueError, match="between 1 and 200"):
        CycleService(empty_dashboard_database).list_cycles(limit=201)
