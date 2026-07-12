"""Persistence for Milestone 4 forward-performance evaluations
(`storage/evaluation_schema.py`'s tables). `save_evaluation` is an upsert
keyed by `(recommendation_id, horizon_trading_days)` — recomputing an
evaluation (e.g. once a horizon that was `PENDING` finally has price data)
overwrites the prior row rather than duplicating it, matching
`evaluate_recommendation`'s idempotent-recomputation contract.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

from ..evaluation.models import RecommendationEvaluation


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _iso_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def save_evaluation(conn: sqlite3.Connection, evaluation: RecommendationEvaluation) -> None:
    conn.execute(
        "INSERT INTO recommendation_evaluations "
        "(recommendation_id, horizon_trading_days, status, evaluation_date, benchmark_symbol, "
        "recommendation_price, execution_price, benchmark_price_at_execution, ending_symbol_price, "
        "ending_benchmark_price, gross_return, net_return, benchmark_return, excess_return, slippage, "
        "fees, max_favorable_excursion, max_adverse_excursion, missing_data_reasons_json, model_version, "
        "prompt_version, config_hash, market_regime, price_source_as_of, evaluated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (recommendation_id, horizon_trading_days) DO UPDATE SET "
        "status=excluded.status, evaluation_date=excluded.evaluation_date, "
        "recommendation_price=excluded.recommendation_price, execution_price=excluded.execution_price, "
        "benchmark_price_at_execution=excluded.benchmark_price_at_execution, "
        "ending_symbol_price=excluded.ending_symbol_price, ending_benchmark_price=excluded.ending_benchmark_price, "
        "gross_return=excluded.gross_return, net_return=excluded.net_return, "
        "benchmark_return=excluded.benchmark_return, excess_return=excluded.excess_return, "
        "slippage=excluded.slippage, fees=excluded.fees, "
        "max_favorable_excursion=excluded.max_favorable_excursion, "
        "max_adverse_excursion=excluded.max_adverse_excursion, "
        "missing_data_reasons_json=excluded.missing_data_reasons_json, "
        "price_source_as_of=excluded.price_source_as_of, evaluated_at=excluded.evaluated_at",
        (
            evaluation.recommendation_id, evaluation.horizon_trading_days, evaluation.status,
            evaluation.evaluation_date.isoformat(), evaluation.benchmark_symbol,
            str(evaluation.recommendation_price) if evaluation.recommendation_price is not None else None,
            str(evaluation.execution_price) if evaluation.execution_price is not None else None,
            str(evaluation.benchmark_price_at_execution) if evaluation.benchmark_price_at_execution is not None else None,
            str(evaluation.ending_symbol_price) if evaluation.ending_symbol_price is not None else None,
            str(evaluation.ending_benchmark_price) if evaluation.ending_benchmark_price is not None else None,
            str(evaluation.gross_return) if evaluation.gross_return is not None else None,
            str(evaluation.net_return) if evaluation.net_return is not None else None,
            str(evaluation.benchmark_return) if evaluation.benchmark_return is not None else None,
            str(evaluation.excess_return) if evaluation.excess_return is not None else None,
            str(evaluation.slippage) if evaluation.slippage is not None else None,
            str(evaluation.fees),
            str(evaluation.max_favorable_excursion) if evaluation.max_favorable_excursion is not None else None,
            str(evaluation.max_adverse_excursion) if evaluation.max_adverse_excursion is not None else None,
            json.dumps(list(evaluation.missing_data_reasons)), evaluation.model_version,
            evaluation.prompt_version, evaluation.config_hash, evaluation.market_regime,
            evaluation.price_source_as_of, evaluation.evaluated_at.isoformat(),
        ),
    )
    conn.commit()


def _row_to_evaluation(row: sqlite3.Row) -> RecommendationEvaluation:
    return RecommendationEvaluation(
        recommendation_id=row["recommendation_id"], horizon_trading_days=row["horizon_trading_days"],
        status=row["status"], evaluation_date=date.fromisoformat(row["evaluation_date"]),
        benchmark_symbol=row["benchmark_symbol"], recommendation_price=_dec(row["recommendation_price"]),
        execution_price=_dec(row["execution_price"]),
        benchmark_price_at_execution=_dec(row["benchmark_price_at_execution"]),
        ending_symbol_price=_dec(row["ending_symbol_price"]),
        ending_benchmark_price=_dec(row["ending_benchmark_price"]), gross_return=_dec(row["gross_return"]),
        net_return=_dec(row["net_return"]), benchmark_return=_dec(row["benchmark_return"]),
        excess_return=_dec(row["excess_return"]), slippage=_dec(row["slippage"]), fees=Decimal(row["fees"]),
        max_favorable_excursion=_dec(row["max_favorable_excursion"]),
        max_adverse_excursion=_dec(row["max_adverse_excursion"]),
        missing_data_reasons=tuple(json.loads(row["missing_data_reasons_json"])),
        model_version=row["model_version"], prompt_version=row["prompt_version"], config_hash=row["config_hash"],
        market_regime=row["market_regime"], price_source_as_of=row["price_source_as_of"],
        evaluated_at=_iso_dt(row["evaluated_at"]),
    )


def get_evaluation(conn: sqlite3.Connection, recommendation_id: str, horizon_trading_days: int) -> RecommendationEvaluation | None:
    row = conn.execute(
        "SELECT * FROM recommendation_evaluations WHERE recommendation_id = ? AND horizon_trading_days = ?",
        (recommendation_id, horizon_trading_days),
    ).fetchone()
    return _row_to_evaluation(row) if row else None


def list_evaluations_for_recommendation(conn: sqlite3.Connection, recommendation_id: str) -> list[RecommendationEvaluation]:
    rows = conn.execute(
        "SELECT * FROM recommendation_evaluations WHERE recommendation_id = ? ORDER BY horizon_trading_days",
        (recommendation_id,),
    ).fetchall()
    return [_row_to_evaluation(r) for r in rows]


def list_evaluations_by_status(conn: sqlite3.Connection, status: str) -> list[RecommendationEvaluation]:
    rows = conn.execute(
        "SELECT * FROM recommendation_evaluations WHERE status = ? ORDER BY recommendation_id, horizon_trading_days",
        (status,),
    ).fetchall()
    return [_row_to_evaluation(r) for r in rows]


def list_all_evaluations(conn: sqlite3.Connection, *, horizon_trading_days: int | None = None) -> list[RecommendationEvaluation]:
    if horizon_trading_days is None:
        rows = conn.execute(
            "SELECT * FROM recommendation_evaluations ORDER BY recommendation_id, horizon_trading_days"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM recommendation_evaluations WHERE horizon_trading_days = ? ORDER BY recommendation_id",
            (horizon_trading_days,),
        ).fetchall()
    return [_row_to_evaluation(r) for r in rows]
