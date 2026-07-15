"""Persistence for the trading-desk analysis layer: screening runs,
candidate scores, and frozen recommendations (with their factors).

Kept separate from storage/repositories.py (the research-pipeline
repositories over migrations.py's tables) since these operate on
trading_schema.py's tables. Deliberately has no function that writes to
`real_orders` — that table is reserved and DB triggers reject every write
regardless (see storage/trading_schema.py), but this module also never
attempts one, by construction.
"""
from __future__ import annotations

import json
import sqlite3

from ..analysis.scorer import CompositeScore
from ..recommendations.builder import FrozenRecommendation


def save_screening_run(
    conn: sqlite3.Connection,
    run_id: str,
    config_hash: str,
    universe_count: int,
    passed_count: int,
    ran_at: str,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO screening_runs (run_id, ran_at, config_hash, universe_count, passed_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, ran_at, config_hash, universe_count, passed_count),
    )
    conn.commit()


def save_candidate_score(conn: sqlite3.Connection, run_id: str, score: CompositeScore) -> None:
    pillars = {p.pillar: p.pillar_score for p in score.pillars}
    conn.execute(
        "INSERT OR REPLACE INTO candidate_scores "
        "(run_id, symbol, fundamentals_score, technicals_score, catalyst_score, reddit_component, total, rank) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            run_id, score.symbol, pillars.get("fundamentals"), pillars.get("technicals"),
            pillars.get("catalysts"), pillars.get("reddit"), score.total_score,
        ),
    )
    conn.commit()


def recommendation_exists(conn: sqlite3.Connection, rec_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM recommendations WHERE rec_id = ?", (rec_id,)).fetchone()
    return row is not None


def save_frozen_recommendation(conn: sqlite3.Connection, rec: FrozenRecommendation) -> bool:
    """Persist a frozen recommendation and its factors in one transaction.

    Returns True if this was a new insert, False if `rec_id` already existed
    (an idempotent no-op — retried creation never conflicts, never
    duplicates). On any failure partway through, the transaction is rolled
    back so no partial recommendation is ever left behind.
    """
    p = rec.payload
    if recommendation_exists(conn, p["rec_id"]):
        return False

    try:
        conn.execute(
            "INSERT INTO recommendations "
            "(rec_id, run_id, symbol, side, ts, price_at_rec, score, confidence, status, acted, "
            "rationale_text, model_version, prompt_version, config_hash, git_sha, frozen, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                p["rec_id"], p["run_id"], p["symbol"], p["side"], p["ts"], p["price_at_rec"], p["score"],
                p["confidence"], p["status"], int(p["acted"]), p["rationale_text"], p["model_version"],
                p["prompt_version"], p["config_hash"], p["git_sha"], json.dumps(p),
            ),
        )
        for factor in p["factors"]:
            conn.execute(
                "INSERT INTO recommendation_factors (rec_id, factor, raw_value, normalized, weight, contribution) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    p["rec_id"], factor["factor"], factor.get("raw_value"), factor.get("normalized"),
                    factor.get("weight"), factor.get("contribution"),
                ),
            )
        conn.commit()
        return True
    except sqlite3.Error:
        conn.rollback()
        raise


def load_recommendation(conn: sqlite3.Connection, rec_id: str) -> dict | None:
    """Read back the full frozen payload for a recommendation (Milestone 3:
    paper-execution eligibility/intent construction needs risk_plan, which
    the flat `recommendations` columns never carried). Returns None if the
    recommendation does not exist, or if it was written before `payload_json`
    existed — callers must fail closed on None, never reconstruct a guess
    from the flat columns."""
    row = conn.execute(
        "SELECT payload_json FROM recommendations WHERE rec_id = ?", (rec_id,)
    ).fetchone()
    if row is None or row["payload_json"] is None:
        return None
    return json.loads(row["payload_json"])


def load_recommendation_factors(conn: sqlite3.Connection, rec_id: str) -> list[dict]:
    """Read back the persisted factor rows for a recommendation — used to
    independently reconstruct its score from storage alone (see
    tests/integration for the reconstruction proof)."""
    rows = conn.execute(
        "SELECT factor, raw_value, normalized, weight, contribution FROM recommendation_factors "
        "WHERE rec_id = ? ORDER BY factor",
        (rec_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_recommendations_by_symbol_since(
    conn: sqlite3.Connection, symbol: str, after_ts: str, upto_ts: str
) -> list[dict]:
    """Frozen recommendations for `symbol` strictly newer than `after_ts` and
    at-or-before `upto_ts` (ISO8601 strings, lexically comparable), newest
    first — Milestone 9's point-in-time-safe recommendation-reversal lookup
    (docs/milestone-9.md Section 3: `position_opened_at < ts <= as_of`).
    Flat columns only (rec_id/side/status/ts); never the full payload_json,
    since a reversal check only needs the frozen side/status classification."""
    rows = conn.execute(
        "SELECT rec_id, symbol, side, status, ts FROM recommendations "
        "WHERE symbol = ? AND ts > ? AND ts <= ? ORDER BY ts DESC",
        (symbol, after_ts, upto_ts),
    ).fetchall()
    return [dict(r) for r in rows]
