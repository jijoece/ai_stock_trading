from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_research.evaluation.evaluation_service import evaluate_recommendation
from trading_research.evaluation.price_provider import DeterministicPriceProvider
from trading_research.storage import evaluation_repositories as eval_repo
from trading_research.storage.database import connect

EXECUTION_AT = datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "eval_repo.sqlite3")
    yield c
    c.close()


def _provider():
    provider = DeterministicPriceProvider()
    provider.register("SOFI", date(2026, 7, 13), "14.25")
    provider.register("SOFI", date(2026, 7, 14), "14.80")
    provider.register("SPY", date(2026, 7, 13), "550.00")
    provider.register("SPY", date(2026, 7, 14), "552.00")
    return provider


def _evaluation(now):
    return evaluate_recommendation(
        recommendation_id="rec-1", symbol="SOFI", recommendation_price=Decimal("14.25"),
        execution_price=Decimal("14.30"), filled_quantity=70, requested_quantity=70,
        execution_completed_at=EXECUTION_AT, price_provider=_provider(), now=now, horizon_trading_days=1,
    )


def test_save_and_get_roundtrip(conn):
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    evaluation = _evaluation(now)
    eval_repo.save_evaluation(conn, evaluation)

    fetched = eval_repo.get_evaluation(conn, "rec-1", 1)
    assert fetched == evaluation


def test_recompute_upserts_rather_than_duplicates(conn):
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    eval_repo.save_evaluation(conn, _evaluation(now))
    eval_repo.save_evaluation(conn, _evaluation(now + timedelta(seconds=1)))

    count = conn.execute("SELECT COUNT(*) AS n FROM recommendation_evaluations").fetchone()["n"]
    assert count == 1


def test_list_evaluations_for_recommendation(conn):
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    eval_repo.save_evaluation(conn, _evaluation(now))
    results = eval_repo.list_evaluations_for_recommendation(conn, "rec-1")
    assert len(results) == 1
    assert results[0].horizon_trading_days == 1


def test_list_evaluations_by_status(conn):
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    eval_repo.save_evaluation(conn, _evaluation(now))
    completed = eval_repo.list_evaluations_by_status(conn, "COMPLETED")
    assert len(completed) == 1
    pending = eval_repo.list_evaluations_by_status(conn, "PENDING")
    assert pending == []
