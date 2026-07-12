from datetime import datetime, timezone

import pytest

from trading_research.analysis.scorer import CompositeScore, FactorScore, PillarScore
from trading_research.analysis.screener import GateResult, ScreeningResult
from trading_research.recommendations.builder import (
    RecommendationBuildError,
    build_recommendation,
    derive_rec_id,
)
from trading_research.risk.position_sizing import PositionPlan
from trading_research.storage.database import connect
from trading_research.storage.trading_repositories import (
    recommendation_exists,
    save_frozen_recommendation,
)

NOW = datetime(2026, 7, 11, 14, 0, 0, tzinfo=timezone.utc)
CONFIG_HASH = "a" * 64

PASSING_SCREEN = ScreeningResult(
    symbol="SOFI", passed=True,
    gate_results=(GateResult("max_share_price", True, False, 25.0, 14.9, "ok", None),),
    config_hash=CONFIG_HASH, config_version=1, screened_at=NOW.isoformat(),
)
FAILING_SCREEN = ScreeningResult(
    symbol="SHELX", passed=False,
    gate_results=(GateResult("exclude_shell_company", False, True, False, True, "shell company", None),),
    config_hash=CONFIG_HASH, config_version=1, screened_at=NOW.isoformat(),
)


def make_score(total=62.5) -> CompositeScore:
    factor = FactorScore("revenue_growth_yoy", "fundamentals", 0.31, 0.62, 0.35, 21.7, None, "ok", "explained")
    pillar = PillarScore("fundamentals", 68.0, 0.35, 23.8, 1, 0)
    return CompositeScore(
        symbol="SOFI", total_score=total, pillars=(pillar,), factors=(factor,),
        config_hash=CONFIG_HASH, config_version=1, scored_at=NOW.isoformat(),
        warnings=(), reddit_materially_changed_score=False,
    )


ACTIONABLE_PLAN = PositionPlan(
    shares=70, entry_price=14.25, stop_price=13.11, target_price=16.53,
    risk_per_share=1.14, dollars_at_risk=79.8, position_value=997.5, reward_risk=2.0,
)
ZERO_SHARE_PLAN = PositionPlan(warnings=("no affordable size at policy limits",), no_action_reason="zero_shares_at_caps")


def build(**overrides):
    defaults = dict(
        symbol="SOFI", idempotency_key="test-key-1", run_id="run-1",
        screening_result=PASSING_SCREEN, composite_score=make_score(), position_plan=ACTIONABLE_PLAN,
        price_at_rec=14.25, data_timestamps={"quote(fixture)": NOW.isoformat()},
        warnings=[], missing_data_reasons=[], reddit_component=None,
        model_version="m1", prompt_version="p1", config_hash=CONFIG_HASH, now=NOW,
    )
    defaults.update(overrides)
    return build_recommendation(**defaults)


def test_passing_active_paper_recommendation():
    rec = build()
    assert rec.side == "buy_candidate"
    assert rec.status == "active"
    assert rec.payload["risk_plan"]["shares"] == 70


def test_screened_out_recommendation():
    rec = build(screening_result=FAILING_SCREEN, composite_score=None, position_plan=None)
    assert rec.side == "screened_out"
    assert rec.status == "active"
    assert rec.payload["risk_plan"] is None


def test_analysis_incomplete_from_missing_data_reasons():
    rec = build(missing_data_reasons=["no market quote available"], composite_score=None, position_plan=None)
    assert rec.side == "analysis_incomplete"
    assert rec.status == "analysis_incomplete"
    assert rec.payload["risk_plan"] is None
    assert rec.payload["missing_data_reasons"]


def test_analysis_incomplete_when_score_missing():
    rec = build(composite_score=None, position_plan=None)
    assert rec.status == "analysis_incomplete"
    assert "composite score unavailable" in rec.payload["missing_data_reasons"]


def test_no_action_from_zero_share_plan():
    rec = build(position_plan=ZERO_SHARE_PLAN)
    assert rec.side == "no_action"
    assert rec.status == "active"
    assert rec.payload["risk_plan"] is None


def test_forbidden_executable_fields_on_incomplete_status():
    rec = build(missing_data_reasons=["no market quote available"], composite_score=None, position_plan=None)
    assert rec.payload["risk_plan"] is None
    assert rec.payload["score"] is None


def test_missing_configuration_hash_rejected():
    with pytest.raises(RecommendationBuildError, match="config_hash"):
        build(config_hash="not-a-hash")


def test_json_schema_validation_enforced():
    # symbol violates the schema's ^[A-Z]{1,5}$ pattern
    with pytest.raises(RecommendationBuildError):
        build(symbol="toolongsymbol")


def test_reddit_component_over_cap_rejected():
    with pytest.raises(RecommendationBuildError, match="reddit"):
        build(reddit_component={"weight": 0.5, "net_sentiment": 0.2, "total_mentions": 3, "unique_authors": 2})


def test_immutability_no_update_method():
    rec = build()
    assert not hasattr(rec, "update")
    with pytest.raises(Exception):
        rec.payload = {}  # frozen dataclass: attribute assignment must fail


def test_rec_id_deterministic_from_idempotency_key():
    assert derive_rec_id("same-key") == derive_rec_id("same-key")
    assert derive_rec_id("key-a") != derive_rec_id("key-b")


def test_duplicate_idempotency_key_does_not_conflict(tmp_path):
    conn = connect(tmp_path / "dup.sqlite3")
    rec1 = build(idempotency_key="dup-key")
    rec2 = build(idempotency_key="dup-key")
    assert rec1.rec_id == rec2.rec_id

    inserted_first = save_frozen_recommendation(conn, rec1)
    inserted_second = save_frozen_recommendation(conn, rec2)
    assert inserted_first is True
    assert inserted_second is False  # idempotent no-op, not a conflicting duplicate

    count = conn.execute("SELECT COUNT(*) AS n FROM recommendations WHERE rec_id = ?", (rec1.rec_id,)).fetchone()["n"]
    assert count == 1
    conn.close()


def test_transaction_rollback_on_persistence_failure(tmp_path):
    import sqlite3

    conn = connect(tmp_path / "rollback.sqlite3")
    # Two factors sharing the same name violate recommendation_factors' PK
    # (rec_id, factor) on the second INSERT — a real mid-transaction failure
    # after the recommendations row has already been written.
    dup_factor = FactorScore("revenue_growth_yoy", "fundamentals", 0.1, 0.1, 0.35, 3.5, None, "ok", "x")
    score = make_score()
    broken_score = CompositeScore(
        symbol=score.symbol, total_score=score.total_score, pillars=score.pillars,
        factors=(dup_factor, dup_factor), config_hash=score.config_hash, config_version=score.config_version,
        scored_at=score.scored_at, warnings=score.warnings,
        reddit_materially_changed_score=score.reddit_materially_changed_score,
    )
    rec = build(idempotency_key="rollback-key", composite_score=broken_score)

    with pytest.raises(sqlite3.IntegrityError):
        save_frozen_recommendation(conn, rec)

    assert recommendation_exists(conn, rec.rec_id) is False
    conn.close()


def test_no_real_order_write_path():
    import re

    import trading_research.storage.trading_repositories as repo_module
    import trading_research.recommendations.builder as builder_module

    sql_write_pattern = re.compile(r"(INSERT|UPDATE|DELETE)\s+(INTO\s+|FROM\s+)?real_orders", re.IGNORECASE)
    for module in (repo_module, builder_module):
        source = open(module.__file__).read()
        assert not sql_write_pattern.search(source), f"{module.__name__} contains a write to real_orders"
