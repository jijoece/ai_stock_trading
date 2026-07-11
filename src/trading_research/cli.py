"""CLI for the research pipeline proof of concept.

Commands:
  analyze <TICKER>   End-to-end single-ticker analysis on MOCKED data:
                     universe check → reddit mention aggregation → risk plan →
                     schema-validated frozen recommendation JSON (printed).
  paper-status       Show the paper ledger's cash, positions, and last snapshot.

Everything runs offline on deterministic fixtures. No broker call, no order,
no network. Output is research only — never an instruction to trade.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft7Validator

from .analysis.sentiment import KeywordClassifier, aggregate
from .analysis.ticker_extractor import extract_mentions
from .config import REPO_ROOT, load_config
from .mcp.mock_adapters import MockRedditAdapter, MockRobinhoodAdapter
from .risk.position_sizing import IncompleteStateError, RiskInputs, compute_position_plan
from .storage.database import session
from .universe.tickers import default_universe

DISCLAIMER = "Research output only. Not financial advice. Not an instruction to trade."

REDDIT_WEIGHT = 0.10  # hard cap per architecture §14


def _load_schema() -> Draft7Validator:
    schema_path = REPO_ROOT / "schemas" / "recommendation.schema.json"
    return Draft7Validator(json.loads(schema_path.read_text()))


def _config_hash() -> str:
    """SHA-256 over the non-secret configuration relevant to a run.

    Secrets are deliberately excluded — this hash appears in frozen
    recommendation records and must never leak credential material.
    """
    cfg = load_config()
    public = {
        "anthropic_model": cfg.anthropic_model,
        "reddit_mcp_mode": cfg.reddit_mcp_mode,
        "robinhood_mcp_url": cfg.robinhood_mcp_url,
        "reddit_weight_cap": REDDIT_WEIGHT,
    }
    return hashlib.sha256(json.dumps(public, sort_keys=True).encode()).hexdigest()


def _git_sha() -> str:
    """Current commit for reproducibility; 'unknown' when git is unavailable."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
        )
        sha = proc.stdout.strip()
        if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{7,40}", sha):
            return sha
    except OSError:
        pass
    return "unknown"


def analyze(symbol: str) -> dict:
    symbol = symbol.upper()
    universe = default_universe()
    now = time.time()
    ts = datetime.now(timezone.utc).isoformat()

    rec: dict = {
        "rec_id": str(uuid.uuid4()),
        "run_id": None,
        "symbol": symbol if symbol in universe else "XXXXX",
        "side": "no_action",
        "ts": ts,
        "price_at_rec": None,
        "score": None,
        "confidence": None,
        "status": "analysis_incomplete",
        "acted": False,
        "rationale_text": "",
        "factors": [],
        "risk_plan": None,
        "warnings": [],
        "missing_data_reasons": [],
        "data_timestamps": {},
        "reddit_component": None,
        "model_version": "poc-0.1",
        "prompt_version": "none-deterministic",
        "config_hash": _config_hash(),
        "git_sha": _git_sha(),
        "frozen": True,
        "disclaimer": DISCLAIMER,
    }

    if not universe.is_valid(symbol):
        rec["warnings"].append(f"{symbol} is not in the verified ticker universe — rejected")
        rec["missing_data_reasons"].append("symbol not in verified ticker universe")
        rec["symbol"] = symbol if universe.get(symbol) else "XXXXX"
        return rec

    robinhood = MockRobinhoodAdapter(now_epoch=now)
    reddit = MockRedditAdapter(now_epoch=now)

    # Market data (mocked)
    try:
        quote = robinhood.get_equity_quote(symbol)
        rec["price_at_rec"] = quote.mid
        rec["data_timestamps"]["quote(mock)"] = datetime.fromtimestamp(
            quote.as_of_epoch, timezone.utc
        ).isoformat()
    except KeyError:
        rec["warnings"].append("no market data available — fail closed")
        rec["missing_data_reasons"].append("no market quote available")
        rec["status"] = "analysis_incomplete"
        rec["side"] = "analysis_incomplete"
        return rec

    try:
        fundamentals = robinhood.get_equity_fundamentals(symbol)
        rec["data_timestamps"]["fundamentals(mock)"] = ts
    except KeyError:
        fundamentals = None

    # Reddit mention aggregation (deterministic; records are fixtures)
    records = reddit.fetch_records(symbol)
    day = 86_400.0
    agg = aggregate(records, symbol, now - day, now, KeywordClassifier())
    mention_confirmed = any(
        m.counted for r in records for m in extract_mentions(r.text, universe) if m.symbol == symbol
    )
    if records and not mention_confirmed:
        rec["warnings"].append("reddit records present but no counted mention survived extraction rules")
    rec["reddit_component"] = {
        "weight": REDDIT_WEIGHT,
        "net_sentiment": round(agg.net_sentiment, 3),
        "total_mentions": agg.total_mentions,
        "unique_authors": agg.unique_authors,
        "note": "sentiment, not fact; untrusted source; capped at 10% of score",
    }
    rec["data_timestamps"]["reddit(mock)"] = ts

    # Toy composite score for the PoC: fundamentals gate + reddit cap only.
    base_score = 50.0
    factors = []
    if fundamentals:
        growth = fundamentals.get("revenue_growth_yoy", 0.0)
        growth_norm = max(-1.0, min(1.0, growth / 0.5))
        factors.append(
            {"factor": "revenue_growth_yoy", "raw_value": growth, "normalized": growth_norm,
             "weight": 0.35, "contribution": round(35 * growth_norm, 2)}
        )
        base_score += 35 * growth_norm * 0.5
    reddit_contrib = 10 * agg.net_sentiment
    factors.append(
        {"factor": "reddit_net_sentiment", "raw_value": agg.net_sentiment,
         "normalized": agg.net_sentiment, "weight": REDDIT_WEIGHT,
         "contribution": round(reddit_contrib, 2)}
    )
    base_score += reddit_contrib * 0.5
    rec["factors"] = factors
    rec["score"] = round(max(0.0, min(100.0, base_score)), 1)

    # Deterministic risk plan (fail-closed)
    account = robinhood.get_account_state()
    stop = round(quote.mid * 0.92, 2)  # PoC stop: 8% below mid (config later)
    try:
        plan = compute_position_plan(
            RiskInputs(
                account_equity=account["equity"],
                settled_cash=account["settled_cash"],
                entry_price=quote.mid,
                stop_price=stop,
                price_as_of_epoch=quote.as_of_epoch,
                now_epoch=now,
                avg_daily_dollar_volume=(fundamentals or {}).get("avg_daily_dollar_volume"),
                days_to_earnings=(fundamentals or {}).get("days_to_earnings"),
                earnings_date_known=bool(fundamentals and "days_to_earnings" in fundamentals),
            )
        )
        rec["risk_plan"] = {
            "shares": plan.shares,
            "entry_price": plan.entry_price,
            "stop_price": plan.stop_price,
            "target_price": plan.target_price,
            "risk_per_share": plan.risk_per_share,
            "dollars_at_risk": plan.dollars_at_risk,
            "position_value": plan.position_value,
            "reward_risk": plan.reward_risk,
            "warnings": list(plan.warnings),
        }
        rec["status"] = "active"
        rec["side"] = "watch" if not plan.actionable else "buy_candidate"
        rec["confidence"] = "low"  # PoC scoring is illustrative only
        rec["warnings"].extend(plan.warnings)
    except IncompleteStateError as exc:
        rec["status"] = "analysis_incomplete"
        rec["side"] = "analysis_incomplete"
        rec["risk_plan"] = None
        rec["warnings"].append(f"risk engine fail-closed: {exc}")
        rec["missing_data_reasons"].append(f"risk engine: {exc}")

    rec["rationale_text"] = (
        f"Deterministic PoC analysis of {symbol}: score {rec['score']} from "
        f"{len(factors)} stored factors; reddit net sentiment "
        f"{agg.net_sentiment:+.2f} over {agg.total_mentions} mentions "
        f"({agg.unique_authors} unique authors), capped at 10% weight. "
        f"All numbers computed by Python, none by an LLM."
    )
    return rec


def paper_status(db_path: Path) -> dict:
    from .paper.ledger import PaperLedger

    with session(db_path) as conn:
        ledger = PaperLedger(conn)
        snap = conn.execute(
            "SELECT * FROM simulated_portfolio_snapshots ORDER BY snap_date DESC LIMIT 1"
        ).fetchone()
        return {
            "settled_cash": round(ledger.settled_cash(), 2),
            "total_cash": round(ledger.total_cash(), 2),
            "open_positions": ledger.positions(),
            "last_snapshot": dict(snap) if snap else None,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trading-research", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="Analyze one ticker on mocked data")
    p_analyze.add_argument("ticker")

    sub.add_parser("paper-status", help="Show paper ledger state")

    args = parser.parse_args(argv)

    if args.command == "analyze":
        rec = analyze(args.ticker)
        validator = _load_schema()
        errors = sorted(validator.iter_errors(rec), key=lambda e: e.json_path)
        if errors:
            for e in errors:
                print(f"SCHEMA ERROR at {e.json_path}: {e.message}", file=sys.stderr)
            return 2
        print(json.dumps(rec, indent=2))
        return 0

    if args.command == "paper-status":
        cfg = load_config()
        print(json.dumps(paper_status(cfg.research_database_path), indent=2, default=str))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
