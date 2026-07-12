"""Shared fixture builders for Milestone 3 (paper-execution) tests.

`buy_candidate_payload` returns a plain dict shaped exactly like what
`storage.trading_repositories.load_recommendation` returns (i.e. a frozen
recommendation's full payload) — tests mutate individual fields via
`**overrides` to build ineligible/edge-case scenarios without needing a
live sqlite connection or the full screener/scorer/risk-engine pipeline
for every scenario. `persist_buy_candidate` is used by the handful of
tests (mainly integration tests) that need a real row in a real
connection, e.g. to exercise `execute_paper_recommendation`'s own
`load_recommendation` call.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

CONFIG_HASH = "a" * 64
GIT_SHA = "abc1234567"


def buy_candidate_payload(
    *,
    rec_id: str = "rec-fixture-0001",
    symbol: str = "SOFI",
    now: datetime | None = None,
    shares: int = 70,
    entry_price: float = 14.25,
    market_data_age_seconds: float = 30.0,
    frozen: bool = True,
    status: str = "active",
    side: str = "buy_candidate",
    **overrides,
) -> dict:
    now = now or datetime(2026, 7, 11, 14, 0, 0, tzinfo=timezone.utc)
    market_ts = now.timestamp() - market_data_age_seconds
    payload = {
        "rec_id": rec_id,
        "run_id": "run-fixture-1",
        "symbol": symbol,
        "side": side,
        "ts": now.isoformat(),
        "price_at_rec": entry_price,
        "score": 62.5,
        "confidence": "medium",
        "status": status,
        "acted": False,
        "rationale_text": "Fixture recommendation for Milestone 3 tests.",
        "factors": [
            {"factor": "revenue_growth_yoy", "raw_value": 0.31, "normalized": 0.62, "weight": 0.35,
             "contribution": 21.7},
        ],
        "risk_plan": {
            "shares": shares,
            "entry_price": entry_price,
            "stop_price": round(entry_price * 0.92, 2),
            "target_price": round(entry_price * 1.16, 2),
            "risk_per_share": round(entry_price * 0.08, 2),
            "dollars_at_risk": round(shares * entry_price * 0.08, 2),
            "position_value": round(shares * entry_price, 2),
            "reward_risk": 2.0,
            "warnings": [],
        } if side == "buy_candidate" else None,
        "warnings": [],
        "missing_data_reasons": [],
        "data_timestamps": {
            "market": datetime.fromtimestamp(market_ts, tz=timezone.utc).isoformat(),
        },
        "reddit_component": None,
        "model_version": "m1",
        "prompt_version": "p1",
        "config_hash": CONFIG_HASH,
        "git_sha": GIT_SHA,
        "frozen": frozen,
        "disclaimer": "Research output only. Not financial advice. Not an instruction to trade.",
    }
    payload.update(overrides)
    return payload


def insert_recommendation_row(conn: sqlite3.Connection, payload: dict) -> None:
    """Insert a recommendation row (including `payload_json`) directly —
    bypasses `build_recommendation`'s schema validation so tests can persist
    intentionally-invalid/edge-case payloads (e.g. unfrozen, expired) that
    the real builder would refuse to construct."""
    import json

    conn.execute(
        "INSERT INTO recommendations "
        "(rec_id, run_id, symbol, side, ts, price_at_rec, score, confidence, status, acted, "
        "rationale_text, model_version, prompt_version, config_hash, git_sha, frozen, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            payload["rec_id"], payload["run_id"], payload["symbol"], payload["side"], payload["ts"],
            payload["price_at_rec"], payload["score"], payload["confidence"], payload["status"],
            int(payload["acted"]), payload["rationale_text"], payload["model_version"],
            payload["prompt_version"], payload["config_hash"], payload["git_sha"], int(payload["frozen"]),
            json.dumps(payload),
        ),
    )
    conn.commit()
