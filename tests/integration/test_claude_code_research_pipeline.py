from __future__ import annotations

import os
import stat
import textwrap
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from trading_research.research.claude_code_provider import ClaudeCodeProviderConfig, ClaudeCodeResearchProvider
from trading_research.research.configuration import load_research_config
from trading_research.research.fixtures import build_fixture_snapshot
from trading_research.research.orchestration import analyze_with_research_committee
from trading_research.research.prompt_registry import PromptRegistry
from trading_research.research.usage import PricingEntry
from trading_research.storage.database import connect
from trading_research.storage.research_repositories import SQLiteResearchRepository, save_evidence_snapshot


def test_fake_claude_code_flows_through_existing_validation_and_persistence(tmp_path, monkeypatch):
    binary = tmp_path / "fake-claude"
    binary.write_text("#!/usr/bin/env python3\n" + textwrap.dedent("""
        import json, sys
        args = sys.argv[1:]
        if args == ["--version"]:
            print("2.1.205")
        elif args == ["auth", "status"]:
            print(json.dumps({"loggedIn": True, "authMethod": "oauth"}))
        else:
            schema = json.loads(args[args.index("--json-schema") + 1])
            if "rating" in schema["properties"]:
                payload = {
                    "rating": "HOLD", "confidence": 0.5, "thesis": "bounded synthesis",
                    "bull_case": "bounded upside case", "bear_case": "bounded downside case",
                    "catalysts": [], "risks": ["bounded test risk"], "invalidation_conditions": [],
                    "claims": [], "evidence_ids": [], "missing_data_reasons": [],
                }
            else:
                payload = {
                    "stance": "NEUTRAL", "summary": "bounded analyst report", "claims": [],
                    "catalysts": [], "risks": ["bounded test risk"],
                    "uncertainties": ["offline fake process"], "missing_data_reasons": [],
                }
            print(json.dumps({
                "type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
                "session_id": "fake-session", "structured_output": payload,
                "usage": {"input_tokens": 100, "output_tokens": 50,
                          "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5},
                "modelUsage": {"claude-sonnet-resolved": {"inputTokens": 100, "outputTokens": 50}},
            }))
    """))
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "offline-test-value")

    pricing = (
        PricingEntry(
            provider="claude_code", model="sonnet", effective_date="2020-01-01", currency="USD",
            input_price_per_million=Decimal("3"), output_price_per_million=Decimal("15"),
            pricing_version="offline-test",
        ),
    )
    provider = ClaudeCodeResearchProvider(ClaudeCodeProviderConfig(
        binary_path=binary, minimum_version="2.1.205", request_timeout_seconds=5,
        terminate_grace_seconds=1, maximum_stdout_bytes=65536, maximum_stderr_bytes=4096,
        maximum_schema_bytes=32768, maximum_prompt_bytes=32768,
        maximum_budget_usd_per_call=Decimal("0.50"), maximum_turns=1,
        working_directory=runtime, pricing_entries=pricing, model_alias="sonnet",
    ))
    base = load_research_config()
    config = replace(base, enabled=True, provider="claude_code", model="sonnet", roles=("fundamental", "manager"))
    now = datetime.now(timezone.utc)
    snapshot = build_fixture_snapshot("AAPL", now, config_hash=config.config_hash, git_sha="test", clock=lambda: now)
    conn = connect(tmp_path / "research.sqlite3")
    try:
        save_evidence_snapshot(conn, snapshot)
        result = analyze_with_research_committee(
            snapshot, provider=provider, provider_name="claude_code", model_name="sonnet",
            prompt_registry=PromptRegistry(), research_repository=SQLiteResearchRepository(conn),
            configuration=config, clock=lambda: now, run_mode="test",
        )
        assert result.status == "COMPLETED"
        rows = conn.execute(
            "SELECT provider, cost_estimate_basis, configured_model_alias, resolved_model_name, "
            "claude_code_version, input_tokens, output_tokens FROM research_attempts ORDER BY role"
        ).fetchall()
        assert len(rows) == 2
        assert all(row[0] == "claude_code" for row in rows)
        assert all(row[1] == "SUBSCRIPTION_API_EQUIVALENT_ESTIMATE" for row in rows)
        assert all(row[2] == "sonnet" and row[3] == "claude-sonnet-resolved" for row in rows)
        assert all(row[4] == "2.1.205" and row[5] == 100 and row[6] == 50 for row in rows)
        assert conn.execute("SELECT COUNT(*) FROM paper_book_orders").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM paper_external_order_events").fetchone()[0] == 0
    finally:
        conn.close()
