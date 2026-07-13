"""Opt-in real Claude API structured-output smoke test (docs/milestone-5.md
Step 21).

Skipped by default. Requires ALL of:
  * RUN_CLAUDE_RESEARCH_TESTS=true
  * a real ANTHROPIC_API_KEY
  * research.model configured (config/research.yaml or RESEARCH_MODEL override)

Never runs automatically just because credentials happen to be present —
that is exactly what `RUN_CLAUDE_RESEARCH_TESTS` guards against. This test
uses a fixture evidence snapshot (not current market data), invokes exactly
one research role, and never touches execution, the paper ledger, or
`real_orders`. It does not evaluate investment quality and does not claim
or simulate a real trade.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.claude_api

_RUN_FLAG = os.environ.get("RUN_CLAUDE_RESEARCH_TESTS", "").strip().lower() == "true"
_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
_MODEL = os.environ.get("RESEARCH_MODEL") or os.environ.get("ANTHROPIC_MODEL")

_SKIP_REASON = (
    "opt-in real Claude API test: set RUN_CLAUDE_RESEARCH_TESTS=true, ANTHROPIC_API_KEY, "
    "and RESEARCH_MODEL (or ANTHROPIC_MODEL) to run it"
)

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


@pytest.mark.skipif(not (_RUN_FLAG and _API_KEY and _MODEL), reason=_SKIP_REASON)
def test_real_claude_structured_role_output():
    from trading_research.research.anthropic_provider import AnthropicProviderConfig, AnthropicResearchProvider
    from trading_research.research.fixtures import build_fixture_snapshot
    from trading_research.research.output_validation import role_report_json_schema
    from trading_research.research.claim_validation import validate_role_report
    from trading_research.research.output_validation import build_role_report
    from trading_research.research.prompt_registry import PromptRegistry
    from trading_research.research.prompts import build_system_prompt, build_user_prompt
    from trading_research.research.models import ResearchModelRequest

    # 1. Small immutable fixture evidence snapshot — not current market data.
    snapshot = build_fixture_snapshot(
        "AAPL", NOW, config_hash="c" * 64, git_sha="smoke-test", clock=lambda: NOW,
    )

    # 2. No broker/execution tool is constructed anywhere in this test —
    # structurally confirmed by not importing execution/, paper/, or runtime/.
    import sys

    assert "trading_research.execution" not in sys.modules
    assert "trading_research.paper" not in sys.modules

    registry = PromptRegistry()
    prompt_def = registry.get("fundamental")
    schema = role_report_json_schema()
    request = ResearchModelRequest(
        role="fundamental", research_run_id="smoke-test-run", snapshot=snapshot,
        system_prompt=build_system_prompt(prompt_def),
        user_prompt=build_user_prompt(snapshot, json_schema=schema, max_input_characters=100_000),
        json_schema=schema, model_name=_MODEL, max_output_tokens=4000, temperature=0.0,
        prompt_name=prompt_def.name, prompt_version=prompt_def.version, prompt_hash=prompt_def.text_hash,
        system_prompt_hash=registry.system_prompt_hash(), schema_version=prompt_def.schema_version,
        attempt_number=1,
    )

    provider = AnthropicResearchProvider(AnthropicProviderConfig(api_key=_API_KEY, request_timeout_seconds=60))

    # 3. Invoke exactly one research role.
    response = provider.generate_structured(request)

    # 4. Valid structured output.
    report = build_role_report(
        dict(response.parsed_json), report_id="smoke-test-report", research_run_id="smoke-test-run",
        role="fundamental", symbol=snapshot.symbol, snapshot_id=snapshot.snapshot_id,
        model_name=_MODEL, prompt_version=prompt_def.version, schema=schema,
    )
    assert report.stance in ("BULLISH", "BEARISH", "NEUTRAL", "ANALYSIS_INCOMPLETE")

    # 5. Validate every cited evidence ID against the exact snapshot used.
    #
    # A rejected claim is not automatically a test failure here: it can mean
    # the validator correctly caught a real model producing an unsupported
    # numeric claim (e.g. an arithmetic derivation not present verbatim in
    # any cited evidence's normalized_values) — that is the validator doing
    # its job on live, non-deterministic output, not a defect. What this
    # smoke test hard-fails on is the more severe case Step 21 explicitly
    # asks it to check: a citation of an evidence_id that does not exist in
    # the snapshot at all (an unknown/fabricated citation).
    validation = validate_role_report(report, snapshot)
    for claim, reasons in validation.rejected_claims:
        fabricated = [r for r in reasons if "unknown evidence_id" in r]
        if fabricated:
            pytest.fail(f"claim {claim.claim_id} cited a fabricated evidence_id: {fabricated}")
        print(f"claim {claim.claim_id} rejected by claim validation (expected mechanism, not a failure): {reasons}")

    # 6. Usage and latency were persisted on the response (not fabricated).
    assert response.usage.provider == "anthropic"
    assert response.usage.latency_ms is not None
    print(
        f"Claude smoke test usage: input_tokens={response.usage.input_tokens} "
        f"output_tokens={response.usage.output_tokens} latency_ms={response.usage.latency_ms} "
        f"cost_status={response.usage.cost_status}"
    )

    # 7. No recommendation, order, or ledger mutation happened — no
    # persistence call of any kind was made in this test.
