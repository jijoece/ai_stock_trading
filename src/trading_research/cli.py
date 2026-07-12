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
                existing_position_shares=0,
                portfolio_exposure_fraction=0.0,
                account_state_as_of_epoch=account["as_of_epoch"],
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


def _paper_runtime_command_env() -> dict:
    """Adds this repository's `paper_runtime/src` to PYTHONPATH so the
    default `config/paper_runtime.yaml` command (`python3 -m
    trading_paper_runtime`) works out of the box in this development
    checkout, without requiring a separate `pip install` step. A real
    deployment overrides `paper_runtime.command` in the config to point at
    an isolated virtualenv's interpreter instead — this convenience wiring
    changes nothing about process isolation (the main process still never
    imports lumibot itself)."""
    import os

    env = os.environ.copy()
    paper_runtime_src = REPO_ROOT / "paper_runtime" / "src"
    if paper_runtime_src.is_dir():
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(paper_runtime_src) + (os.pathsep + existing if existing else "")
    return env


def _build_runtime_client():
    from .runtime.client.process_client import RuntimeClient
    from .runtime.paper_runtime_config import load_paper_runtime_config

    config = load_paper_runtime_config()
    client = RuntimeClient(
        command=list(config.command), startup_timeout_seconds=config.startup_timeout_seconds,
        request_timeout_seconds=config.request_timeout_seconds, cwd=str(REPO_ROOT),
        env=_paper_runtime_command_env(),
    )
    return client, config


def paper_runtime_health() -> dict:
    """`paper-runtime health` CLI command (docs/milestone-4.md Step 15).
    Starts the isolated runtime, health-checks it, and reports the result —
    never prints a credential value, only presence booleans (from the
    runtime's own `health` response)."""
    client, config = _build_runtime_client()
    try:
        client.start()
    except Exception as exc:
        return {"broker_provider": config.broker_provider, "available": False, "error": str(exc)}
    try:
        return {
            "broker_provider": config.broker_provider, "available": True,
            "health": client.last_health, "capabilities": client.last_capabilities,
        }
    finally:
        client.shutdown()


def sync_paper_orders_cli(db_path: Path) -> dict:
    """`sync-paper-orders` CLI command (docs/milestone-4.md Step 15). One
    bounded pass over every unresolved credentialed submission — does not
    loop and does not imply this process keeps running after it exits."""
    from datetime import datetime, timezone

    from .paper.ledger import PaperLedger
    from .services.sync_paper_orders import sync_paper_orders

    client, _config = _build_runtime_client()
    with session(db_path) as conn:
        try:
            client.start()
        except Exception as exc:
            return {"error": f"paper runtime unavailable: {exc}"}
        try:
            ledger = PaperLedger(conn)
            outcomes = sync_paper_orders(
                conn=conn, ledger=ledger, client=client, clock=lambda: datetime.now(timezone.utc),
            )
        finally:
            client.shutdown()
        return {
            "synced": [
                {
                    "intent_id": o.intent_id, "outcome": o.outcome,
                    "submission_status": o.submission_status, "new_events": o.new_events,
                }
                for o in outcomes
            ]
        }


def reconcile_paper_cli(db_path: Path) -> dict:
    """`reconcile-paper` CLI command (docs/milestone-4.md Step 15). Account
    and per-symbol position reconciliation against the credentialed paper
    broker — reports mismatches, never silently repairs them."""
    from datetime import datetime, timezone

    from .paper.ledger import PaperLedger
    from .services.reconcile_paper import reconcile_paper_account_and_positions

    client, _config = _build_runtime_client()
    with session(db_path) as conn:
        try:
            client.start()
        except Exception as exc:
            return {"error": f"paper runtime unavailable: {exc}"}
        try:
            ledger = PaperLedger(conn)
            report = reconcile_paper_account_and_positions(
                conn=conn, ledger=ledger, client=client, clock=lambda: datetime.now(timezone.utc),
            )
        finally:
            client.shutdown()
        return {
            "account": {
                "status": report.account.status, "broker_cash": str(report.account.broker_cash),
                "ledger_cash": str(report.account.ledger_cash), "reasons": list(report.account.reasons),
            },
            "positions": [
                {
                    "symbol": p.symbol, "status": p.status, "broker_quantity": str(p.broker_quantity),
                    "ledger_quantity": str(p.ledger_quantity), "reasons": list(p.reasons),
                }
                for p in report.positions
            ],
        }


def evaluate_recommendations_cli(db_path: Path, recommendation_ids: list[str]) -> dict:
    """`evaluate-recommendations` CLI command (docs/milestone-4.md Step 15).

    No live historical-price data source ships in this milestone (see
    `evaluation/price_provider.py`'s module docstring) — this command uses
    an empty `DeterministicPriceProvider` by default, so every horizon will
    correctly report `DELISTED_OR_UNAVAILABLE`/missing-data rather than a
    fabricated price. It exists to prove the persistence/orchestration path
    end-to-end; a future milestone wires in a real point-in-time price
    source without changing any other code in this path.
    """
    from datetime import datetime, timezone
    from decimal import Decimal

    from .evaluation.evaluation_service import evaluate_recommendation_all_horizons
    from .evaluation.price_provider import DeterministicPriceProvider
    from .execution.config import load_execution_config
    from .runtime.paper_runtime_config import load_paper_runtime_config
    from .storage import evaluation_repositories as eval_repo
    from .storage import execution_repositories as exec_repo
    from .storage.trading_repositories import load_recommendation

    exec_config = load_execution_config()
    runtime_config = load_paper_runtime_config()
    now = datetime.now(timezone.utc)
    price_provider = DeterministicPriceProvider()
    results = {}

    with session(db_path) as conn:
        for rec_id in recommendation_ids:
            recommendation = load_recommendation(conn, rec_id)
            if recommendation is None:
                results[rec_id] = {"error": "recommendation not found"}
                continue

            result = None
            intent = exec_repo.get_intent_by_recommendation(conn, rec_id, exec_config.execution_version)
            if intent is not None:
                result = exec_repo.get_result(conn, intent.intent_id)

            evaluations = evaluate_recommendation_all_horizons(
                recommendation_id=rec_id, symbol=recommendation["symbol"],
                recommendation_price=Decimal(str(recommendation["price_at_rec"])) if recommendation.get("price_at_rec") is not None else None,
                execution_price=result.average_fill_price if result else None,
                filled_quantity=result.filled_quantity if result else 0,
                requested_quantity=result.requested_quantity if result else 0,
                execution_completed_at=result.completed_at if result else None,
                price_provider=price_provider, now=now,
                horizons=runtime_config.evaluation_horizons_trading_days,
                benchmark_symbol=runtime_config.evaluation_benchmark,
                model_version=recommendation.get("model_version"), prompt_version=recommendation.get("prompt_version"),
                config_hash=recommendation.get("config_hash"),
            )
            for evaluation in evaluations:
                eval_repo.save_evaluation(conn, evaluation)
            results[rec_id] = [
                {"horizon_trading_days": e.horizon_trading_days, "status": e.status,
                 "net_return": str(e.net_return) if e.net_return is not None else None}
                for e in evaluations
            ]

    return {"benchmark": runtime_config.evaluation_benchmark, "evaluations": results}


def paper_performance_cli(db_path: Path) -> dict:
    """`paper-performance` CLI command (docs/milestone-4.md Step 15).
    Aggregate metrics over every persisted evaluation — insufficient-data
    and undefined metrics are reported explicitly, never as a misleading
    zero (see `evaluation/metrics.py`)."""
    from .evaluation import metrics
    from .storage import evaluation_repositories as eval_repo

    with session(db_path) as conn:
        evaluations = eval_repo.list_all_evaluations(conn)

    def _fmt(result) -> dict:
        return {"status": result.status, "value": str(result.value) if result.value is not None else None,
                "sample_size": result.sample_size, "reason": result.reason}

    return {
        "hit_rate": _fmt(metrics.hit_rate(evaluations)),
        "average_return": _fmt(metrics.average_return(evaluations)),
        "median_return": _fmt(metrics.median_return(evaluations)),
        "gain_loss_ratio": _fmt(metrics.gain_loss_ratio(evaluations)),
        "cumulative_return": _fmt(metrics.cumulative_return(evaluations)),
        "benchmark_relative_cumulative_return": _fmt(metrics.benchmark_relative_cumulative_return(evaluations)),
        "sharpe_ratio": _fmt(metrics.sharpe_ratio(evaluations)),
        "sortino_ratio": _fmt(metrics.sortino_ratio(evaluations)),
        "max_drawdown": _fmt(metrics.max_drawdown(evaluations)),
        "calmar_ratio": _fmt(metrics.calmar_ratio(evaluations)),
    }


def execute_paper(recommendation_id: str, db_path: Path, *, adapter: str = "deterministic") -> dict:
    """Milestone 3/4 CLI entry point: run one frozen recommendation through
    paper-execution eligibility and intent construction, then either a
    deterministic fill (`--adapter deterministic`, the default) or a real
    credentialed paper-broker acknowledgement via the isolated runtime
    process (`--adapter credentialed`, Milestone 4).

    The deterministic path uses
    `runtime.deterministic_adapter.DeterministicPaperAdapter`,
    auto-registered here to fill immediately at the recommendation's own
    `price_at_rec` — a deterministic, offline stand-in, NOT a real LumiBot
    broker round trip (that requires credentials/network this CLI does not
    have; see `runtime/lumibot/adapter.py` and
    docs/milestone3-lumibot-paper-integration.md). This is disclosed in the
    returned dict's `adapter` field, never presented as a real fill.

    The credentialed path only acknowledges submission (docs/milestone-
    4.md's asynchronous submit/poll split) — run `sync-paper-orders`
    afterward to observe fills.
    """
    from datetime import datetime, timezone
    from decimal import Decimal

    from .execution.adapter_protocol import BrokerExecutionSnapshot
    from .execution.config import load_execution_config
    from .execution.eligibility import PaperExecutionEligibilityPolicy
    from .execution.models import PaperExecutionEvent, PaperExecutionResult, derive_intent_id
    from .paper.ledger import PaperLedger
    from .runtime.deterministic_adapter import DeterministicPaperAdapter
    from .services.execute_paper_recommendation import (
        RecommendationNotFoundError,
        execute_paper_recommendation,
    )
    from .storage.trading_repositories import load_recommendation
    from .universe.tickers import default_universe

    exec_config = load_execution_config()  # fails closed if trading_mode != paper (see execution/config.py)
    now = datetime.now(timezone.utc)

    if adapter == "credentialed":
        from .services.submit_credentialed_paper_order import submit_credentialed_paper_order

        client, _runtime_config = _build_runtime_client()
        with session(db_path) as conn:
            try:
                client.start()
            except Exception as exc:
                return {"mode": exec_config.trading_mode, "adapter": "credentialed", "error": f"paper runtime unavailable: {exc}"}
            try:
                policy = PaperExecutionEligibilityPolicy(universe=default_universe(), config=exec_config)
                try:
                    outcome = submit_credentialed_paper_order(
                        recommendation_id, conn=conn, execution_config=exec_config, eligibility_policy=policy,
                        client=client, git_sha=_git_sha(), clock=lambda: now,
                    )
                except RecommendationNotFoundError as exc:
                    return {"mode": exec_config.trading_mode, "adapter": "credentialed", "error": str(exc)}
            finally:
                client.shutdown()
            return {
                "mode": exec_config.trading_mode,
                "adapter": "credentialed-alpaca-paper (real broker acknowledgement via isolated runtime process)",
                "status": outcome.status,
                "eligibility_reasons": list(outcome.eligibility.reasons) if outcome.eligibility else [],
                "intent_id": outcome.intent.intent_id if outcome.intent else None,
                "client_order_id": outcome.submission.client_order_id if outcome.submission else None,
                "broker_order_id": outcome.submission.broker_order_id if outcome.submission else None,
                "submission_status": outcome.submission.submission_status if outcome.submission else None,
            }

    with session(db_path) as conn:
        recommendation = load_recommendation(conn, recommendation_id)
        adapter_impl = DeterministicPaperAdapter()

        if recommendation is not None:
            intent_id = derive_intent_id(recommendation_id, exec_config.execution_version)
            risk_plan = recommendation.get("risk_plan") or {}
            quantity = risk_plan.get("shares")
            price = recommendation.get("price_at_rec")
            if quantity and price:
                price_dec = Decimal(str(price))
                event = PaperExecutionEvent(
                    event_id=f"{intent_id}-cli-fill-1", intent_id=intent_id,
                    recommendation_id=recommendation_id, symbol=recommendation["symbol"],
                    event_type="FILLED", broker_order_id=f"cli-sim-{intent_id}", quantity=quantity,
                    filled_quantity=quantity, fill_price=price_dec, occurred_at=now, raw_status="fill",
                )
                result = PaperExecutionResult(
                    intent_id=intent_id, recommendation_id=recommendation_id, final_status="FILLED",
                    requested_quantity=quantity, filled_quantity=quantity, average_fill_price=price_dec,
                    fees=Decimal("0"), event_ids=(event.event_id,), completed_at=now,
                )
                adapter_impl.register(intent_id, (event,), result)
                adapter_impl.register_reconciliation(
                    intent_id,
                    BrokerExecutionSnapshot(
                        intent_id=intent_id, broker_quantity=quantity, broker_notional=price_dec * quantity,
                        broker_status="fill", as_of=now,
                    ),
                )

        ledger = PaperLedger(conn)
        policy = PaperExecutionEligibilityPolicy(universe=default_universe(), config=exec_config)

        try:
            outcome = execute_paper_recommendation(
                recommendation_id, conn=conn, execution_config=exec_config, ledger=ledger, adapter=adapter_impl,
                eligibility_policy=policy, git_sha=_git_sha(), clock=lambda: now,
            )
        except RecommendationNotFoundError as exc:
            return {"mode": exec_config.trading_mode, "error": str(exc)}

        return {
            "mode": exec_config.trading_mode,
            "adapter": "deterministic-cli-simulator (not a real LumiBot broker round trip)",
            "status": outcome.status,
            "eligibility_reasons": list(outcome.eligibility.reasons) if outcome.eligibility else [],
            "intent_id": outcome.intent.intent_id if outcome.intent else None,
            "result_status": outcome.result.final_status if outcome.result else None,
            "reconciliation_status": outcome.reconciliation.status if outcome.reconciliation else None,
        }


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

    p_execute_paper = sub.add_parser(
        "execute-paper", help="Run one frozen recommendation through paper execution (Milestone 3/4)"
    )
    p_execute_paper.add_argument("--recommendation-id", required=True)
    p_execute_paper.add_argument(
        "--adapter", choices=("deterministic", "credentialed"), default="deterministic",
        help="deterministic (default, offline) or credentialed (Milestone 4: real Alpaca paper "
             "broker acknowledgement via the isolated runtime process)",
    )

    sub.add_parser("paper-runtime-health", help="Health-check the isolated LumiBot paper-runtime process (Milestone 4)")

    sub.add_parser("sync-paper-orders", help="Poll the paper runtime for order-state changes and apply fills (Milestone 4)")

    sub.add_parser("reconcile-paper", help="Reconcile account/positions against the credentialed paper broker (Milestone 4)")

    p_evaluate = sub.add_parser(
        "evaluate-recommendations", help="Compute forward-performance evaluations for recommendations (Milestone 4)"
    )
    p_evaluate.add_argument("--recommendation-id", action="append", dest="recommendation_ids", required=True)

    sub.add_parser("paper-performance", help="Aggregate portfolio/strategy metrics over evaluations (Milestone 4)")

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

    if args.command == "execute-paper":
        cfg = load_config()
        outcome = execute_paper(args.recommendation_id, cfg.research_database_path, adapter=args.adapter)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-runtime-health":
        outcome = paper_runtime_health()
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if outcome.get("available") else 2

    if args.command == "sync-paper-orders":
        cfg = load_config()
        outcome = sync_paper_orders_cli(cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "reconcile-paper":
        cfg = load_config()
        outcome = reconcile_paper_cli(cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "evaluate-recommendations":
        cfg = load_config()
        outcome = evaluate_recommendations_cli(cfg.research_database_path, args.recommendation_ids)
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    if args.command == "paper-performance":
        cfg = load_config()
        outcome = paper_performance_cli(cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
