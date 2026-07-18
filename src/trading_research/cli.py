"""CLI for the research pipeline proof of concept.

Commands:
  analyze <TICKER>   End-to-end single-ticker analysis with mocked market data
                     and credential-free Reddit RSS by default: universe check
                     → reddit mention aggregation → risk plan → schema-validated
                     frozen recommendation JSON (printed).
  paper-status       Show the paper ledger's cash, positions, and last snapshot.

No broker call or order is possible. Use `analyze --provider-mode fixture` for
a fully offline deterministic run. Output is research only — never an
instruction to trade.
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
from typing import Sequence

from jsonschema import Draft7Validator

from .analysis.sentiment import KeywordClassifier, RedditRecord, aggregate
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


def analyze(
    symbol: str,
    *,
    reddit_records: Sequence[RedditRecord] | None = None,
    reddit_net_sentiment: float | None = None,
    reddit_missing_data_reasons: tuple[str, ...] = (),
    reddit_source_label: str = "reddit(mock)",
) -> dict:
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
    use_mock_reddit = reddit_records is None
    reddit = MockRedditAdapter(now_epoch=now) if use_mock_reddit else None

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
    records = reddit.fetch_records(symbol) if reddit is not None else list(reddit_records)
    day = 86_400.0
    agg = aggregate(records, symbol, now - day, now, KeywordClassifier())
    mention_confirmed = any(
        m.counted for r in records for m in extract_mentions(r.text, universe) if m.symbol == symbol
    )
    if records and not mention_confirmed:
        rec["warnings"].append("reddit records present but no counted mention survived extraction rules")
    sentiment_value = agg.net_sentiment if use_mock_reddit else reddit_net_sentiment
    if sentiment_value is None:
        rec["missing_data_reasons"].extend(reddit_missing_data_reasons or ("Reddit sentiment unavailable",))
    else:
        rec["reddit_component"] = {
            "weight": REDDIT_WEIGHT,
            "net_sentiment": round(sentiment_value, 3),
            "total_mentions": agg.total_mentions,
            "unique_authors": agg.unique_authors,
            "note": "sentiment, not fact; untrusted source; capped at 10% of score",
        }
        rec["data_timestamps"][reddit_source_label] = ts

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
    reddit_contrib = 10 * sentiment_value if sentiment_value is not None else 0.0
    factors.append(
        {"factor": "reddit_net_sentiment", "raw_value": sentiment_value,
         "normalized": sentiment_value, "weight": REDDIT_WEIGHT if sentiment_value is not None else 0.0,
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
        f"{sentiment_value:+.2f} over {agg.total_mentions} mentions "
        f"({agg.unique_authors} unique authors), capped at 10% weight. "
        f"All numbers computed by Python, none by an LLM."
    ) if sentiment_value is not None else (
        f"Deterministic PoC analysis of {symbol}: score {rec['score']} from {len(factors)} stored factors; "
        "Reddit sentiment was unavailable and contributed zero points. All numbers computed by Python, none by an LLM."
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

    # Deliberately do not copy the parent environment wholesale — only this
    # exact allowlist crosses the subprocess boundary. ALPACA_* values (if
    # present) are passed through verbatim, never parsed or acted on by this
    # process (that's what "the main process does not read credential
    # values" means): every other application secret (Anthropic, Reddit,
    # Robinhood, MCP, database) is excluded by construction. PAPER_RUNTIME_
    # ENV_FILE is not a secret itself — it is only a path telling the runtime
    # process which dedicated, Alpaca-only dotenv file to load on its own.
    _ALLOWED_KEYS = (
        "PATH", "PYTHONPATH", "ALPACA_API_KEY", "ALPACA_API_SECRET", "ALPACA_IS_PAPER",
        "ALPACA_BASE_URL", "PAPER_BROKER_PROVIDER", "PAPER_RUNTIME_ENV_FILE",
    )
    env = {key: os.environ[key] for key in _ALLOWED_KEYS if key in os.environ}
    paper_runtime_src = REPO_ROOT / "paper_runtime" / "src"
    if paper_runtime_src.is_dir():
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(paper_runtime_src) + (os.pathsep + existing if existing else "")
    return env


def _bounded_message(message: object, limit: int = 500) -> str:
    text = str(message)
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


def _sanitized_cli_error(exc: Exception) -> dict:
    """Part 17: never return `str(exc)` for an unexpected failure — a raw
    exception can carry a filesystem path, subprocess detail, or other
    internal information the CLI must not surface. Known, already-sanitized
    domain error types (ExternalPaperError, RuntimeOperationError, which
    carries the isolated runtime's own curated code/message) are passed
    through (bounded); anything else collapses to one stable generic code
    and message, never the exception's own text.
    """
    from .paper_books.external_broker import ExternalPaperError
    from .runtime.client.errors import RuntimeClientError, RuntimeOperationError

    if isinstance(exc, ExternalPaperError):
        return {"code": exc.code, "message": _bounded_message(exc.message)}
    if isinstance(exc, RuntimeOperationError):
        return {"code": exc.code, "message": _bounded_message(exc.message)}
    if isinstance(exc, RuntimeClientError):
        return {
            "code": type(exc).__name__.upper(),
            "message": "the isolated paper runtime process is unavailable or did not respond in time",
        }
    return {"code": "EXTERNAL_RUNTIME_ERROR", "message": "an unexpected internal error occurred"}


def _external_paper_cli(db_path: Path, operation) -> dict:
    from .paper_books.config import load_paper_books_config

    client, _runtime_config = _build_runtime_client()
    try:
        client.start()
        with session(db_path) as conn:
            return operation(conn, client, load_paper_books_config())
    except Exception as exc:
        return {"error": _sanitized_cli_error(exc)}
    finally:
        client.shutdown()


def external_paper_account_check_cli(db_path: Path, *, book_id: str) -> dict:
    from .paper_books.external_broker import check_external_paper_account
    return _external_paper_cli(
        db_path, lambda conn, runtime, config: check_external_paper_account(
            conn, book_id=book_id, runtime=runtime, config=config,
        ),
    )


def external_paper_preview_cli(db_path: Path, *, book_id: str, intent_id: str, operator: str) -> dict:
    from .paper_books.external_broker import preview_external_paper_order
    return _external_paper_cli(
        db_path, lambda conn, runtime, config: preview_external_paper_order(
            conn, book_id=book_id, paper_order_intent_id=intent_id, operator=operator,
            runtime=runtime, config=config,
        ),
    )


def external_paper_submit_cli(
    db_path: Path, *, book_id: str, intent_id: str, preview_id: str, operator: str, reason: str,
) -> dict:
    from .paper_books.external_broker import submit_external_paper_order
    return _external_paper_cli(
        db_path, lambda conn, runtime, config: submit_external_paper_order(
            conn, book_id=book_id, paper_order_intent_id=intent_id, preview_id=preview_id,
            operator=operator, reason=reason, runtime=runtime, config=config,
        ),
    )


def external_paper_reconcile_cli(db_path: Path, *, book_id: str, client_order_id: str | None) -> dict:
    from .paper_books.external_broker import reconcile_external_paper_order
    return _external_paper_cli(
        db_path, lambda conn, runtime, config: reconcile_external_paper_order(
            conn, book_id=book_id, client_order_id=client_order_id, runtime=runtime, config=config,
        ),
    )


def external_paper_cancel_cli(
    db_path: Path, *, book_id: str, client_order_id: str, operator: str, reason: str,
) -> dict:
    from .paper_books.external_broker import cancel_external_paper_order
    return _external_paper_cli(
        db_path, lambda conn, runtime, config: cancel_external_paper_order(
            conn, book_id=book_id, client_order_id=client_order_id, operator=operator,
            reason=reason, runtime=runtime, config=config,
        ),
    )


def external_paper_retry_cli(
    db_path: Path, *, book_id: str, intent_id: str, operator: str, reason: str,
) -> dict:
    from .paper_books.external_broker import retry_external_paper_order
    return _external_paper_cli(
        db_path, lambda conn, runtime, config: retry_external_paper_order(
            conn, book_id=book_id, paper_order_intent_id=intent_id, operator=operator,
            reason=reason, runtime=runtime, config=config,
        ),
    )


def external_paper_refresh_retry_preview_cli(
    db_path: Path, *, book_id: str, intent_id: str, operator: str, reason: str,
) -> dict:
    """Part 17: read-only, no broker/runtime call — does not route through
    `_external_paper_cli` (which spawns the isolated runtime subprocess)."""
    from .paper_books.config import load_paper_books_config
    from .paper_books.external_broker import refresh_retry_preview
    try:
        with session(db_path) as conn:
            return refresh_retry_preview(
                conn, book_id=book_id, paper_order_intent_id=intent_id, operator=operator,
                reason=reason, config=load_paper_books_config(),
            )
    except Exception as exc:
        return {"error": _sanitized_cli_error(exc)}


def external_paper_order_show_cli(db_path: Path, *, book_id: str, client_order_id: str) -> dict:
    from .paper_books.external_broker import show_external_paper_order
    try:
        with session(db_path) as conn:
            return show_external_paper_order(conn, book_id=book_id, client_order_id=client_order_id)
    except Exception as exc:
        return {"error": _sanitized_cli_error(exc)}


def external_paper_queue_show_cli(db_path: Path, *, book_id: str) -> dict:
    """Read-only queue display — no runtime client, no lease, no mutation."""
    from .paper_books.external_broker import list_external_submission_queue_view
    try:
        with session(db_path) as conn:
            return {"book_id": book_id, "queue": list_external_submission_queue_view(conn, book_id=book_id)}
    except Exception as exc:
        return {"error": _sanitized_cli_error(exc)}


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
    paper-execution eligibility and intent construction with the offline
    deterministic adapter. The former credentialed shortcut is closed in
    Milestone 11; external paper execution requires the separate explicit
    preview and submit commands.

    The deterministic path uses
    `runtime.deterministic_adapter.DeterministicPaperAdapter`,
    auto-registered here to fill immediately at the recommendation's own
    `price_at_rec` — a deterministic, offline stand-in, NOT a real LumiBot
    broker round trip (that requires credentials/network this CLI does not
    have; see `runtime/lumibot/adapter.py` and
    docs/milestone3-lumibot-paper-integration.md). This is disclosed in the
    returned dict's `adapter` field, never presented as a real fill.

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
        return {
            "mode": exec_config.trading_mode, "adapter": "credentialed",
            "error": "CREDENTIALED_SHORTCUT_DISABLED: use external-paper-preview then external-paper-submit",
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


def build_evidence_cli(symbol: str, as_of_str: str, db_path: Path) -> dict:
    """`build-evidence` CLI command (Milestone 5). Fixture-backed only in
    this vertical slice — see `research/fixtures.py`. Never calls Claude."""
    from datetime import datetime, timezone

    from .research.configuration import load_research_config
    from .research.fixtures import build_fixture_snapshot, fixture_symbols, is_fixture_symbol
    from .storage.research_repositories import save_evidence_snapshot

    symbol = symbol.upper()
    try:
        as_of = datetime.fromisoformat(as_of_str.replace("Z", "+00:00"))
    except ValueError as exc:
        return {"error": f"invalid --as-of: {exc}"}
    if as_of.tzinfo is None:
        return {"error": "--as-of must be timezone-aware (e.g. 2026-07-01T20:00:00Z)"}
    if not is_fixture_symbol(symbol):
        return {"error": f"{symbol} is not a fixture-backed symbol in this vertical slice", "fixture_symbols": list(fixture_symbols())}

    research_config = load_research_config()
    snapshot = build_fixture_snapshot(
        symbol, as_of, config_hash=research_config.config_hash, git_sha=_git_sha(),
        clock=lambda: datetime.now(timezone.utc),
    )
    with session(db_path) as conn:
        newly_persisted = save_evidence_snapshot(conn, snapshot)
    return {
        "snapshot_id": snapshot.snapshot_id, "symbol": snapshot.symbol, "as_of": snapshot.as_of.isoformat(),
        "evidence_item_count": len(snapshot.evidence_items), "source_record_count": len(snapshot.source_records),
        "missing_data_reasons": list(snapshot.missing_data_reasons), "point_in_time_safe": snapshot.point_in_time_safe,
        "newly_persisted": newly_persisted,
    }


def run_research_cli(snapshot_id: str, provider_name: str, db_path: Path) -> dict:
    """`run-research` CLI command (Milestone 5). `--provider deterministic`
    (default) never leaves this machine; `--provider anthropic` requires an
    explicit flag plus a configured ANTHROPIC_API_KEY and research.model —
    never selected silently."""
    from datetime import datetime, timezone

    from .research.configuration import load_research_config
    from .research.deterministic_provider import DeterministicResearchProvider
    from .research.orchestration import analyze_with_research_committee
    from .research.prompt_registry import PromptRegistry
    from .storage.research_repositories import SQLiteResearchRepository, load_evidence_snapshot

    research_config = load_research_config()

    with session(db_path) as conn:
        snapshot = load_evidence_snapshot(conn, snapshot_id)
        if snapshot is None:
            return {"error": f"no persisted evidence snapshot {snapshot_id!r} — run build-evidence first"}

        if provider_name == "deterministic":
            provider = DeterministicResearchProvider()
            model_name = "deterministic-v1"
        elif provider_name == "anthropic":
            from .research.anthropic_provider import AnthropicProviderConfig, AnthropicResearchProvider
            from .research.usage import load_pricing_config

            cfg = load_config(require_anthropic=True)
            research_config.require_ready()
            model_name = research_config.model
            provider = AnthropicResearchProvider(AnthropicProviderConfig(
                api_key=cfg.anthropic_api_key, request_timeout_seconds=research_config.request_timeout_seconds,
                pricing_entries=load_pricing_config(),
            ))
        elif provider_name == "claude_code":
            from .research.claude_code_provider import ClaudeCodeResearchProvider
            from .research.usage import load_pricing_config

            research_config.require_ready()
            model_name = research_config.model
            provider = ClaudeCodeResearchProvider(
                research_config.build_claude_code_provider_config(pricing_entries=load_pricing_config())
            )
            provider.preflight()
        else:
            return {"error": f"unknown provider {provider_name!r}"}

        repo = SQLiteResearchRepository(conn)
        result = analyze_with_research_committee(
            snapshot, provider=provider, provider_name=provider_name, model_name=model_name,
            prompt_registry=PromptRegistry(), research_repository=repo, configuration=research_config,
            clock=lambda: datetime.now(timezone.utc), run_mode=provider_name,
        )

    return {
        "provider": provider_name, "model": model_name, "snapshot_id": snapshot_id,
        "research_run_id": result.research_run_id, "status": result.status,
        "reused_existing_run": result.reused_existing_run,
        "role_reports": [r.role for r in result.role_reports],
        "decision_rating": result.decision.rating if result.decision else None,
        "incomplete_reasons": list(result.incomplete_reasons),
    }


def claude_code_provider_preflight_cli(research_config_path: Path | None = None) -> dict:
    """Sanitized version/auth readiness only; never makes an inference call."""
    from .research.claude_code_provider import ClaudeCodeResearchProvider
    from .research.configuration import load_research_config
    from .research.usage import load_pricing_config

    config = load_research_config(research_config_path) if research_config_path is not None else load_research_config()
    config.require_ready()
    if config.provider != "claude_code":
        return {
            "ready": False,
            "provider": config.provider,
            "failure_code": "CLAUDE_CODE_NOT_CONFIGURED",
        }
    try:
        provider = ClaudeCodeResearchProvider(
            config.build_claude_code_provider_config(pricing_entries=load_pricing_config())
        )
        result = provider.preflight(force=True)
    except Exception as exc:
        return {
            "ready": False,
            "provider": "claude_code",
            "configured_model": config.model,
            "binary_version": None,
            "authenticated": False,
            "authentication_method": None,
            "failure_code": getattr(exc, "code", "CLAUDE_CODE_PREFLIGHT_FAILED"),
        }
    return {
        "ready": result.ready,
        "provider": "claude_code",
        "configured_model": config.model,
        "binary_version": result.binary_version,
        "minimum_version_satisfied": True,
        "oauth_token_present": True,
        "authenticated": result.authenticated,
        "authentication_method": result.authentication_method,
        "usage_metadata_required": True,
        "failure_code": result.failure_code,
        "checked_at": result.checked_at.isoformat(),
        "paper_submission_enabled": False,
        "external_execution_reachable": False,
    }


def replay_research_cli(research_run_id: str, db_path: Path) -> dict:
    """`replay-research` CLI command (Milestone 5). Never calls a provider —
    reconstructs the persisted decision from the persisted evidence snapshot
    and re-runs the deterministic validators/overlay only."""
    from .research.configuration import load_research_config
    from .research.prompt_registry import PromptRegistry
    from .research.replay import replay_research_run
    from .storage.research_repositories import SQLiteResearchRepository, load_evidence_snapshot

    research_config = load_research_config()
    with session(db_path) as conn:
        run_row = conn.execute(
            "SELECT * FROM research_committee_runs WHERE research_run_id = ?", (research_run_id,)
        ).fetchone()
        if run_row is None:
            return {"error": f"no persisted research run {research_run_id!r}"}
        snapshot = load_evidence_snapshot(conn, run_row["snapshot_id"])
        if snapshot is None:
            return {"error": f"snapshot {run_row['snapshot_id']!r} is no longer persisted"}

        repo = SQLiteResearchRepository(conn)
        result = replay_research_run(
            research_run_id, research_repository=repo, snapshot=snapshot, provider_name=run_row["provider"],
            model_name=run_row["model_name"], prompt_registry=PromptRegistry(), configuration=research_config,
            run_mode=run_row["run_mode"],
        )

    return {
        "research_run_id": research_run_id, "matches": result.matches, "mismatches": list(result.mismatches),
        "decision_rating": result.reconstructed_decision.rating if result.reconstructed_decision else None,
        "overlay_action": result.reconstructed_overlay.action if result.reconstructed_overlay else None,
        "persisted_failure_count": len(result.persisted_failures),
        "failure_comparison": result.failure_comparison,
    }


def compare_research_arms_cli(experiment_id: str, db_path: Path) -> dict:
    """`compare-research-arms` CLI command (Milestone 5)."""
    from .storage.research_repositories import list_experiment_assignments

    with session(db_path) as conn:
        assignments = list_experiment_assignments(conn, experiment_id)
    return {
        "experiment_id": experiment_id,
        "assignments": [
            {
                "symbol": a.symbol, "arm": a.arm, "as_of": a.as_of.isoformat(),
                "baseline_recommendation_id": a.baseline_recommendation_id,
                "enhanced_recommendation_id": a.enhanced_recommendation_id,
            }
            for a in assignments
        ],
    }


def research_performance_cli(db_path: Path) -> dict:
    """`research-performance` CLI command (Milestone 5): research-run-level
    outcome rates. Trading-performance comparison lives in
    `evaluation/research_comparison.py` (evaluate-research-arms)."""
    from .storage.research_repositories import list_research_committee_runs

    with session(db_path) as conn:
        runs = list_research_committee_runs(conn)
    total = len(runs)
    completed = sum(1 for r in runs if r["status"] == "COMPLETED")
    incomplete = sum(1 for r in runs if r["status"] == "ANALYSIS_INCOMPLETE")
    failed = sum(1 for r in runs if r["status"] == "FAILED")
    return {
        "total_runs": total, "completed": completed, "analysis_incomplete": incomplete, "failed": failed,
        "completion_rate": (completed / total) if total else None,
        "incomplete_rate": (incomplete / total) if total else None,
    }


def research_usage_cli(db_path: Path) -> dict:
    """`research-usage` CLI command (Milestone 5): token/latency/cost
    aggregation grouped by role, over every persisted attempt."""
    from decimal import Decimal

    from .storage.research_repositories import list_attempt_usage_rows

    with session(db_path) as conn:
        rows = list_attempt_usage_rows(conn)

    by_role: dict[str, dict] = {}
    total_cost = Decimal("0")
    cost_available = False
    estimate_bases: set[str] = set()
    for row in rows:
        agg = by_role.setdefault(row["role"], {
            "attempts": 0, "successes": 0, "total_input_tokens": 0, "total_output_tokens": 0,
            "_latency_sum": 0, "_latency_count": 0,
        })
        agg["attempts"] += 1
        agg["successes"] += int(bool(row["success"]))
        agg["total_input_tokens"] += row["input_tokens"] or 0
        agg["total_output_tokens"] += row["output_tokens"] or 0
        if row["latency_ms"] is not None:
            agg["_latency_sum"] += row["latency_ms"]
            agg["_latency_count"] += 1
        if row["cost_status"] == "CALCULATED" and row["estimated_cost"] is not None:
            total_cost += Decimal(row["estimated_cost"])
            cost_available = True
        estimate_bases.add(row["cost_estimate_basis"])

    summary = {}
    for role, agg in by_role.items():
        summary[role] = {
            "attempts": agg["attempts"], "successes": agg["successes"],
            "total_input_tokens": agg["total_input_tokens"], "total_output_tokens": agg["total_output_tokens"],
            "average_latency_ms": (agg["_latency_sum"] / agg["_latency_count"]) if agg["_latency_count"] else None,
        }
    return {
        "by_role": summary,
        "total_estimated_cost": str(total_cost) if cost_available else None,
        "cost_status": "CALCULATED" if cost_available else "PRICING_NOT_CONFIGURED_OR_NO_USAGE",
        "cost_estimate_bases": sorted(estimate_bases),
        "subscription_cost_is_api_equivalent_estimate": (
            "SUBSCRIPTION_API_EQUIVALENT_ESTIMATE" in estimate_bases
        ),
    }


def research_failures_cli(
    research_run_id: str, db_path: Path, *,
    role: str | None = None, attempt_number: int | None = None, stage: str | None = None, code: str | None = None,
) -> dict:
    """`research-failures` CLI command (Milestone 6.1 Step 15): sanitized, structured
    failure diagnostics for one research run. Never prints a raw prompt, a complete raw
    provider response, chain-of-thought, or a secret — every field returned here already
    passed `research/failure_taxonomy.py::ResearchValidationFailure`'s own bounded,
    allowlisted-metadata validation before it was ever persisted."""
    from .storage.research_repositories import list_run_failures, summarize_run_failures

    with session(db_path) as conn:
        run_row = conn.execute(
            "SELECT research_run_id FROM research_committee_runs WHERE research_run_id = ?", (research_run_id,)
        ).fetchone()
        if run_row is None:
            return {"error": f"no persisted research run {research_run_id!r}"}

        failures = list_run_failures(
            conn, research_run_id, role=role, attempt_number=attempt_number, stage=stage, code=code,
        )
        summary = summarize_run_failures(conn, research_run_id)

    return {
        "research_run_id": research_run_id,
        "total_failures": len(failures),
        "counts_by_stage": summary["counts_by_stage"],
        "counts_by_code": summary["counts_by_code"],
        "failures": [
            {
                "attempt_id": f.attempt_id, "role": f.role, "attempt_number": f.attempt_number,
                "stage": f.stage, "code": f.code, "field_path": f.field_path, "claim_id": f.claim_id,
                "evidence_ids": list(f.evidence_ids), "sanitized_message": f.message, "retryable": f.retryable,
                "stop_reason": f.metadata.get("stop_reason"), "input_tokens": f.metadata.get("input_tokens"),
                "output_tokens": f.metadata.get("output_tokens"), "prompt_version": f.prompt_version,
                "schema_version": f.schema_version, "occurred_at": f.occurred_at.isoformat(),
            }
            for f in failures
        ],
    }


def research_failure_metrics_cli(db_path: Path) -> dict:
    """`research-failure-metrics` CLI command (Milestone 6.1 Step 17): deterministic
    failure-rate/token/latency metrics over every persisted attempt and structured
    failure. Every metric reports an explicit status
    (`OK`/`INSUFFICIENT_DATA`/`UNDEFINED`) rather than a misleading zero when there is no
    relevant data."""
    from .research.failure_metrics import compute_research_failure_metrics
    from .storage.research_repositories import list_all_attempt_failures, list_attempt_rows_for_metrics

    with session(db_path) as conn:
        attempt_rows = list_attempt_rows_for_metrics(conn)
        failures = list_all_attempt_failures(conn)

    return compute_research_failure_metrics(attempt_rows=attempt_rows, failures=failures)


def _make_persist_hook(conn) -> "Callable[[dict], None] | None":
    """Bridges `HttpJsonClient.on_response`'s plain-dict callback to
    `evidence_providers/persistence.py::save_provider_request` (Milestone 6
    Step 5). `None` when no database connection is available yet (e.g. the
    registry is being built outside a `session()` block)."""
    if conn is None:
        return None
    from datetime import datetime, timezone

    from .evidence_providers.persistence import LICENSE_ACCOUNT_LINKED, LICENSE_PUBLIC_DOMAIN, ProviderRequestRecord, save_provider_request

    def _hook(record: dict) -> None:
        licensing = LICENSE_PUBLIC_DOMAIN if record["provider"] == "sec-edgar" else LICENSE_ACCOUNT_LINKED
        now = datetime.now(timezone.utc)
        save_provider_request(conn, ProviderRequestRecord(
            provider=record["provider"], operation=record["operation"], symbol=record["symbol"] or "__NONE__",
            requested_as_of=now, retrieved_at=now, provider_response_timestamp=None,
            http_status=record["http_status"], content_hash=None, normalized_record_hash=None,
            cache_status=record["cache_status"], rate_limited=record["rate_limited"], retry_count=record["retry_count"],
            latency_ms=record["latency_ms"], success=record["success"], error_code=record["error_code"],
            retryable=record["retryable"], licensing_classification=licensing, raw_payload=None,
        ))

    return _hook


def _build_evidence_provider_registry(provider_mode: str, *, cfg, conn=None) -> tuple:
    """Returns `(registry, health_provider_names)`. `provider_mode` is
    "fixture" (offline, no network/credentials — always available) or "real"
    (docs/milestone-6.md Step 21: real mode requires explicit selection).
    `conn`, when supplied, wires real HTTP calls to persist request/response
    metadata (Step 5) for `provider-health`/`evidence-provider-usage`.
    """
    from .evidence_providers.alpaca_news_provider import PROVIDER_NAME as NEWS_PROVIDER_NAME
    from .evidence_providers.alpaca_news_provider import AlpacaNewsClient
    from .evidence_providers.cache import ProviderCache
    from .evidence_providers.config import load_evidence_provider_config
    from .evidence_providers.corporate_status_adapters import SecCorporateStatusProvider
    from .evidence_providers.evidence_adapters import (
        RealFilingEvidenceProvider,
        RealFundamentalsEvidenceProvider,
        RealMarketEvidenceProvider,
        RealNewsEvidenceProvider,
        RealSentimentEvidenceProvider,
    )
    from .evidence_providers.fixture_clients import FixtureMarketDataClient, FixtureSecClient
    from .evidence_providers.http_client import HttpJsonClient
    from .evidence_providers.market_data_provider import AlpacaMarketDataClient
    from .evidence_providers.news_provider import UnconfiguredNewsProvider
    from .evidence_providers.rate_limits import MinIntervalRateLimiter
    from .evidence_providers.sec_provider import SecEdgarClient
    from .evidence_providers.sentiment_provider import RedditSentimentSource
    from .research.scheduled_cycle import PROVIDER_MODE_FIXTURE, PROVIDER_MODE_REAL, EvidenceProviderRegistry

    if provider_mode == PROVIDER_MODE_FIXTURE:
        sec = FixtureSecClient()
        market = FixtureMarketDataClient()
        registry = EvidenceProviderRegistry(
            fundamentals=RealFundamentalsEvidenceProvider(sec), market=RealMarketEvidenceProvider(market),
            filings=RealFilingEvidenceProvider(sec), news=None, sentiment=None, portfolio_context=None,
            market_data_client=market, sec_client=sec,
            # filing_document_client=None -> metadata-only corporate status (Step 6),
            # deterministic and offline, matching every other fixture-mode provider here.
            corporate_status=SecCorporateStatusProvider(sec),
        )
        return registry, ()

    if provider_mode == "reddit_free":
        provider_config = load_evidence_provider_config()
        from .evidence_providers.reddit_free import PROVIDER_NAME as REDDIT_FREE_PROVIDER_NAME
        from .evidence_providers.reddit_free import RedditFreeProvider

        reddit_free = RedditFreeProvider(
            provider_config.reddit_free,
            conn=conn,
            data_dir=cfg.research_data_dir,
        )
        registry = EvidenceProviderRegistry(
            fundamentals=None,
            market=None,
            filings=None,
            news=None,
            sentiment=RealSentimentEvidenceProvider(reddit_free),
            portfolio_context=None,
            market_data_client=None,
            sec_client=None,
            corporate_status=None,
        )
        return registry, (REDDIT_FREE_PROVIDER_NAME,)

    if provider_mode != PROVIDER_MODE_REAL:
        raise ValueError(f"unknown provider-mode {provider_mode!r} — must be 'fixture', 'real', or 'reddit_free'")

    provider_config = load_evidence_provider_config()
    used_providers: list[str] = []
    persist_hook = _make_persist_hook(conn)

    sec = None
    if provider_config.sec.enabled:
        sec_cache = ProviderCache(clock=time.monotonic, on_response=persist_hook)
        sec_http = HttpJsonClient(
            base_headers={"User-Agent": provider_config.sec.user_agent_contact},
            rate_limiter=MinIntervalRateLimiter(provider_config.sec.min_request_interval_seconds),
            max_attempts=provider_config.sec.max_attempts, timeout_seconds=provider_config.sec.request_timeout_seconds,
            provider="sec-edgar", on_response=persist_hook,
        )
        sec = SecEdgarClient(http_client=sec_http, cache=sec_cache, user_agent=provider_config.sec.user_agent_contact)
        used_providers.append("sec-edgar")

    filing_document_client = None
    if sec is not None:
        from .evidence_providers.filing_documents import FilingDocumentCache, FilingDocumentClient

        filing_document_client = FilingDocumentClient(
            user_agent=provider_config.sec.user_agent_contact,
            rate_limiter=MinIntervalRateLimiter(provider_config.sec.min_request_interval_seconds),
            cache=FilingDocumentCache(),
        )

    market = None
    if provider_config.market_data.enabled and cfg.alpaca_market_data_api_key and cfg.alpaca_market_data_api_secret:
        market_cache = ProviderCache(clock=time.monotonic, on_response=persist_hook)
        market_http = HttpJsonClient(
            base_headers={"APCA-API-KEY-ID": cfg.alpaca_market_data_api_key, "APCA-API-SECRET-KEY": cfg.alpaca_market_data_api_secret},
            rate_limiter=MinIntervalRateLimiter(provider_config.market_data.min_request_interval_seconds),
            max_attempts=provider_config.market_data.max_attempts, timeout_seconds=provider_config.market_data.request_timeout_seconds,
            provider="alpaca-data", on_response=persist_hook,
        )
        market = AlpacaMarketDataClient(
            api_key=cfg.alpaca_market_data_api_key, api_secret=cfg.alpaca_market_data_api_secret,
            http_client=market_http, cache=market_cache,
        )
        used_providers.append("alpaca-data")
    # market_data.enabled=true with absent credentials fails closed to
    # market=None (excluded from the registry) rather than raising — the
    # cycle proceeds with whatever other evidence is available and records
    # the gap in missing_data_reasons, consistent with "absent credentials
    # fail closed" (docs/milestone-6.md Step 20).

    news = None
    if provider_config.news.enabled and provider_config.news.provider == "alpaca_news":
        if cfg.alpaca_market_data_api_key and cfg.alpaca_market_data_api_secret:
            news_cache = ProviderCache(clock=time.monotonic, on_response=persist_hook)
            news_http = HttpJsonClient(
                base_headers={"APCA-API-KEY-ID": cfg.alpaca_market_data_api_key, "APCA-API-SECRET-KEY": cfg.alpaca_market_data_api_secret},
                rate_limiter=MinIntervalRateLimiter(0.35),
                max_attempts=provider_config.news.max_attempts, timeout_seconds=provider_config.news.request_timeout_seconds,
                provider=NEWS_PROVIDER_NAME, on_response=persist_hook,
            )
            news = RealNewsEvidenceProvider(AlpacaNewsClient(
                api_key=cfg.alpaca_market_data_api_key, api_secret=cfg.alpaca_market_data_api_secret,
                http_client=news_http, cache=news_cache,
            ))
            used_providers.append(NEWS_PROVIDER_NAME)
        # news.enabled=true with provider=alpaca_news but absent credentials fails
        # closed to news=None (excluded from the registry), matching market_data's
        # existing "absent credentials fail closed" posture (docs/milestone-6.md Step 20).
    elif provider_config.news.enabled:
        news = RealNewsEvidenceProvider(UnconfiguredNewsProvider())

    sentiment = None
    if provider_config.sentiment.enabled:
        from .evidence_providers.reddit_fetch import build_reddit_sentiment_source

        sentiment = RealSentimentEvidenceProvider(build_reddit_sentiment_source(cfg))

    registry = EvidenceProviderRegistry(
        fundamentals=RealFundamentalsEvidenceProvider(sec) if sec else None,
        market=RealMarketEvidenceProvider(market) if market else None,
        filings=RealFilingEvidenceProvider(sec) if sec else None,
        news=news, sentiment=sentiment, portfolio_context=None,
        market_data_client=market, sec_client=sec,
        corporate_status=(
            SecCorporateStatusProvider(sec, filing_document_client=filing_document_client) if sec else None
        ),
    )
    return registry, tuple(used_providers)


def provider_health_cli(db_path: Path) -> dict:
    """`provider-health` CLI command (Milestone 6; concentration fields added Milestone
    6.1 Step 18)."""
    from .evidence_providers.health import compute_all_provider_health, compute_provider_concentration
    from .evidence_providers.persistence import list_provider_requests

    with session(db_path) as conn:
        rows = list_provider_requests(conn)
    summaries = compute_all_provider_health(rows)
    return {
        "providers": [
            {
                "provider": s.provider, "status": s.status, "total_requests": s.total_requests,
                "success_rate": s.success_rate, "cache_hit_rate": s.cache_hit_rate,
                "average_latency_ms": s.average_latency_ms, "p95_latency_ms": s.p95_latency_ms,
            }
            for s in summaries
        ],
        "concentration": compute_provider_concentration(),
    }


def fetch_evidence_cli(symbol: str, as_of_str: str | None, db_path: Path, provider_mode: str) -> dict:
    """`fetch-evidence` CLI command (Milestone 6). Builds and persists a real
    (or fixture) point-in-time evidence snapshot — never calls Claude."""
    from .research.configuration import load_research_config
    from .research.scheduled_cycle import build_real_evidence_snapshot
    from .storage.research_repositories import save_evidence_snapshot

    symbol = symbol.upper()
    try:
        as_of = (
            datetime.fromisoformat(as_of_str.replace("Z", "+00:00"))
            if as_of_str
            else datetime.now(timezone.utc)
        )
    except ValueError as exc:
        return {"error": f"invalid --as-of: {exc}"}
    if as_of.tzinfo is None:
        return {"error": "--as-of must be timezone-aware (e.g. 2026-07-01T20:00:00Z)"}

    cfg = load_config()
    research_config = load_research_config()

    with session(db_path) as conn:
        registry, used_providers = _build_evidence_provider_registry(provider_mode, cfg=cfg, conn=conn)
        snapshot, corporate_status = build_real_evidence_snapshot(
            symbol, as_of, providers=registry, deterministic_factors={}, config_hash=research_config.config_hash,
            git_sha=_git_sha(), clock=lambda: datetime.now(timezone.utc),
            max_evidence_items=research_config.max_evidence_items,
            max_items_per_source_category=research_config.max_items_per_source_category,
        )
        newly_persisted = save_evidence_snapshot(conn, snapshot)

    return {
        "provider_mode": provider_mode, "providers_used": list(used_providers), "snapshot_id": snapshot.snapshot_id,
        "symbol": snapshot.symbol, "as_of": snapshot.as_of.isoformat(), "evidence_item_count": len(snapshot.evidence_items),
        "source_record_count": len(snapshot.source_records), "missing_data_reasons": list(snapshot.missing_data_reasons),
        "point_in_time_safe": snapshot.point_in_time_safe, "newly_persisted": newly_persisted,
        "corporate_reporting_status": corporate_status.reporting_status if corporate_status is not None else None,
    }


def run_research_cycle_cli(as_of_str: str, db_path: Path, provider_mode: str, symbols: list[str] | None) -> dict:
    """`run-research-cycle` CLI command (Milestone 6). `--provider-mode
    fixture` (offline) is available by default; `--provider-mode real`
    requires explicit selection and only queries providers explicitly
    enabled in `config/evidence_providers.yaml`."""
    from decimal import Decimal

    from .analysis.scorer import load_scoring_config
    from .analysis.screener import load_screening_config
    from .research.configuration import load_research_config
    from .research.deterministic_provider import DeterministicResearchProvider
    from .research.prompt_registry import PromptRegistry
    from .research.scheduled_cycle import run_scheduled_research_cycle
    from .research.scheduled_research_config import load_scheduled_research_config
    from .storage.research_cycle_repositories import SQLiteResearchCycleRepository
    from .storage.research_repositories import SQLiteResearchRepository

    try:
        as_of = datetime.fromisoformat(as_of_str.replace("Z", "+00:00"))
    except ValueError as exc:
        return {"error": f"invalid --as-of: {exc}"}
    if as_of.tzinfo is None:
        return {"error": "--as-of must be timezone-aware (e.g. 2026-07-01T20:00:00Z)"}

    sr_config = load_scheduled_research_config()
    cycle_config = sr_config.to_cycle_configuration(provider_mode=provider_mode)
    research_config = load_research_config()

    candidate_symbols = tuple(s.upper() for s in symbols) if symbols else tuple(
        s.upper() for s in (["AAPL", "MSFT", "SHEL"] if provider_mode == "fixture" else [])
    )
    if not candidate_symbols:
        return {"error": "no candidate symbols supplied — pass --symbol at least once for provider-mode=real"}

    with session(db_path) as conn:
        from .universe.tickers import default_universe
        from .models.trading_models import PortfolioState

        registry, used_providers = _build_evidence_provider_registry(provider_mode, cfg=load_config(), conn=conn)
        result = run_scheduled_research_cycle(
            as_of=as_of, symbols=candidate_symbols, configuration=cycle_config, conn=conn,
            cycle_repository=SQLiteResearchCycleRepository(conn), universe=default_universe(),
            screening_config=load_screening_config(), scoring_config=load_scoring_config(),
            evidence_providers=registry, research_provider=DeterministicResearchProvider(),
            research_provider_name="deterministic", research_model_name="deterministic-v1",
            research_configuration=research_config, research_repository=SQLiteResearchRepository(conn),
            prompt_registry=PromptRegistry(), portfolio=PortfolioState(account_equity=Decimal("100000"), settled_cash=Decimal("100000"), as_of=as_of),
            paper_submitter=None, clock=lambda: datetime.now(timezone.utc), git_sha=_git_sha(),
        )

    return {
        "cycle_id": result.cycle_id, "status": result.status, "provider_mode": provider_mode,
        "providers_used": list(used_providers), "reused_existing_cycle": result.reused_existing_cycle,
        "symbol_results": [
            {
                "symbol": r.symbol, "status": r.status, "evidence_outcome": r.evidence_outcome,
                "baseline_recommendation_id": r.baseline_recommendation_id,
                "enhanced_recommendation_id": r.enhanced_recommendation_id, "baseline_side": r.baseline_side,
                "enhanced_side": r.enhanced_side, "failure_reason": r.failure_reason,
            }
            for r in result.symbol_results
        ],
    }


def resume_research_cycle_cli(cycle_id: str, db_path: Path) -> dict:
    """`resume-research-cycle` CLI command (Milestone 6). Re-invokes the same
    cycle_id's already-attempted symbols — already-COMPLETED symbols are a
    pure read (idempotent), unresolved ones are retried."""
    from decimal import Decimal

    from .analysis.scorer import load_scoring_config
    from .analysis.screener import load_screening_config
    from .models.trading_models import PortfolioState
    from .research.configuration import load_research_config
    from .research.deterministic_provider import DeterministicResearchProvider
    from .research.prompt_registry import PromptRegistry
    from .research.scheduled_cycle import ScheduledResearchConfiguration, run_scheduled_research_cycle
    from .storage.research_cycle_repositories import SQLiteResearchCycleRepository
    from .storage.research_repositories import SQLiteResearchRepository
    from .universe.tickers import default_universe

    with session(db_path) as conn:
        repo = SQLiteResearchCycleRepository(conn)
        cycle_row = repo.get_cycle(cycle_id)
        if cycle_row is None:
            return {"error": f"no persisted cycle {cycle_id!r}"}
        symbol_rows = repo.list_symbol_results(cycle_id)
        symbols = tuple(r["symbol"] for r in symbol_rows)
        as_of = datetime.fromisoformat(cycle_row["as_of"])
        provider_mode = cycle_row["provider_mode"]

        cycle_config = ScheduledResearchConfiguration(
            universe_id=cycle_row["universe_id"], max_candidates_per_cycle=max(len(symbols), 1),
            experiment_policy=cycle_row["experiment_policy"], submit_paper_orders=False,
            require_complete_evidence=True, require_point_in_time_safe=True, continue_on_symbol_failure=True,
            provider_mode=provider_mode, config_hash=cycle_row["configuration_hash"],
        )
        registry, _used = _build_evidence_provider_registry(provider_mode, cfg=load_config(), conn=conn)
        research_config = load_research_config()

        result = run_scheduled_research_cycle(
            as_of=as_of, symbols=symbols, configuration=cycle_config, conn=conn, cycle_repository=repo,
            universe=default_universe(), screening_config=load_screening_config(), scoring_config=load_scoring_config(),
            evidence_providers=registry, research_provider=DeterministicResearchProvider(),
            research_provider_name="deterministic", research_model_name="deterministic-v1",
            research_configuration=research_config, research_repository=SQLiteResearchRepository(conn),
            prompt_registry=PromptRegistry(), portfolio=PortfolioState(account_equity=Decimal("100000"), settled_cash=Decimal("100000"), as_of=as_of),
            paper_submitter=None, clock=lambda: datetime.now(timezone.utc), git_sha=_git_sha(),
        )

    return {
        "cycle_id": result.cycle_id, "status": result.status, "reused_existing_cycle": result.reused_existing_cycle,
        "symbol_results": [{"symbol": r.symbol, "status": r.status} for r in result.symbol_results],
    }


def evaluate_research_cycle_cli(cycle_id: str, db_path: Path) -> dict:
    """`evaluate-research-cycle` CLI command (Milestone 6). Computes forward
    evaluations for every baseline+enhanced recommendation the cycle
    produced — reuses `evaluation/evaluation_service.py` unchanged."""
    from decimal import Decimal

    from .evaluation.evaluation_service import evaluate_recommendation_all_horizons
    from .evaluation.price_provider import DeterministicPriceProvider
    from .runtime.paper_runtime_config import load_paper_runtime_config
    from .storage import evaluation_repositories as eval_repo
    from .storage.research_cycle_repositories import SQLiteResearchCycleRepository
    from .storage.trading_repositories import load_recommendation

    runtime_config = load_paper_runtime_config()
    now = datetime.now(timezone.utc)
    price_provider = DeterministicPriceProvider()

    with session(db_path) as conn:
        repo = SQLiteResearchCycleRepository(conn)
        symbol_rows = repo.list_symbol_results(cycle_id)
        if not symbol_rows:
            return {"error": f"no persisted symbol results for cycle {cycle_id!r}"}

        results = {}
        for row in symbol_rows:
            for label, rec_id in (("baseline", row["baseline_recommendation_id"]), ("enhanced", row["enhanced_recommendation_id"])):
                if not rec_id:
                    continue
                recommendation = load_recommendation(conn, rec_id)
                if recommendation is None:
                    continue
                evaluations = evaluate_recommendation_all_horizons(
                    recommendation_id=rec_id, symbol=recommendation["symbol"],
                    recommendation_price=Decimal(str(recommendation["price_at_rec"])) if recommendation.get("price_at_rec") is not None else None,
                    execution_price=None, filled_quantity=0, requested_quantity=0, execution_completed_at=None,
                    price_provider=price_provider, now=now, horizons=runtime_config.evaluation_horizons_trading_days,
                    benchmark_symbol=runtime_config.evaluation_benchmark, model_version=recommendation.get("model_version"),
                    prompt_version=recommendation.get("prompt_version"), config_hash=recommendation.get("config_hash"),
                )
                for e in evaluations:
                    eval_repo.save_evaluation(conn, e)
                results[f"{row['symbol']}:{label}"] = [
                    {"horizon_trading_days": e.horizon_trading_days, "status": e.status} for e in evaluations
                ]

    return {"cycle_id": cycle_id, "benchmark": runtime_config.evaluation_benchmark, "evaluations": results}


def compare_research_cycles_cli(db_path: Path) -> dict:
    """`compare-research-cycles` CLI command (Milestone 6)."""
    with session(db_path) as conn:
        rows = conn.execute("SELECT * FROM research_cycles ORDER BY started_at").fetchall()
        cycles = []
        for row in rows:
            symbol_rows = conn.execute(
                "SELECT status FROM research_cycle_symbol_results WHERE cycle_id = ?", (row["cycle_id"],)
            ).fetchall()
            completed = sum(1 for r in symbol_rows if r["status"] == "COMPLETED")
            failed = sum(1 for r in symbol_rows if r["status"] == "FAILED")
            cycles.append({
                "cycle_id": row["cycle_id"], "universe_id": row["universe_id"], "as_of": row["as_of"],
                "status": row["status"], "experiment_policy": row["experiment_policy"], "provider_mode": row["provider_mode"],
                "symbol_count": len(symbol_rows), "completed": completed, "failed": failed,
            })
    return {"cycles": cycles}


def research_promotion_status_cli(experiment_id: str, db_path: Path) -> dict:
    """`research-promotion-status` CLI command (Milestone 6). Never produces
    a live-trading status — `research/promotion.py`'s status enum has none."""
    from .evaluation.research_comparison import compare_arms
    from .research.promotion import PromotionGateInputs, PromotionMetricInput, evaluate_promotion
    from .research.scheduled_research_config import load_scheduled_research_config
    from .storage import evaluation_repositories as eval_repo
    from .storage.research_repositories import list_attempt_usage_rows, list_experiment_assignments

    sr_config = load_scheduled_research_config()
    if not sr_config.promotion_enabled:
        return {"status": "PROMOTION_DISABLED", "reason": "promotion.enabled=false in config/scheduled_research.yaml"}

    with session(db_path) as conn:
        assignments = list_experiment_assignments(conn, experiment_id)
        if not assignments:
            return {"error": f"no persisted experiment assignments for {experiment_id!r}"}
        all_evaluations = eval_repo.list_all_evaluations(conn)
        by_rec = {}
        for e in all_evaluations:
            by_rec.setdefault(e.recommendation_id, []).append(e)
        comparison = compare_arms(assignments, by_rec)
        usage_rows = list_attempt_usage_rows(conn)

    provider_failure_rate = 1 - (sum(1 for r in usage_rows if r["success"]) / len(usage_rows)) if usage_rows else 0.0
    retry_rate = (sum(1 for r in usage_rows if r["retry_count"] > 0) / len(usage_rows)) if usage_rows else 0.0

    decision = evaluate_promotion(
        PromotionGateInputs(
            completed_evaluations=min(comparison.baseline.recommendation_count, comparison.enhanced.recommendation_count),
            market_regimes_observed=1,  # single-run session — see docs/milestone6 known limitations
            excess_return_enhanced=PromotionMetricInput(status=comparison.enhanced.benchmark_relative_cumulative_return.status, value=comparison.enhanced.benchmark_relative_cumulative_return.value),
            excess_return_baseline=PromotionMetricInput(status=comparison.baseline.benchmark_relative_cumulative_return.status, value=comparison.baseline.benchmark_relative_cumulative_return.value),
            max_drawdown_enhanced=PromotionMetricInput(status=comparison.enhanced.max_drawdown.status, value=comparison.enhanced.max_drawdown.value),
            max_drawdown_baseline=PromotionMetricInput(status=comparison.baseline.max_drawdown.status, value=comparison.baseline.max_drawdown.value),
            incomplete_analysis_rate=0.0, unsupported_claim_rate=0.0, provider_failure_rate=provider_failure_rate,
            retry_rate=retry_rate, reproducibility_rate=None,
        ),
        sr_config.promotion,
    )
    return {"experiment_id": experiment_id, "status": decision.status, "policy_version": decision.policy_version, "reasons": list(decision.reasons)}


def evidence_provider_usage_cli(db_path: Path) -> dict:
    """`evidence-provider-usage` CLI command (Milestone 6)."""
    from .evidence_providers.persistence import list_provider_requests

    with session(db_path) as conn:
        rows = list_provider_requests(conn)
    by_provider: dict[str, dict] = {}
    for row in rows:
        agg = by_provider.setdefault(row["provider"], {"requests": 0, "successes": 0, "cache_hits": 0})
        agg["requests"] += 1
        agg["successes"] += int(bool(row["success"]))
        agg["cache_hits"] += int(row["cache_status"] == "HIT")
    return {"by_provider": by_provider}


def _shadow_scheduler_run_view(row: dict) -> dict:
    """Sanitized, documented-field view of one `shadow_scheduler_runs` row —
    everything already persisted there is derived data (no raw provider
    payload, no raw prompt, no raw Claude response, no credentials), so this
    is a pure column passthrough with no additional redaction needed."""
    return dict(row)


def run_due_shadow_cycle_cli(
    db_path: Path,
    *,
    provider_mode: str = "fixture",
    symbols: list[str] | None = None,
    research_config_path: Path | None = None,
    scheduled_research_config_path: Path | None = None,
    shadow_config_path: Path | None = None,
) -> dict:
    """`run-due-shadow-cycle` CLI command (docs/milestone-7.md Step 18/25;
    docs/milestone-7.1.md Step 20). Thin wiring only — delegates entirely to
    `shadow/scheduler.py::run_due_shadow_cycle`. Every successful-no-op
    status (disabled, not-due, holiday, already-completed, lease-held,
    paused, killed) is NOT an error; only an actual internal exception is.

    `--provider-mode fixture` (default) drives the offline/deterministic
    path exactly as before this task — no network, no credentials, no real
    Claude call. `--provider-mode real` builds the real SEC/corporate-status
    provider and the configured market/news/sentiment providers (only those
    explicitly enabled in `config/evidence_providers.yaml`), and, when
    `research.yaml`'s `provider: anthropic`, the real Anthropic provider —
    but ONLY after every preflight below passes. `real` is never selected
    from credential presence alone; it requires this explicit flag.
    """
    from decimal import Decimal
    from datetime import timezone as _tz

    from .analysis.scorer import load_scoring_config
    from .analysis.screener import load_screening_config
    from .models.trading_models import PortfolioState
    from .research.configuration import load_research_config
    from .research.deterministic_provider import DeterministicResearchProvider
    from .research.prompt_registry import PromptRegistry
    from .research.scheduled_cycle import (
        PROVIDER_MODE_FIXTURE,
        PROVIDER_MODE_REAL,
        run_scheduled_research_cycle,
    )
    from .research.scheduled_research_config import load_scheduled_research_config
    from .research.usage import load_pricing_config, select_pricing
    from .shadow.config import load_shadow_operations_config
    from .shadow.scheduler import DEPLOYMENT_SOURCE_MANUAL, run_due_shadow_cycle
    from .storage.research_cycle_repositories import SQLiteResearchCycleRepository
    from .storage.research_repositories import SQLiteResearchRepository
    from .universe.tickers import default_universe

    if provider_mode not in (PROVIDER_MODE_FIXTURE, PROVIDER_MODE_REAL):
        return {"error": f"unknown --provider-mode {provider_mode!r} — must be 'fixture' or 'real'", "status": "INTERNAL_ERROR"}

    shadow_config = (
        load_shadow_operations_config(shadow_config_path) if shadow_config_path is not None
        else load_shadow_operations_config()
    )
    sr_config = (
        load_scheduled_research_config(scheduled_research_config_path)
        if scheduled_research_config_path is not None else load_scheduled_research_config()
    )
    cycle_config = sr_config.to_cycle_configuration(provider_mode=provider_mode)
    research_config = load_research_config(research_config_path) if research_config_path is not None else load_research_config()
    pricing_entries = load_pricing_config()
    cfg = load_config()

    if provider_mode == PROVIDER_MODE_REAL:
        research_provider_name = research_config.provider
        research_model_name = research_config.model if research_config.provider in {"anthropic", "claude_code"} else f"{research_config.provider}-v1"
    else:
        research_provider_name, research_model_name = "deterministic", "deterministic-v1"

    # --- Preflight: fail closed BEFORE any lease/budget/DB-session work
    # (docs/milestone-7.1.md Step 21 pricing-failure requirement: "fail
    # before lease work that could spend money, or before any Claude call").
    real_research_provider = None
    if provider_mode == PROVIDER_MODE_REAL and research_config.provider == "anthropic":
        research_config.require_ready()  # raises ResearchConfigError if model is unset
        if not cfg.anthropic_api_key:
            return {
                "error": "research.provider=anthropic requires ANTHROPIC_API_KEY to be configured",
                "status": "MISSING_CREDENTIALS",
            }
        as_of_date = datetime.now(_tz.utc).date().isoformat()
        if select_pricing(pricing_entries, "anthropic", research_model_name or "", as_of_date) is None:
            return {
                "error": (
                    f"no pricing configured for provider=anthropic model={research_model_name!r} "
                    f"as_of={as_of_date!r} — scheduled real-Claude operation is blocked (fails closed)"
                ),
                "status": "PRICING_NOT_CONFIGURED",
            }
    elif provider_mode == PROVIDER_MODE_REAL and research_config.provider == "claude_code":
        from .research.claude_code_provider import ClaudeCodeResearchProvider

        try:
            research_config.require_ready()
            as_of_date = datetime.now(_tz.utc).date().isoformat()
            if select_pricing(pricing_entries, "claude_code", research_model_name or "", as_of_date) is None:
                return {
                    "error": "Claude Code API-equivalent pricing is not configured",
                    "status": "PRICING_NOT_CONFIGURED",
                }
            real_research_provider = ClaudeCodeResearchProvider(
                research_config.build_claude_code_provider_config(pricing_entries=pricing_entries)
            )
            # Version/auth preflight happens before opening a DB session, lease,
            # or budget reservation and consumes no inference call.
            real_research_provider.preflight()
        except Exception as exc:
            try:
                from .shadow import pause as pause_mod

                with session(db_path) as preflight_conn:
                    current = pause_mod.current_state(preflight_conn)
                    if current.state == pause_mod.STATE_ACTIVE:
                        pause_mod.request_pause(
                            preflight_conn,
                            reason=f"Claude Code preflight failed: {getattr(exc, 'code', 'CLAUDE_CODE_PREFLIGHT_FAILED')}",
                            source=pause_mod.SOURCE_AUTOMATIC_HEALTH_RULE,
                            target_state=pause_mod.STATE_PAUSED_PROVIDER_HEALTH,
                            clock=lambda: datetime.now(_tz.utc),
                        )
            except Exception:
                pass
            return {
                "error": "Claude Code provider preflight failed",
                "status": "PROVIDER_PREFLIGHT_FAILED",
                "failure_code": getattr(exc, "code", "CLAUDE_CODE_PREFLIGHT_FAILED"),
            }

    candidate_symbols = tuple(s.upper() for s in symbols) if symbols else (
        ("AAPL", "MSFT", "SHEL") if provider_mode == PROVIDER_MODE_FIXTURE else ()
    )
    if not candidate_symbols:
        return {"error": "no candidate symbols supplied — pass --symbol at least once for --provider-mode real", "status": "INTERNAL_ERROR"}

    def _cycle_kwargs_builder(cycle_symbols, as_of):
        registry, used_providers = _build_evidence_provider_registry(provider_mode, cfg=cfg, conn=conn)
        if provider_mode == PROVIDER_MODE_REAL and research_config.provider == "anthropic":
            from .research.anthropic_provider import AnthropicProviderConfig, AnthropicResearchProvider

            provider = AnthropicResearchProvider(AnthropicProviderConfig(
                api_key=cfg.anthropic_api_key, request_timeout_seconds=research_config.request_timeout_seconds,
                pricing_entries=pricing_entries,
            ))
        elif provider_mode == PROVIDER_MODE_REAL and research_config.provider == "claude_code":
            assert real_research_provider is not None
            provider = real_research_provider
        else:
            provider = DeterministicResearchProvider()
        return dict(
            cycle_repository=SQLiteResearchCycleRepository(conn), universe=default_universe(),
            screening_config=load_screening_config(), scoring_config=load_scoring_config(),
            evidence_providers=registry, research_provider=provider,
            research_provider_name=research_provider_name, research_model_name=research_model_name,
            research_configuration=research_config, research_repository=SQLiteResearchRepository(conn),
            prompt_registry=PromptRegistry(),
            portfolio=PortfolioState(account_equity=Decimal("100000"), settled_cash=Decimal("100000"), as_of=as_of),
            paper_submitter=None, git_sha=_git_sha(),
        )

    now = datetime.now(_tz.utc)
    try:
        with session(db_path) as conn:
            result = run_due_shadow_cycle(
                now=now, conn=conn, shadow_config=shadow_config, cycle_configuration=cycle_config,
                candidate_symbols=lambda: candidate_symbols, run_cycle=run_scheduled_research_cycle,
                cycle_kwargs_builder=_cycle_kwargs_builder, pricing_entries=pricing_entries,
                clock=lambda: datetime.now(_tz.utc), research_provider_name=research_provider_name,
                research_model_name=research_model_name, research_roles=research_config.roles,
                deployment_source=DEPLOYMENT_SOURCE_MANUAL,
            )
    except Exception as exc:  # only a genuine internal error is non-zero-exit-worthy
        return {"error": str(exc), "status": "INTERNAL_ERROR"}

    return {
        "status": result.status, "is_successful_no_op": result.is_successful_no_op, "is_blocked": result.is_blocked,
        "is_error": result.is_error, "provider_mode": provider_mode, "research_provider": research_provider_name,
        "research_model": research_model_name, "scheduler_run_id": result.scheduler_run_id,
        "intended_schedule_id": result.intended_schedule_id,
        "intended_schedule_time": result.intended_schedule_time.isoformat() if result.intended_schedule_time else None,
        "cycle_id": result.cycle_id, "symbols_attempted": result.symbols_attempted,
        "symbols_completed": result.symbols_completed, "symbols_skipped": result.symbols_skipped,
        "budget_reservation_id": result.budget_reservation_id, "budget_reserved_usd": result.budget_reserved_usd,
        "budget_consumed_usd": result.budget_consumed_usd, "failure_reason": result.failure_reason,
        "reason": result.reason,
        "cost_estimate_basis": (
            "SUBSCRIPTION_API_EQUIVALENT_ESTIMATE" if research_provider_name == "claude_code"
            else "DIRECT_API_ESTIMATE" if research_provider_name == "anthropic" else "NOT_APPLICABLE"
        ),
    }


def shadow_status_cli(db_path: Path) -> dict:
    """`shadow-status` CLI command (docs/milestone-7.md Step 25): current
    pause/kill state plus the last few scheduler-run and run-summary rows."""
    from .shadow import pause as pause_mod
    from .storage.shadow_alerts_repositories import list_run_summaries
    from .storage.shadow_operations_repositories import list_scheduler_runs

    with session(db_path) as conn:
        state = pause_mod.current_state(conn)
        runs = list_scheduler_runs(conn)[:5]
        summaries = list_run_summaries(conn)[:5]

    return {
        "pause_state": state.state, "pause_reason": state.reason, "pause_source": state.source,
        "pause_operator": state.operator, "pause_since": state.created_at.isoformat(),
        "recent_scheduler_runs": [_shadow_scheduler_run_view(r) for r in runs],
        "recent_run_summaries": [dict(s) for s in summaries],
    }


def shadow_readiness_cli(
    db_path: Path, *, research_config_path: Path | None = None, shadow_config_path: Path | None = None,
) -> dict:
    """`shadow-readiness` CLI command (docs/milestone-7.md Step 23/25;
    docs/milestone-7.2.md Part 12 added the `activation_readiness` block —
    an honest manual-vs-recurring activation decision built on top of the
    same category readiness, never a new independent data source)."""
    import os
    from datetime import timezone as _tz

    from .research.configuration import load_research_config
    from .shadow.config import load_shadow_operations_config
    from .shadow.readiness import build_readiness_report, evaluate_activation_readiness

    shadow_config = (
        load_shadow_operations_config(shadow_config_path) if shadow_config_path is not None
        else load_shadow_operations_config()
    )
    now = datetime.now(_tz.utc)

    environmentally_blocked_reason = None
    provider_preflight = None
    try:
        research_config = (
            load_research_config(research_config_path) if research_config_path is not None
            else load_research_config()
        )
        if research_config.provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
            environmentally_blocked_reason = "research.provider=anthropic but ANTHROPIC_API_KEY is absent"
        elif research_config.provider == "claude_code":
            from .research.claude_code_provider import ClaudeCodeResearchProvider
            from .research.usage import load_pricing_config

            try:
                provider = ClaudeCodeResearchProvider(
                    research_config.build_claude_code_provider_config(pricing_entries=load_pricing_config())
                )
                preflight = provider.preflight()
                provider_preflight = {
                    "provider": "claude_code", "configured_model": research_config.model,
                    "binary_available": True, "binary_version": preflight.binary_version,
                    "minimum_version_satisfied": True, "oauth_token_present": True,
                    "authenticated": preflight.authenticated,
                    "authentication_method": preflight.authentication_method,
                    "usage_metadata_required": True,
                    "cost_estimate_basis": "SUBSCRIPTION_API_EQUIVALENT_ESTIMATE",
                    "paper_submission_enabled": False, "external_execution_reachable": False,
                }
            except Exception as exc:
                environmentally_blocked_reason = (
                    f"Claude Code preflight failed: {getattr(exc, 'code', 'CLAUDE_CODE_PREFLIGHT_FAILED')}"
                )
                provider_preflight = {
                    "provider": "claude_code", "configured_model": research_config.model,
                    "binary_available": False, "binary_version": None,
                    "minimum_version_satisfied": False,
                    "oauth_token_present": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
                    "authenticated": False, "authentication_method": None,
                    "usage_metadata_required": True,
                    "cost_estimate_basis": "SUBSCRIPTION_API_EQUIVALENT_ESTIMATE",
                    "failure_code": getattr(exc, "code", "CLAUDE_CODE_PREFLIGHT_FAILED"),
                    "paper_submission_enabled": False, "external_execution_reachable": False,
                }
    except Exception:
        pass  # research config errors are reported by other commands; never block readiness reporting itself

    with session(db_path) as conn:
        report = build_readiness_report(conn, now, shadow_config)
        activation = evaluate_activation_readiness(
            conn, now, shadow_config, environmentally_blocked_reason=environmentally_blocked_reason,
        )
        if provider_preflight is not None and provider_preflight.get("provider") == "claude_code":
            today_calls = conn.execute(
                "SELECT COUNT(*) FROM research_attempts WHERE provider = 'claude_code' AND created_at LIKE ?",
                (now.date().isoformat() + "%",),
            ).fetchone()[0]
            month_calls = conn.execute(
                "SELECT COUNT(*) FROM research_attempts WHERE provider = 'claude_code' AND created_at LIKE ?",
                (now.strftime("%Y-%m") + "%",),
            ).fetchone()[0]
            latest_model = conn.execute(
                "SELECT resolved_model_name FROM research_attempts WHERE provider = 'claude_code' "
                "AND resolved_model_name IS NOT NULL ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            provider_preflight.update({
                "resolved_model": latest_model[0] if latest_model else None,
                "cycle_call_budget_available": shadow_config.budgets.max_claude_code_calls_per_cycle > 0,
                "daily_call_budget_available": today_calls < shadow_config.budgets.max_claude_code_calls_per_day,
                "monthly_call_budget_available": month_calls < shadow_config.budgets.max_claude_code_calls_per_month,
            })

    return {
        "as_of": report.as_of.isoformat(), "policy_version": report.policy_version,
        "overall_status": report.overall_status,
        "categories": [
            {"category": c.category, "status": c.status, "reasons": list(c.reasons), "metrics": c.metrics}
            for c in report.categories
        ],
        "completed_cycle_count": report.completed_cycle_count,
        "real_provider_cycle_count": report.real_provider_cycle_count,
        "evidence_completeness_rate": report.evidence_completeness_rate,
        "role_completion_rate": report.role_completion_rate,
        "retry_exhaustion_rate": report.retry_exhaustion_rate,
        "unsupported_claim_rate": report.unsupported_claim_rate,
        "provider_failure_rate": report.provider_failure_rate,
        "cost_per_completed_cycle_usd": str(report.cost_per_completed_cycle_usd) if report.cost_per_completed_cycle_usd is not None else None,
        "average_cycle_duration_seconds": report.average_cycle_duration_seconds,
        "scheduler_miss_count": report.scheduler_miss_count, "lease_conflict_count": report.lease_conflict_count,
        "reconciliation_mismatch_count": report.reconciliation_mismatch_count,
        "alert_delivery_failure_count": report.alert_delivery_failure_count, "reasons": list(report.reasons),
        "activation_readiness": {"status": activation.status, "reasons": list(activation.reasons)},
        "provider_preflight": provider_preflight,
    }


def shadow_run_history_cli(db_path: Path, *, status: str | None = None, limit: int = 20) -> dict:
    """`shadow-run-history` CLI command (docs/milestone-7.md Step 25): query
    `shadow_scheduler_runs`/`shadow_run_summaries`, filterable by `status`,
    else recent N (`limit`)."""
    from .storage.shadow_alerts_repositories import list_run_summaries
    from .storage.shadow_operations_repositories import list_scheduler_runs

    with session(db_path) as conn:
        runs = list_scheduler_runs(conn, status=status)[:limit]
        summaries = list_run_summaries(conn)[:limit]

    return {
        "filter_status": status, "limit": limit,
        "scheduler_runs": [_shadow_scheduler_run_view(r) for r in runs],
        "run_summaries": [dict(s) for s in summaries],
    }


def shadow_budget_status_cli(db_path: Path) -> dict:
    """`shadow-budget-status` CLI command (docs/milestone-7.md Step 25):
    daily/monthly usage vs configured caps from `shadow/budget.py`."""
    from datetime import timezone as _tz
    from decimal import Decimal

    from .shadow import budget as budget_mod
    from .shadow.config import load_shadow_operations_config
    from .research.configuration import load_research_config
    from .storage.shadow_operations_repositories import list_budget_reservations, list_budget_usage

    shadow_config = load_shadow_operations_config()
    research_config = load_research_config()
    is_claude_code = research_config.provider == "claude_code"
    now = datetime.now(_tz.utc)
    today_prefix = now.date().isoformat()
    month_prefix = now.strftime("%Y-%m")

    with session(db_path) as conn:
        today_usage = list_budget_usage(conn, usage_date_prefix=today_prefix)
        month_usage = list_budget_usage(conn, usage_date_prefix=month_prefix)
        live_reservations = list_budget_reservations(conn, status=budget_mod.RESERVATION_STATUS_RESERVED)
        claude_calls_today = conn.execute(
            "SELECT COUNT(*) FROM research_attempts WHERE provider = 'claude_code' AND created_at LIKE ?",
            (today_prefix + "%",),
        ).fetchone()[0]
        claude_calls_month = conn.execute(
            "SELECT COUNT(*) FROM research_attempts WHERE provider = 'claude_code' AND created_at LIKE ?",
            (month_prefix + "%",),
        ).fetchone()[0]

    spent_today = sum((Decimal(r["actual_cost_usd"]) for r in today_usage), Decimal("0"))
    spent_month = sum((Decimal(r["actual_cost_usd"]) for r in month_usage), Decimal("0"))
    live_reserved = sum((Decimal(r["reserved_estimated_cost_usd"]) for r in live_reservations), Decimal("0"))
    daily_cap = Decimal(str(
        shadow_config.budgets.max_claude_code_api_equivalent_cost_per_day_usd if is_claude_code
        else shadow_config.budgets.max_actual_cost_per_day_usd
    ))
    monthly_cap = Decimal(str(
        shadow_config.budgets.max_claude_code_api_equivalent_cost_per_month_usd if is_claude_code
        else shadow_config.budgets.max_actual_cost_per_month_usd
    ))

    return {
        "as_of": now.isoformat(),
        "provider": research_config.provider,
        "cost_estimate_basis": (
            "SUBSCRIPTION_API_EQUIVALENT_ESTIMATE" if is_claude_code else "DIRECT_API_ESTIMATE"
        ),
        "daily_cap_usd": str(daily_cap),
        "monthly_cap_usd": str(monthly_cap),
        "spent_today_usd": str(spent_today), "spent_month_usd": str(spent_month),
        "live_reserved_usd": str(live_reserved), "live_reservation_count": len(live_reservations),
        "remaining_today_usd": str(daily_cap - spent_today - live_reserved),
        "remaining_month_usd": str(monthly_cap - spent_month - live_reserved),
        "claude_code_calls_today": claude_calls_today,
        "claude_code_calls_month": claude_calls_month,
        "claude_code_daily_call_cap": shadow_config.budgets.max_claude_code_calls_per_day,
        "claude_code_monthly_call_cap": shadow_config.budgets.max_claude_code_calls_per_month,
    }


def shadow_alerts_cli(db_path: Path, *, severity: str | None = None, limit: int = 20) -> dict:
    """`shadow-alerts` CLI command (docs/milestone-7.md Step 25): recent
    `shadow_alerts` plus delivery status, filterable by `severity`."""
    from .storage.shadow_alerts_repositories import list_alert_deliveries, list_alerts

    with session(db_path) as conn:
        alerts = list_alerts(conn, severity=severity)[:limit]
        result = []
        for a in alerts:
            deliveries = list_alert_deliveries(conn, a["alert_id"])
            result.append({**dict(a), "deliveries": [dict(d) for d in deliveries]})

    return {"filter_severity": severity, "limit": limit, "alerts": result}


_ALERT_LIST_DEFAULT_LIMIT = 50
_ALERT_LIST_MAX_LIMIT = 200


def shadow_alert_list_cli(
    db_path: Path, *, severity: str | None = None, unresolved_only: bool = False, limit: int = _ALERT_LIST_DEFAULT_LIMIT,
) -> dict:
    """`shadow-alert-list` CLI command (Milestone 9.2 Section 9): read-only,
    bounded, deterministically ordered (newest first, `shadow_alerts_repositories.
    list_alerts`'s own `ORDER BY created_at DESC`), sanitized listing — never a
    raw provider payload, never a credential. `limit` is clamped to
    `[1, _ALERT_LIST_MAX_LIMIT]` so an operator can never request an
    unbounded dump."""
    from .storage.shadow_alerts_repositories import list_alerts

    bounded_limit = max(1, min(limit, _ALERT_LIST_MAX_LIMIT))
    with session(db_path) as conn:
        alerts = list_alerts(conn, severity=severity, unresolved_only=unresolved_only, limit=bounded_limit)

    return {
        "filter_severity": severity, "unresolved_only": unresolved_only, "limit": bounded_limit,
        "count": len(alerts),
        "alerts": [
            {
                "alert_id": a["alert_id"], "alert_type": a["alert_type"], "severity": a["severity"],
                "created_at": a["created_at"], "resolved": a["resolved_at"] is not None,
                "resolved_at": a["resolved_at"], "resolved_by": a["resolved_by"],
                "resolved_reason": a["resolved_reason"], "message": (a["message"] or "")[:500],
            }
            for a in alerts
        ],
    }


def shadow_alert_resolve_cli(db_path: Path, *, alert_id: str, operator: str, reason: str) -> dict:
    """`shadow-alert-resolve` CLI command (Milestone 9.2 Section 10): audited,
    idempotent alert resolution — fails closed on an unknown alert, never
    overwrites an already-resolved alert's original operator/reason/
    resolved_at, never clears pause/kill state, never implies the underlying
    incident is repaired. No bulk "resolve all" command exists here or
    anywhere else in this milestone."""
    from datetime import timezone as _tz

    from .storage.shadow_alerts_repositories import load_alert, resolve_alert

    if not operator or not operator.strip():
        return {"error": "--operator is required and must be non-empty"}
    if not reason or not reason.strip():
        return {"error": "--reason is required and must be non-empty"}

    with session(db_path) as conn:
        existing = load_alert(conn, alert_id)
        if existing is None:
            return {"error": f"unknown alert_id {alert_id!r} — fails closed"}
        resolve_alert(conn, alert_id, resolved_by=operator, reason=reason, resolved_at=datetime.now(_tz.utc).isoformat())
        current = load_alert(conn, alert_id)
        assert current is not None  # just resolved (or already resolved) above — always present now

    return {
        "alert_id": current["alert_id"], "resolved": current["resolved_at"] is not None,
        "resolved_at": current["resolved_at"], "resolved_by": current["resolved_by"],
        "resolved_reason": current["resolved_reason"],
        "newly_resolved_this_call": existing["resolved_at"] is None,
    }


def shadow_pause_cli(db_path: Path, reason: str) -> dict:
    """`shadow-pause --reason "..."` CLI command (docs/milestone-7.md Step
    25). Delegates to `shadow/pause.py::request_pause` with
    `source="OPERATOR"` — the operator action is persisted by `pause.py`
    itself, this is thin wiring only."""
    from datetime import timezone as _tz

    from .shadow import pause as pause_mod

    if not reason or not reason.strip():
        return {"error": "--reason is required and must be non-empty"}

    try:
        with session(db_path) as conn:
            state = pause_mod.request_pause(
                conn, reason, pause_mod.SOURCE_OPERATOR, clock=lambda: datetime.now(_tz.utc),
            )
    except pause_mod.PauseStateError as exc:
        return {"error": str(exc)}

    return {"state": state.state, "previous_state": state.previous_state, "reason": state.reason, "source": state.source}


def shadow_resume_cli(db_path: Path, reason: str, operator: str) -> dict:
    """`shadow-resume --reason "..."` CLI command (docs/milestone-7.md Step
    25). Delegates to `shadow/pause.py::resume` — cannot override `KILLED`;
    `pause.py` itself raises `PauseStateError` in that case and this
    function does not add a bypass."""
    from datetime import timezone as _tz

    from .shadow import pause as pause_mod

    if not reason or not reason.strip():
        return {"error": "--reason is required and must be non-empty"}
    if not operator or not operator.strip():
        return {"error": "--operator is required and must be non-empty"}

    try:
        with session(db_path) as conn:
            state = pause_mod.resume(conn, reason, operator, clock=lambda: datetime.now(_tz.utc))
    except pause_mod.PauseStateError as exc:
        return {"error": str(exc)}

    return {"state": state.state, "previous_state": state.previous_state, "reason": state.reason, "source": state.source}


def shadow_kill_cli(db_path: Path, reason: str, operator: str) -> dict:
    """`shadow-kill --reason "..."` CLI command (docs/milestone-7.md Step 25)."""
    from datetime import timezone as _tz

    from .shadow import pause as pause_mod

    if not reason or not reason.strip():
        return {"error": "--reason is required and must be non-empty"}
    if not operator or not operator.strip():
        return {"error": "--operator is required and must be non-empty"}

    try:
        with session(db_path) as conn:
            state = pause_mod.kill(conn, reason, operator, clock=lambda: datetime.now(_tz.utc))
    except pause_mod.PauseStateError as exc:
        return {"error": str(exc)}

    return {"state": state.state, "previous_state": state.previous_state, "reason": state.reason, "source": state.source}


def shadow_force_clear_kill_cli(db_path: Path, reason: str, operator: str) -> dict:
    """`shadow-force-clear-kill --reason "..."` CLI command — the SEPARATE,
    clearly-scarier command required to leave `KILLED` (docs/milestone-7.md
    Step 25: "resume cannot override KILLED without a separate explicit
    process"). Delegates to `shadow/pause.py::force_clear_kill`, which
    itself always records the operator action."""
    from datetime import timezone as _tz

    from .shadow import pause as pause_mod

    if not reason or not reason.strip():
        return {"error": "--reason is required and must be non-empty"}
    if not operator or not operator.strip():
        return {"error": "--operator is required and must be non-empty"}

    try:
        with session(db_path) as conn:
            state = pause_mod.force_clear_kill(conn, reason, operator, clock=lambda: datetime.now(_tz.utc))
    except pause_mod.PauseStateError as exc:
        return {"error": str(exc)}

    return {"state": state.state, "previous_state": state.previous_state, "reason": state.reason, "source": state.source}


def shadow_lease_status_cli(db_path: Path) -> dict:
    """`shadow-lease-status` CLI command (docs/milestone-7.md Step 25):
    current lease state(s) from `shadow_run_leases`."""
    from .storage.shadow_operations_repositories import list_leases

    with session(db_path) as conn:
        leases = list_leases(conn)

    return {"leases": [dict(l) for l in leases]}


def shadow_health_explain_cli(
    db_path: Path, *, scheduler_run_id: str | None = None, cycle_id: str | None = None,
) -> dict:
    """`shadow-health-explain --scheduler-run-id <id>` (or `--cycle-id <id>`)
    CLI command (docs/milestone-7.2.md Part 10): explains every field-level
    health check behind one scheduler run's `shadow_run_summaries` verdict.
    Sanitized output only (no credentials, no raw model content — the
    persisted `shadow_run_health_checks` rows this reads already only ever
    carry bounded, structured diagnostic fields). An unknown run is reported
    as `{"error": ...}` (mapped to a nonzero exit code by the caller), never
    an unhandled exception or a fabricated empty-but-successful result."""
    from .shadow.health import CHECK_NAMES_IN_ORDER
    from .storage.shadow_alerts_repositories import list_health_checks, load_run_summary
    from .storage.shadow_operations_repositories import find_scheduler_run_by_cycle_id

    if not scheduler_run_id and not cycle_id:
        return {"error": "either --scheduler-run-id or --cycle-id is required"}

    with session(db_path) as conn:
        resolved_scheduler_run_id = scheduler_run_id
        if resolved_scheduler_run_id is None:
            scheduler_run = find_scheduler_run_by_cycle_id(conn, cycle_id)
            if scheduler_run is None:
                return {"error": f"no scheduler run found for cycle_id={cycle_id!r}"}
            resolved_scheduler_run_id = scheduler_run["scheduler_run_id"]

        summary = load_run_summary(conn, resolved_scheduler_run_id)
        if summary is None:
            return {"error": f"no shadow_run_summaries row found for scheduler_run_id={resolved_scheduler_run_id!r}"}

        checks = list_health_checks(conn, scheduler_run_id=resolved_scheduler_run_id)

    # Deterministic ordering: the same canonical, versioned dimension order
    # `shadow/health.py::evaluate_cycle_health` builds `HealthResult.checks`
    # in — not merely SQL's own alphabetical `ORDER BY check_name`.
    checks_by_name = {c["check_name"]: c for c in checks}
    ordered_checks = [checks_by_name[name] for name in CHECK_NAMES_IN_ORDER if name in checks_by_name]

    return {
        "scheduler_run_id": resolved_scheduler_run_id,
        "cycle_id": checks[0]["cycle_id"] if checks else None,
        "health_status": summary["health_status"],
        "policy_version": summary["policy_version"],
        "reasons": json.loads(summary["health_reasons_json"]),
        "triggering_flags": [
            c["check_name"] for c in ordered_checks if c["check_status"] == "FAIL" and c["pause_flag_enabled"]
        ],
        "checks": [
            {
                "check_name": c["check_name"], "status": c["check_status"], "input_value": c["input_value"],
                "input_unit": c["input_unit"], "threshold_value": c["threshold_value"],
                "threshold_unit": c["threshold_unit"], "comparison": c["comparison"],
                "applicable": bool(c["applicable"]), "pause_flag_enabled": bool(c["pause_flag_enabled"]),
                "reason": c["reason"],
            }
            for c in ordered_checks
        ],
    }


def corporate_status_cli(symbol: str, as_of_str: str, db_path: Path) -> dict:
    """`corporate-status --symbol SYM --as-of ISO8601` CLI command
    (docs/milestone-7.md Step 25). Uses the real `SecEdgarClient` — no
    credentials are required for SEC EDGAR (only a `User-Agent` contact
    string, matching every other real-SEC code path in this repository)."""
    from .evidence_providers.cache import ProviderCache
    from .evidence_providers.config import load_evidence_provider_config
    from .evidence_providers.corporate_status_adapters import derive_corporate_status
    from .evidence_providers.http_client import HttpJsonClient
    from .evidence_providers.rate_limits import MinIntervalRateLimiter
    from .evidence_providers.sec_provider import SecEdgarClient

    try:
        as_of = datetime.fromisoformat(as_of_str.replace("Z", "+00:00"))
    except ValueError as exc:
        return {"error": f"invalid --as-of: {exc}"}
    if as_of.tzinfo is None:
        return {"error": "--as-of must be timezone-aware (e.g. 2026-07-10T20:00:00Z)"}

    provider_config = load_evidence_provider_config()
    with session(db_path) as conn:
        persist_hook = _make_persist_hook(conn)
        sec_cache = ProviderCache(clock=time.monotonic, on_response=persist_hook)
        sec_http = HttpJsonClient(
            base_headers={"User-Agent": provider_config.sec.user_agent_contact},
            rate_limiter=MinIntervalRateLimiter(provider_config.sec.min_request_interval_seconds),
            max_attempts=provider_config.sec.max_attempts, timeout_seconds=provider_config.sec.request_timeout_seconds,
            provider="sec-edgar", on_response=persist_hook,
        )
        sec = SecEdgarClient(http_client=sec_http, cache=sec_cache, user_agent=provider_config.sec.user_agent_contact)
        evidence = derive_corporate_status(symbol.upper(), sec_client=sec, as_of=as_of)

    def _signal(s) -> dict:
        return {"signal_type": s.signal_type, "status": s.status, "basis": s.basis, "evidence_ref_count": len(s.evidence_refs)}

    return {
        "symbol": evidence.symbol, "as_of": evidence.as_of.isoformat(), "reporting_status": evidence.reporting_status,
        "reporting_status_reason": evidence.reporting_status_reason,
        "earliest_reliable_filing_date": evidence.earliest_reliable_filing_date.isoformat() if evidence.earliest_reliable_filing_date else None,
        "operating_history_years": str(evidence.operating_history_years) if evidence.operating_history_years is not None else None,
        "has_latest_annual_filing": evidence.latest_annual_filing is not None,
        "has_latest_quarterly_filing": evidence.latest_quarterly_filing is not None,
        "late_filing_notice_count": len(evidence.late_filing_notices),
        "bankruptcy_signals": [_signal(s) for s in evidence.bankruptcy_signals],
        "delisting_signals": [_signal(s) for s in evidence.delisting_signals],
        "registration_status_signals": [_signal(s) for s in evidence.registration_status_signals],
        "shell_company_signals": [_signal(s) for s in evidence.shell_company_signals],
        "going_concern_signals": [_signal(s) for s in evidence.going_concern_signals],
        "completeness_status": evidence.completeness_status,
        "has_any_critical_uncertainty": evidence.has_any_critical_uncertainty(),
        "source_count": len(evidence.sources),
    }


def retention_plan_cli() -> dict:
    """`retention-plan` CLI command (docs/milestone-7.md Step 26). Prints the
    classification only — does not even open the research database, since
    the plan itself (tier assignment, rationale) does not depend on current
    row counts. Read-only, no action taken."""
    from .shadow.retention import RETENTION_PLAN

    return {
        "policy_version": "retention/v1",
        "rules": [
            {
                "table_name": r.table_name, "tier": r.tier, "retention_days": r.retention_days,
                "created_at_column": r.created_at_column, "rationale": r.rationale,
            }
            for r in RETENTION_PLAN
        ],
    }


def retention_apply_cli(db_path: Path, *, dry_run: bool) -> dict:
    """`retention-apply --dry-run` CLI command (docs/milestone-7.md Step 26).
    Without `--dry-run`, `shadow/retention.py::apply_retention` raises
    `NotImplementedError` — this function does not catch it, so it
    propagates to the caller (`main()` maps it to a non-zero exit with a
    structured error message, never a silent success)."""
    from datetime import timezone as _tz

    from .shadow.retention import apply_retention

    now = datetime.now(_tz.utc)
    with session(db_path) as conn:
        report = apply_retention(conn, now, dry_run=dry_run)

    return {
        "policy_version": report.policy_version, "as_of": report.as_of.isoformat(), "dry_run": dry_run,
        "diffs": [
            {
                "table_name": d.table_name, "tier": d.tier, "table_exists": d.table_exists,
                "current_row_count": d.current_row_count, "eligible_row_count": d.eligible_row_count,
                "action_if_applied": d.action_if_applied,
            }
            for d in report.diffs
        ],
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


def _parse_iso_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trading-research", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="Analyze one ticker with mocked market data and selected Reddit evidence")
    p_analyze.add_argument("ticker")
    p_analyze.add_argument("--provider-mode", choices=("fixture", "reddit_free"), default="reddit_free")

    # Milestone 11.3 Part 33: the legacy `paper/ledger.py` subsystem (Milestone
    # 3/4, predates the isolated `paper_books` subsystem hardened through
    # Milestone 11.2) is quarantined behind a `legacy-paper-*` command-name
    # prefix plus a required `--i-understand-this-is-the-legacy-ledger` flag,
    # so it can never be confused with, or accidentally invoked instead of,
    # the active `paper-book-*`/`external-paper-*` commands. It uses a wholly
    # separate set of database tables (`simulated_*`/`paper_cash_state`/
    # `paper_execution_*`, not any `paper_book_*`/`paper_external_*` table)
    # and cannot feed campaigns, recurring scheduling, or external execution
    # — none of those subsystems import `paper.ledger` or read its tables.
    # Retained (not removed) only because it is still exercised by existing
    # regression tests and there is no destructive-migration plan for it.
    _LEGACY_PAPER_HELP_SUFFIX = " [DEPRECATED — legacy pre-paper_books ledger; use paper-book-* / external-paper-* instead]"

    p_legacy_status = sub.add_parser(
        "legacy-paper-status", help="Show legacy paper ledger state" + _LEGACY_PAPER_HELP_SUFFIX
    )
    p_legacy_status.add_argument(
        "--i-understand-this-is-the-legacy-ledger", required=True, action="store_true",
        help="explicit acknowledgement required: this is the deprecated pre-Milestone-8 ledger, not paper_books",
    )

    p_execute_paper = sub.add_parser(
        "legacy-paper-execute",
        help="Run one frozen recommendation through legacy paper execution (Milestone 3/4)" + _LEGACY_PAPER_HELP_SUFFIX,
    )
    p_execute_paper.add_argument("--recommendation-id", required=True)
    p_execute_paper.add_argument(
        "--adapter", choices=("deterministic", "credentialed"), default="deterministic",
        help="deterministic (default, offline); credentialed is retained only to return a fail-closed migration error",
    )
    p_execute_paper.add_argument(
        "--i-understand-this-is-the-legacy-ledger", required=True, action="store_true",
        help="explicit acknowledgement required: this is the deprecated pre-Milestone-8 ledger, not paper_books",
    )

    sub.add_parser("paper-runtime-health", help="Health-check the isolated LumiBot paper-runtime process (Milestone 4)")

    p_legacy_sync = sub.add_parser(
        "legacy-paper-sync-orders",
        help="Poll the paper runtime for order-state changes and apply fills to the legacy ledger (Milestone 4)"
        + _LEGACY_PAPER_HELP_SUFFIX,
    )
    p_legacy_sync.add_argument(
        "--i-understand-this-is-the-legacy-ledger", required=True, action="store_true",
        help="explicit acknowledgement required: this is the deprecated pre-Milestone-8 ledger, not paper_books",
    )

    p_legacy_reconcile = sub.add_parser(
        "legacy-paper-reconcile",
        help="Reconcile account/positions against the credentialed legacy paper broker (Milestone 4)"
        + _LEGACY_PAPER_HELP_SUFFIX,
    )
    p_legacy_reconcile.add_argument(
        "--i-understand-this-is-the-legacy-ledger", required=True, action="store_true",
        help="explicit acknowledgement required: this is the deprecated pre-Milestone-8 ledger, not paper_books",
    )

    p_evaluate = sub.add_parser(
        "evaluate-recommendations", help="Compute forward-performance evaluations for recommendations (Milestone 4)"
    )
    p_evaluate.add_argument("--recommendation-id", action="append", dest="recommendation_ids", required=True)

    sub.add_parser("paper-performance", help="Aggregate portfolio/strategy metrics over evaluations (Milestone 4)")

    p_build_evidence = sub.add_parser("build-evidence", help="Build and persist a point-in-time evidence snapshot (Milestone 5)")
    p_build_evidence.add_argument("--symbol", required=True)
    p_build_evidence.add_argument("--as-of", required=True, help="ISO-8601 timezone-aware timestamp, e.g. 2026-07-01T20:00:00Z")

    p_run_research = sub.add_parser("run-research", help="Invoke the research committee for a persisted evidence snapshot (Milestone 5)")
    p_run_research.add_argument("--snapshot-id", required=True)
    p_run_research.add_argument("--provider", choices=("deterministic", "anthropic", "claude_code"), default="deterministic")

    p_claude_code_preflight = sub.add_parser(
        "claude-code-provider-preflight",
        help="Check Claude Code version and subscription OAuth status without an inference call",
    )
    p_claude_code_preflight.add_argument("--research-config", type=Path, default=None)

    p_replay_research = sub.add_parser("replay-research", help="Deterministically replay a persisted research run — never calls a provider (Milestone 5)")
    p_replay_research.add_argument("--research-run-id", required=True)

    p_compare_arms = sub.add_parser("compare-research-arms", help="Show baseline vs Claude-enhanced experiment assignments (Milestone 5)")
    p_compare_arms.add_argument("--experiment-id", required=True)

    sub.add_parser("research-performance", help="Research-run outcome rates: completed/incomplete/failed (Milestone 5)")

    sub.add_parser("research-usage", help="Token/latency/cost aggregation by role over persisted research attempts (Milestone 5)")

    p_research_failures = sub.add_parser(
        "research-failures", help="Sanitized, structured failure diagnostics for one research run (Milestone 6.1)"
    )
    p_research_failures.add_argument("--research-run-id", required=True)
    p_research_failures.add_argument("--role", default=None)
    p_research_failures.add_argument("--attempt", type=int, default=None, dest="attempt_number")
    p_research_failures.add_argument("--stage", default=None)
    p_research_failures.add_argument("--code", default=None)

    sub.add_parser(
        "research-failure-metrics",
        help="Deterministic failure-rate/token/latency metrics over persisted research attempts (Milestone 6.1)",
    )

    sub.add_parser("provider-health", help="Real evidence-provider health status (Milestone 6)")

    p_fetch_evidence = sub.add_parser("fetch-evidence", help="Build and persist a real (or fixture) point-in-time evidence snapshot (Milestone 6)")
    p_fetch_evidence.add_argument("--symbol", required=True)
    p_fetch_evidence.add_argument("--as-of", help="ISO-8601 timezone-aware timestamp; defaults to the current UTC time")
    p_fetch_evidence.add_argument("--provider-mode", choices=("fixture", "real", "reddit_free"), default="fixture")

    p_run_cycle = sub.add_parser("run-research-cycle", help="Run one scheduled research cycle over a bounded candidate set (Milestone 6)")
    p_run_cycle.add_argument("--as-of", required=True, help="ISO-8601 timezone-aware timestamp, e.g. 2026-07-01T20:00:00Z")
    p_run_cycle.add_argument("--provider-mode", choices=("fixture", "real"), default="fixture")
    p_run_cycle.add_argument("--symbol", action="append", dest="symbols", help="Candidate symbol (repeatable); fixture mode defaults to AAPL/MSFT/SHEL")

    p_resume_cycle = sub.add_parser("resume-research-cycle", help="Resume a previously started scheduled research cycle by cycle_id (Milestone 6)")
    p_resume_cycle.add_argument("--cycle-id", required=True)

    p_eval_cycle = sub.add_parser("evaluate-research-cycle", help="Compute forward-performance evaluations for every recommendation a cycle produced (Milestone 6)")
    p_eval_cycle.add_argument("--cycle-id", required=True)

    sub.add_parser("compare-research-cycles", help="List every persisted scheduled research cycle and its outcome counts (Milestone 6)")

    p_promotion_status = sub.add_parser("research-promotion-status", help="Deterministic promotion-gate status for one experiment (Milestone 6) — never a live-trading status")
    p_promotion_status.add_argument("--experiment-id", required=True)

    sub.add_parser("evidence-provider-usage", help="Real evidence-provider request/cache-hit counts (Milestone 6)")

    p_run_due_shadow_cycle = sub.add_parser("run-due-shadow-cycle", help="Single-invocation scheduler entry point for shadow operations (Milestone 7)")
    p_run_due_shadow_cycle.add_argument("--provider-mode", choices=("fixture", "real"), default="fixture")
    p_run_due_shadow_cycle.add_argument("--symbol", dest="symbols", action="append", help="Repeatable; required for --provider-mode real")
    p_run_due_shadow_cycle.add_argument("--research-config", type=Path, default=None)
    p_run_due_shadow_cycle.add_argument("--scheduled-research-config", type=Path, default=None)
    p_run_due_shadow_cycle.add_argument("--shadow-config", type=Path, default=None)

    sub.add_parser("shadow-status", help="Current pause/kill state and recent shadow scheduler runs (Milestone 7)")

    p_shadow_readiness = sub.add_parser("shadow-readiness", help="Shadow-operations readiness report (Milestone 7)")
    p_shadow_readiness.add_argument("--research-config", type=Path, default=None)
    p_shadow_readiness.add_argument("--shadow-config", type=Path, default=None)

    p_shadow_run_history = sub.add_parser("shadow-run-history", help="Recent shadow scheduler runs/summaries (Milestone 7)")
    p_shadow_run_history.add_argument("--status", default=None)
    p_shadow_run_history.add_argument("--limit", type=int, default=20)

    sub.add_parser("shadow-budget-status", help="Daily/monthly shadow-operations budget usage vs caps (Milestone 7)")

    p_shadow_alerts = sub.add_parser("shadow-alerts", help="Recent shadow operational alerts and delivery status (Milestone 7)")
    p_shadow_alerts.add_argument("--severity", default=None, choices=["INFO", "WARNING", "ERROR", "CRITICAL"])
    p_shadow_alerts.add_argument("--limit", type=int, default=20)

    p_shadow_alert_list = sub.add_parser(
        "shadow-alert-list", help="Read-only, bounded operator alert listing (Milestone 9.2)",
    )
    p_shadow_alert_list.add_argument("--severity", default=None, choices=["INFO", "WARNING", "ERROR", "CRITICAL"])
    p_shadow_alert_list.add_argument("--unresolved-only", action="store_true")
    p_shadow_alert_list.add_argument("--limit", type=int, default=_ALERT_LIST_DEFAULT_LIMIT)

    p_shadow_alert_resolve = sub.add_parser(
        "shadow-alert-resolve", help="Audited, idempotent alert resolution — never clears pause/kill state (Milestone 9.2)",
    )
    p_shadow_alert_resolve.add_argument("--alert-id", required=True)
    p_shadow_alert_resolve.add_argument("--operator", required=True)
    p_shadow_alert_resolve.add_argument("--reason", required=True)

    p_shadow_pause = sub.add_parser("shadow-pause", help="Request an operator pause of shadow operations (Milestone 7)")
    p_shadow_pause.add_argument("--reason", required=True)

    p_shadow_resume = sub.add_parser("shadow-resume", help="Resume shadow operations from a PAUSED_* state — cannot override KILLED (Milestone 7)")
    p_shadow_resume.add_argument("--reason", required=True)
    p_shadow_resume.add_argument("--operator", required=True)

    p_shadow_kill = sub.add_parser("shadow-kill", help="Activate the shadow-operations kill switch (Milestone 7)")
    p_shadow_kill.add_argument("--reason", required=True)
    p_shadow_kill.add_argument("--operator", required=True)

    p_shadow_force_clear_kill = sub.add_parser(
        "shadow-force-clear-kill", help="Explicit, separate command to clear KILLED — not reachable via shadow-resume (Milestone 7)"
    )
    p_shadow_force_clear_kill.add_argument("--reason", required=True)
    p_shadow_force_clear_kill.add_argument("--operator", required=True)

    sub.add_parser("shadow-lease-status", help="Current shadow_run_leases state (Milestone 7)")

    p_shadow_health_explain = sub.add_parser(
        "shadow-health-explain", help="Explain every field-level health check behind a scheduler run's verdict (Milestone 7.2)"
    )
    p_shadow_health_explain.add_argument("--scheduler-run-id", default=None)
    p_shadow_health_explain.add_argument("--cycle-id", default=None)

    p_corporate_status = sub.add_parser("corporate-status", help="Real SEC-derived corporate-status evidence for one symbol (Milestone 7)")
    p_corporate_status.add_argument("--symbol", required=True)
    p_corporate_status.add_argument("--as-of", required=True)

    p_retention_plan = sub.add_parser("retention-plan", help="Print the data-retention plan — read-only, no action taken (Milestone 7)")

    p_retention_apply = sub.add_parser("retention-apply", help="Retention apply — --dry-run prints a diff; without it, raises NotImplementedError (Milestone 7)")
    p_retention_apply.add_argument("--dry-run", action="store_true")

    sub.add_parser("paper-book-list", help="List configured/opened isolated paper books (Milestone 8)")

    p_pb_show = sub.add_parser("paper-book-show", help="Show one isolated paper book's cash/positions (Milestone 8)")
    p_pb_show.add_argument("--book-id", required=True)

    p_pb_snapshot = sub.add_parser("paper-book-snapshot", help="Build a point-in-time mark-to-market snapshot for one book (Milestone 8)")
    p_pb_snapshot.add_argument("--book-id", required=True)
    p_pb_snapshot.add_argument("--as-of", required=True, help="ISO8601 timestamp")

    p_pb_run_cycle = sub.add_parser(
        "paper-book-run-cycle", help="Fixture-mode: size, risk-check, and locally simulate-fill one symbol across isolated books (Milestone 8)"
    )
    p_pb_run_cycle.add_argument("--cycle-id", required=True)
    p_pb_run_cycle.add_argument(
        "--experiment-policy", required=True,
        choices=("OBSERVE_ONLY", "BASELINE_ONLY", "ENHANCED_ONLY", "BOTH_SEPARATE_PAPER_BOOKS", "SHADOW_ENHANCED"),
    )
    p_pb_run_cycle.add_argument("--provider-mode", choices=("fixture",), default="fixture")
    p_pb_run_cycle.add_argument("--symbol", required=True)
    p_pb_run_cycle.add_argument("--quantity-hint", required=True, help="Decimal string")
    p_pb_run_cycle.add_argument("--reference-price", required=True, help="Decimal string")
    p_pb_run_cycle.add_argument("--bid", required=True, help="Decimal string market-simulation input")
    p_pb_run_cycle.add_argument("--ask", required=True, help="Decimal string market-simulation input")
    p_pb_run_cycle.add_argument("--recommendation-id-baseline")
    p_pb_run_cycle.add_argument("--recommendation-id-enhanced")

    p_pb_reconcile = sub.add_parser("paper-book-reconcile", help="Reconcile one isolated paper book against its own fills/cash/positions (Milestone 8)")
    p_pb_reconcile.add_argument("--book-id", required=True)
    p_pb_reconcile.add_argument("--as-of", help="ISO8601 timestamp (default: now)")

    p_pb_compare = sub.add_parser("paper-experiment-compare", help="Compare baseline vs enhanced isolated paper books over a window (Milestone 8)")
    p_pb_compare.add_argument("--experiment-id", required=True)
    p_pb_compare.add_argument("--window-start", required=True, help="ISO8601 timestamp")
    p_pb_compare.add_argument("--window-end", required=True, help="ISO8601 timestamp")
    p_pb_compare.add_argument("--min-comparable-cycles", type=int, default=1)

    p_pb_promotion = sub.add_parser("paper-promotion-status", help="Evidence-only promotion status from the latest comparison (Milestone 8) — never a live-trading status")
    p_pb_promotion.add_argument("--experiment-id", required=True)
    p_pb_promotion.add_argument("--min-comparable-cycles", type=int, default=1)
    p_pb_promotion.add_argument("--min-trading-days", type=int, default=1)
    p_pb_promotion.add_argument("--min-closed-trades", type=int, default=1)

    p_pb_integrate = sub.add_parser(
        "paper-book-integrate-cycle",
        help="Drive an ACTUAL persisted scheduled-research cycle through the isolated paper books (Milestone 8.1) — manual, non-recurring",
    )
    p_pb_integrate.add_argument("--cycle-id", required=True)
    p_pb_integrate.add_argument(
        "--experiment-policy", default="BOTH_SEPARATE_PAPER_BOOKS",
        choices=("OBSERVE_ONLY", "BASELINE_ONLY", "ENHANCED_ONLY", "BOTH_SEPARATE_PAPER_BOOKS", "SHADOW_ENHANCED"),
    )

    p_pb_lifecycle_run = sub.add_parser(
        "paper-book-lifecycle-run",
        help="Manually process pending orders, evaluate exits, snapshot, and reconcile both books for one lifecycle date (Milestone 9)",
    )
    p_pb_lifecycle_run.add_argument("--as-of", required=True, help="ISO8601 timestamp")
    p_pb_lifecycle_run.add_argument("--integrate-cycle-id", action="append", default=[], dest="integrate_cycle_ids")
    p_pb_lifecycle_run.add_argument(
        "--audit-time-now", action="store_true",
        help="Stamp real wall-clock time as created_at audit metadata (Milestone 9.1) — never changes "
             "market-day calculations, order eligibility, price selection, holding-period calculation, "
             "snapshot as_of, or exit-decision effective date, which remain keyed to --as-of regardless",
    )

    p_pb_exit_request = sub.add_parser(
        "paper-book-exit-request", help="Create an explicit, audited manual exit request for one book/symbol (Milestone 9)"
    )
    p_pb_exit_request.add_argument("--book-id", required=True, choices=("BASELINE", "ENHANCED"))
    p_pb_exit_request.add_argument("--symbol", required=True)
    p_pb_exit_request.add_argument("--operator", required=True)
    p_pb_exit_request.add_argument("--reason", required=True)

    p_pb_soak_report = sub.add_parser(
        "paper-book-soak-report", help="Read-only daily soak report for both isolated paper books (Milestone 9)"
    )
    p_pb_soak_report.add_argument("--as-of", required=True, help="ISO8601 timestamp")

    p_pb_soak_readiness = sub.add_parser(
        "paper-book-soak-readiness", help="Deterministic, advisory-only soak-readiness result (Milestone 9) — never activates anything"
    )
    p_pb_soak_readiness.add_argument("--as-of", required=True, help="ISO8601 timestamp")

    p_pb_soak_run = sub.add_parser(
        "paper-soak-run",
        help="Single manual operator command: validate, optionally integrate cycles, run lifecycle, report, "
             "and evaluate combined controlled-soak readiness for one date (Milestone 9.1) — never recurring",
    )
    p_pb_soak_run.add_argument("--as-of", required=True, help="ISO8601 timestamp")
    p_pb_soak_run.add_argument("--integrate-cycle-id", action="append", default=[], dest="integrate_cycle_ids")
    p_pb_soak_run.add_argument(
        "--audit-time-now", action="store_true",
        help="Stamp real wall-clock time as created_at audit metadata — never changes effective market time",
    )

    p_pb_soak_readiness_combined = sub.add_parser(
        "paper-soak-readiness",
        help="Combined paper-soak + shadow-operational activation readiness (Milestone 9.1) — advisory-only, "
             "never activates or schedules anything",
    )
    p_pb_soak_readiness_combined.add_argument("--as-of", required=True, help="ISO8601 timestamp")

    p_pb_cross_check = sub.add_parser(
        "paper-book-cross-check",
        help="Read-only authoritative cross-book isolation verification (Milestone 9.2) — deterministic, no network call",
    )
    p_pb_cross_check.add_argument("--as-of", required=True, help="ISO8601 timestamp")
    p_pb_cross_check.add_argument("--operator-run-id", default=None)
    p_pb_cross_check.add_argument("--lifecycle-run-id", default=None)

    p_campaign_validate = sub.add_parser(
        "paper-soak-campaign-validate", help="Validate a bounded explicit soak-campaign JSON manifest"
    )
    p_campaign_validate.add_argument("--manifest", required=True, type=Path)
    p_campaign_run = sub.add_parser(
        "paper-soak-campaign-run", help="Run a disabled-by-default manual multi-day paper-soak campaign"
    )
    p_campaign_run.add_argument("--manifest", required=True, type=Path)
    p_campaign_run.add_argument(
        "--continue-on-blocker", action="store_true",
        help="Create an explicit continuation attempt after remediation",
    )
    p_campaign_run.add_argument("--operator")
    p_campaign_run.add_argument("--reason")
    p_campaign_resume = sub.add_parser(
        "paper-soak-campaign-resume", help="Resume an incomplete campaign or create a continuation attempt"
    )
    p_campaign_resume.add_argument("--campaign-id", required=True)
    p_campaign_resume.add_argument("--operator", required=True)
    p_campaign_resume.add_argument("--reason", required=True)
    p_campaign_show = sub.add_parser("paper-soak-campaign-show", help="Show a persisted soak campaign")
    p_campaign_show.add_argument("--campaign-id", required=True)
    p_activation_review = sub.add_parser(
        "paper-soak-activation-review", help="Show the persisted advisory-only activation review"
    )
    p_activation_review.add_argument("--campaign-id", required=True)
    p_activation_review.add_argument("--attempt-id")

    p_recurring_request = sub.add_parser(
        "paper-recurring-request-activation", help="Request audited local recurring-paper activation"
    )
    p_recurring_request.add_argument("--activation-review-id", required=True)
    p_recurring_request.add_argument("--operator", required=True)
    p_recurring_request.add_argument("--reason", required=True)

    p_recurring_activate = sub.add_parser(
        "paper-recurring-activate", help="Explicitly approve a current recurring-paper activation request"
    )
    p_recurring_activate.add_argument("--request-event-id", required=True)
    p_recurring_activate.add_argument("--operator", required=True)

    p_recurring_deactivate = sub.add_parser("paper-recurring-deactivate", help="Deactivate recurring local paper execution")
    p_recurring_deactivate.add_argument("--operator", required=True)
    p_recurring_deactivate.add_argument("--reason", required=True)

    p_recurring_enqueue = sub.add_parser(
        "paper-recurring-enqueue-cycle", help="Explicitly enqueue one completed frozen research cycle"
    )
    p_recurring_enqueue.add_argument("--cycle-id", required=True)
    p_recurring_enqueue.add_argument("--operator", required=True)
    p_recurring_enqueue.add_argument("--reason", required=True)

    p_recurring_cancel = sub.add_parser("paper-recurring-cancel-cycle", help="Cancel one queued recurring-paper cycle")
    p_recurring_cancel.add_argument("--queue-item-id", required=True)
    p_recurring_cancel.add_argument("--operator", required=True)
    p_recurring_cancel.add_argument("--reason", required=True)

    p_recurring_run = sub.add_parser(
        "paper-recurring-run-once", help="Invoke at most one due recurring local-paper slot"
    )
    p_recurring_run.add_argument("--now", required=True, help="timezone-aware ISO8601 timestamp")
    p_recurring_run.add_argument("--owner-id", required=True)

    sub.add_parser("paper-recurring-status", help="Show recurring configuration, activation, queue, lease, and runs")
    p_recurring_queue = sub.add_parser("paper-recurring-queue-list", help="List explicit recurring cycle queue items")
    p_recurring_queue.add_argument(
        "--status", choices=("QUEUED", "CLAIMED", "PROCESSED", "FAILED", "CANCELLED"), default=None,
    )

    p_external_account = sub.add_parser("external-paper-account-check", help="Verify the isolated Alpaca paper account")
    p_external_account.add_argument("--book-id", required=True, choices=("BASELINE", "ENHANCED"))
    p_external_preview = sub.add_parser("external-paper-preview", help="Persist an explicit external-paper preview")
    p_external_preview.add_argument("--book-id", required=True, choices=("BASELINE", "ENHANCED"))
    p_external_preview.add_argument("--intent-id", required=True)
    p_external_preview.add_argument("--operator", required=True)
    p_external_submit = sub.add_parser("external-paper-submit", help="Explicitly submit a recently previewed limit order")
    p_external_submit.add_argument("--book-id", required=True, choices=("BASELINE", "ENHANCED"))
    p_external_submit.add_argument("--intent-id", required=True)
    p_external_submit.add_argument("--preview-id", required=True)
    p_external_submit.add_argument("--operator", required=True)
    p_external_submit.add_argument("--reason", required=True)
    p_external_show = sub.add_parser("external-paper-order-show", help="Show bounded local external-order evidence")
    p_external_show.add_argument("--book-id", required=True, choices=("BASELINE", "ENHANCED"))
    p_external_show.add_argument("--client-order-id", required=True)
    p_external_reconcile = sub.add_parser("external-paper-reconcile", help="Reconcile external broker and book state")
    p_external_reconcile.add_argument("--book-id", required=True, choices=("BASELINE", "ENHANCED"))
    p_external_reconcile.add_argument("--client-order-id")
    p_external_cancel = sub.add_parser("external-paper-cancel", help="Explicitly cancel an external paper order")
    p_external_cancel.add_argument("--book-id", required=True, choices=("BASELINE", "ENHANCED"))
    p_external_cancel.add_argument("--client-order-id", required=True)
    p_external_cancel.add_argument("--operator", required=True)
    p_external_cancel.add_argument("--reason", required=True)
    p_external_retry = sub.add_parser("external-paper-retry-submit", help="Retry only after authoritative broker NOT_FOUND")
    p_external_retry.add_argument("--book-id", required=True, choices=("BASELINE", "ENHANCED"))
    p_external_retry.add_argument("--intent-id", required=True)
    p_external_retry.add_argument("--operator", required=True)
    p_external_retry.add_argument("--reason", required=True)
    p_external_refresh = sub.add_parser(
        "external-paper-refresh-retry-preview",
        help="Refresh an expired preview for an order confirmed UNKNOWN_REQUIRES_RECONCILIATION (read-only, no broker call)",
    )
    p_external_refresh.add_argument("--book-id", required=True, choices=("BASELINE", "ENHANCED"))
    p_external_refresh.add_argument("--intent-id", required=True)
    p_external_refresh.add_argument("--operator", required=True)
    p_external_refresh.add_argument("--reason", required=True)
    p_external_queue = sub.add_parser(
        "external-paper-queue-show", help="Show the external submission queue's derived status (read-only)",
    )
    p_external_queue.add_argument("--book-id", required=True, choices=("BASELINE", "ENHANCED"))

    args = parser.parse_args(argv)

    if args.command == "analyze":
        if args.provider_mode == "reddit_free" and default_universe().is_valid(args.ticker.upper()):
            from .evidence_providers.config import load_evidence_provider_config
            from .evidence_providers.reddit_free import RedditFreeProvider

            cfg = load_config()
            provider_config = load_evidence_provider_config().reddit_free
            with session(cfg.research_database_path) as conn:
                with RedditFreeProvider(provider_config, conn=conn, data_dir=cfg.research_data_dir) as provider:
                    reddit_result = provider.fetch(args.ticker, datetime.now(timezone.utc))
            rec = analyze(
                args.ticker,
                reddit_records=reddit_result.records,
                reddit_net_sentiment=reddit_result.net_sentiment,
                reddit_missing_data_reasons=reddit_result.missing_data_reasons,
                reddit_source_label="reddit-free-rss",
            )
        else:
            rec = analyze(args.ticker)
        validator = _load_schema()
        errors = sorted(validator.iter_errors(rec), key=lambda e: e.json_path)
        if errors:
            for e in errors:
                print(f"SCHEMA ERROR at {e.json_path}: {e.message}", file=sys.stderr)
            return 2
        print(json.dumps(rec, indent=2))
        return 0

    if args.command == "legacy-paper-status":
        cfg = load_config()
        print(json.dumps(paper_status(cfg.research_database_path), indent=2, default=str))
        return 0

    if args.command == "legacy-paper-execute":
        cfg = load_config()
        outcome = execute_paper(args.recommendation_id, cfg.research_database_path, adapter=args.adapter)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-runtime-health":
        outcome = paper_runtime_health()
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if outcome.get("available") else 2

    if args.command == "legacy-paper-sync-orders":
        cfg = load_config()
        outcome = sync_paper_orders_cli(cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "legacy-paper-reconcile":
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

    if args.command == "build-evidence":
        cfg = load_config()
        outcome = build_evidence_cli(args.symbol, args.as_of, cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "run-research":
        cfg = load_config()
        outcome = run_research_cli(args.snapshot_id, args.provider, cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        if "error" in outcome:
            return 2
        return 0 if outcome["status"] == "COMPLETED" else 2

    if args.command == "replay-research":
        cfg = load_config()
        outcome = replay_research_cli(args.research_run_id, cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        if "error" in outcome:
            return 2
        return 0 if outcome["matches"] else 2

    if args.command == "compare-research-arms":
        cfg = load_config()
        outcome = compare_research_arms_cli(args.experiment_id, cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    if args.command == "research-performance":
        cfg = load_config()
        outcome = research_performance_cli(cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    if args.command == "research-usage":
        cfg = load_config()
        outcome = research_usage_cli(cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    if args.command == "claude-code-provider-preflight":
        outcome = claude_code_provider_preflight_cli(args.research_config)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if outcome.get("ready") else 2

    if args.command == "research-failures":
        cfg = load_config()
        outcome = research_failures_cli(
            args.research_run_id, cfg.research_database_path, role=args.role,
            attempt_number=args.attempt_number, stage=args.stage, code=args.code,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "research-failure-metrics":
        cfg = load_config()
        outcome = research_failure_metrics_cli(cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    if args.command == "provider-health":
        cfg = load_config()
        outcome = provider_health_cli(cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    if args.command == "fetch-evidence":
        cfg = load_config()
        outcome = fetch_evidence_cli(args.symbol, args.as_of, cfg.research_database_path, args.provider_mode)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "run-research-cycle":
        cfg = load_config()
        outcome = run_research_cycle_cli(args.as_of, cfg.research_database_path, args.provider_mode, args.symbols)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "resume-research-cycle":
        cfg = load_config()
        outcome = resume_research_cycle_cli(args.cycle_id, cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "evaluate-research-cycle":
        cfg = load_config()
        outcome = evaluate_research_cycle_cli(args.cycle_id, cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "compare-research-cycles":
        cfg = load_config()
        outcome = compare_research_cycles_cli(cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    if args.command == "research-promotion-status":
        cfg = load_config()
        outcome = research_promotion_status_cli(args.experiment_id, cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "evidence-provider-usage":
        cfg = load_config()
        outcome = evidence_provider_usage_cli(cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    if args.command == "run-due-shadow-cycle":
        cfg = load_config()
        outcome = run_due_shadow_cycle_cli(
            cfg.research_database_path,
            provider_mode=args.provider_mode,
            symbols=args.symbols,
            research_config_path=args.research_config,
            scheduled_research_config_path=args.scheduled_research_config,
            shadow_config_path=args.shadow_config,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "shadow-status":
        cfg = load_config()
        outcome = shadow_status_cli(cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    if args.command == "shadow-readiness":
        cfg = load_config()
        outcome = shadow_readiness_cli(
            cfg.research_database_path,
            research_config_path=args.research_config,
            shadow_config_path=args.shadow_config,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    if args.command == "shadow-run-history":
        cfg = load_config()
        outcome = shadow_run_history_cli(cfg.research_database_path, status=args.status, limit=args.limit)
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    if args.command == "shadow-budget-status":
        cfg = load_config()
        outcome = shadow_budget_status_cli(cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    if args.command == "shadow-alerts":
        cfg = load_config()
        outcome = shadow_alerts_cli(cfg.research_database_path, severity=args.severity, limit=args.limit)
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    if args.command == "shadow-alert-list":
        cfg = load_config()
        outcome = shadow_alert_list_cli(
            cfg.research_database_path, severity=args.severity, unresolved_only=args.unresolved_only, limit=args.limit,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    if args.command == "shadow-alert-resolve":
        cfg = load_config()
        outcome = shadow_alert_resolve_cli(
            cfg.research_database_path, alert_id=args.alert_id, operator=args.operator, reason=args.reason,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "shadow-pause":
        cfg = load_config()
        outcome = shadow_pause_cli(cfg.research_database_path, args.reason)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "shadow-resume":
        cfg = load_config()
        outcome = shadow_resume_cli(cfg.research_database_path, args.reason, args.operator)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "shadow-kill":
        cfg = load_config()
        outcome = shadow_kill_cli(cfg.research_database_path, args.reason, args.operator)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "shadow-force-clear-kill":
        cfg = load_config()
        outcome = shadow_force_clear_kill_cli(cfg.research_database_path, args.reason, args.operator)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "shadow-lease-status":
        cfg = load_config()
        outcome = shadow_lease_status_cli(cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    if args.command == "shadow-health-explain":
        cfg = load_config()
        outcome = shadow_health_explain_cli(
            cfg.research_database_path, scheduler_run_id=args.scheduler_run_id, cycle_id=args.cycle_id,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "corporate-status":
        cfg = load_config()
        outcome = corporate_status_cli(args.symbol, args.as_of, cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "retention-plan":
        outcome = retention_plan_cli()
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    if args.command == "retention-apply":
        cfg = load_config()
        try:
            outcome = retention_apply_cli(cfg.research_database_path, dry_run=args.dry_run)
        except NotImplementedError as exc:
            print(json.dumps({"error": str(exc)}, indent=2))
            return 2
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    if args.command == "paper-book-list":
        from .paper_books.cli_support import paper_book_list_cli

        cfg = load_config()
        outcome = paper_book_list_cli(cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-book-show":
        from .paper_books.cli_support import paper_book_show_cli

        cfg = load_config()
        outcome = paper_book_show_cli(cfg.research_database_path, args.book_id)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-book-snapshot":
        from .paper_books.cli_support import paper_book_snapshot_cli

        cfg = load_config()
        outcome = paper_book_snapshot_cli(cfg.research_database_path, args.book_id, _parse_iso_datetime(args.as_of))
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-book-run-cycle":
        from .paper_books.cli_support import paper_book_run_cycle_cli

        cfg = load_config()
        outcome = paper_book_run_cycle_cli(
            cfg.research_database_path, cycle_id=args.cycle_id, experiment_policy=args.experiment_policy,
            symbol=args.symbol, quantity_hint=args.quantity_hint, reference_price=args.reference_price,
            bid=args.bid, ask=args.ask, recommendation_id_baseline=args.recommendation_id_baseline,
            recommendation_id_enhanced=args.recommendation_id_enhanced,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-book-reconcile":
        from .paper_books.cli_support import paper_book_reconcile_cli

        cfg = load_config()
        as_of = _parse_iso_datetime(args.as_of) if args.as_of else None
        outcome = paper_book_reconcile_cli(cfg.research_database_path, args.book_id, as_of)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-experiment-compare":
        from .paper_books.cli_support import paper_experiment_compare_cli

        cfg = load_config()
        outcome = paper_experiment_compare_cli(
            cfg.research_database_path, experiment_id=args.experiment_id,
            window_start=_parse_iso_datetime(args.window_start), window_end=_parse_iso_datetime(args.window_end),
            min_comparable_cycles=args.min_comparable_cycles,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-promotion-status":
        from .paper_books.cli_support import paper_promotion_status_cli

        cfg = load_config()
        outcome = paper_promotion_status_cli(
            cfg.research_database_path, experiment_id=args.experiment_id,
            min_comparable_cycles=args.min_comparable_cycles, min_trading_days=args.min_trading_days,
            min_closed_trades=args.min_closed_trades,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-book-integrate-cycle":
        from .paper_books.cli_support import paper_book_integrate_cycle_cli

        cfg = load_config()
        outcome = paper_book_integrate_cycle_cli(
            cfg.research_database_path, cycle_id=args.cycle_id, experiment_policy=args.experiment_policy,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-book-lifecycle-run":
        from .paper_books.cli_support import paper_book_lifecycle_run_cli

        cfg = load_config()
        outcome = paper_book_lifecycle_run_cli(
            cfg.research_database_path, as_of=_parse_iso_datetime(args.as_of),
            integrate_cycle_ids=tuple(args.integrate_cycle_ids), audit_time_now=args.audit_time_now,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-book-exit-request":
        from .paper_books.cli_support import paper_book_exit_request_cli

        cfg = load_config()
        outcome = paper_book_exit_request_cli(
            cfg.research_database_path, book_id=args.book_id, symbol=args.symbol, operator=args.operator,
            reason=args.reason,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-book-soak-report":
        from .paper_books.cli_support import paper_book_soak_report_cli

        cfg = load_config()
        outcome = paper_book_soak_report_cli(cfg.research_database_path, as_of=_parse_iso_datetime(args.as_of))
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-book-soak-readiness":
        from .paper_books.cli_support import paper_book_soak_readiness_cli

        cfg = load_config()
        outcome = paper_book_soak_readiness_cli(cfg.research_database_path, as_of=_parse_iso_datetime(args.as_of))
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-soak-run":
        from .paper_books.cli_support import paper_soak_run_cli

        cfg = load_config()
        outcome = paper_soak_run_cli(
            cfg.research_database_path, as_of=_parse_iso_datetime(args.as_of),
            integrate_cycle_ids=tuple(args.integrate_cycle_ids), audit_time_now=args.audit_time_now,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-soak-readiness":
        from .paper_books.cli_support import paper_soak_readiness_cli

        cfg = load_config()
        outcome = paper_soak_readiness_cli(cfg.research_database_path, as_of=_parse_iso_datetime(args.as_of))
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-book-cross-check":
        from .paper_books.cli_support import paper_book_cross_check_cli

        cfg = load_config()
        outcome = paper_book_cross_check_cli(
            cfg.research_database_path, as_of=_parse_iso_datetime(args.as_of),
            operator_run_id=args.operator_run_id, lifecycle_run_id=args.lifecycle_run_id,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-soak-campaign-validate":
        from .paper_books.cli_support import paper_soak_campaign_validate_cli
        outcome = paper_soak_campaign_validate_cli(args.manifest)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-soak-campaign-run":
        from .paper_books.cli_support import paper_soak_campaign_run_cli
        cfg = load_config()
        outcome = paper_soak_campaign_run_cli(
            cfg.research_database_path, manifest_path=args.manifest,
            continue_on_blocker=args.continue_on_blocker, operator=args.operator, reason=args.reason,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-soak-campaign-resume":
        from .paper_books.cli_support import paper_soak_campaign_resume_cli
        cfg = load_config()
        outcome = paper_soak_campaign_resume_cli(
            cfg.research_database_path, campaign_id=args.campaign_id,
            operator=args.operator, reason=args.reason,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-soak-campaign-show":
        from .paper_books.cli_support import paper_soak_campaign_show_cli
        cfg = load_config()
        outcome = paper_soak_campaign_show_cli(cfg.research_database_path, campaign_id=args.campaign_id)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-soak-activation-review":
        from .paper_books.cli_support import paper_soak_activation_review_cli
        cfg = load_config()
        outcome = paper_soak_activation_review_cli(
            cfg.research_database_path, campaign_id=args.campaign_id, attempt_id=args.attempt_id,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-recurring-request-activation":
        from .paper_books.cli_support import paper_recurring_request_activation_cli
        cfg = load_config()
        outcome = paper_recurring_request_activation_cli(
            cfg.research_database_path, activation_review_id=args.activation_review_id,
            operator=args.operator, reason=args.reason,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-recurring-activate":
        from .paper_books.cli_support import paper_recurring_activate_cli
        cfg = load_config()
        outcome = paper_recurring_activate_cli(
            cfg.research_database_path, request_event_id=args.request_event_id, operator=args.operator,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-recurring-deactivate":
        from .paper_books.cli_support import paper_recurring_deactivate_cli
        cfg = load_config()
        outcome = paper_recurring_deactivate_cli(
            cfg.research_database_path, operator=args.operator, reason=args.reason,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-recurring-enqueue-cycle":
        from .paper_books.cli_support import paper_recurring_enqueue_cycle_cli
        cfg = load_config()
        outcome = paper_recurring_enqueue_cycle_cli(
            cfg.research_database_path, cycle_id=args.cycle_id, operator=args.operator, reason=args.reason,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-recurring-cancel-cycle":
        from .paper_books.cli_support import paper_recurring_cancel_cycle_cli
        cfg = load_config()
        outcome = paper_recurring_cancel_cycle_cli(
            cfg.research_database_path, queue_item_id=args.queue_item_id,
            operator=args.operator, reason=args.reason,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-recurring-run-once":
        from .paper_books.cli_support import paper_recurring_run_once_cli
        cfg = load_config()
        outcome = paper_recurring_run_once_cli(
            cfg.research_database_path, now=_parse_iso_datetime(args.now), owner_id=args.owner_id,
        )
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-recurring-status":
        from .paper_books.cli_support import paper_recurring_status_cli
        cfg = load_config()
        outcome = paper_recurring_status_cli(cfg.research_database_path)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "paper-recurring-queue-list":
        from .paper_books.cli_support import paper_recurring_queue_list_cli
        cfg = load_config()
        outcome = paper_recurring_queue_list_cli(cfg.research_database_path, status=args.status)
        print(json.dumps(outcome, indent=2, default=str))
        return 0 if "error" not in outcome else 2

    if args.command == "external-paper-account-check":
        cfg = load_config()
        outcome = external_paper_account_check_cli(cfg.research_database_path, book_id=args.book_id)
    elif args.command == "external-paper-preview":
        cfg = load_config()
        outcome = external_paper_preview_cli(
            cfg.research_database_path, book_id=args.book_id, intent_id=args.intent_id, operator=args.operator,
        )
    elif args.command == "external-paper-submit":
        cfg = load_config()
        outcome = external_paper_submit_cli(
            cfg.research_database_path, book_id=args.book_id, intent_id=args.intent_id,
            preview_id=args.preview_id, operator=args.operator, reason=args.reason,
        )
    elif args.command == "external-paper-order-show":
        cfg = load_config()
        outcome = external_paper_order_show_cli(
            cfg.research_database_path, book_id=args.book_id, client_order_id=args.client_order_id,
        )
    elif args.command == "external-paper-reconcile":
        cfg = load_config()
        outcome = external_paper_reconcile_cli(
            cfg.research_database_path, book_id=args.book_id, client_order_id=args.client_order_id,
        )
    elif args.command == "external-paper-cancel":
        cfg = load_config()
        outcome = external_paper_cancel_cli(
            cfg.research_database_path, book_id=args.book_id, client_order_id=args.client_order_id,
            operator=args.operator, reason=args.reason,
        )
    elif args.command == "external-paper-retry-submit":
        cfg = load_config()
        outcome = external_paper_retry_cli(
            cfg.research_database_path, book_id=args.book_id, intent_id=args.intent_id,
            operator=args.operator, reason=args.reason,
        )
    elif args.command == "external-paper-refresh-retry-preview":
        cfg = load_config()
        outcome = external_paper_refresh_retry_preview_cli(
            cfg.research_database_path, book_id=args.book_id, intent_id=args.intent_id,
            operator=args.operator, reason=args.reason,
        )
    elif args.command == "external-paper-queue-show":
        cfg = load_config()
        outcome = external_paper_queue_show_cli(cfg.research_database_path, book_id=args.book_id)
    else:
        return 1
    print(json.dumps(outcome, indent=2, default=str))
    return 0 if "error" not in outcome else 2

if __name__ == "__main__":
    raise SystemExit(main())
