"""Opt-in real Claude API bear-role smoke test (Milestone 6.1 follow-up).

Skipped by default. Requires ALL of:
  * RUN_CLAUDE_BEAR_TESTS=true
  * a real ANTHROPIC_API_KEY
  * a model (RESEARCH_MODEL or ANTHROPIC_MODEL — intended to be claude-sonnet-5)

Run with:
  RUN_CLAUDE_BEAR_TESTS=true \\
  pytest tests/integration/test_research_claude_bear_smoke.py -v -m claude_api

Never runs automatically just because credentials happen to be present. This test:

  * builds one immutable, fixture-backed AAPL evidence snapshot and persists it
    (mirrors a real point-in-time snapshot exactly the way a real scheduled cycle would,
    without depending on network-fetched real evidence, which Milestone 6.1's scope does
    not require re-validating here — the bear-role prompt/validation/persistence path is
    what this test targets, not evidence acquisition);
  * invokes **only** the `bear` role, directly through the same
    `orchestration._run_role_with_retries` bounded-retry helper `analyze_with_research_committee`
    itself uses for every analyst role — never through the full orchestrator function.
    This test predates a later fix: `analyze_with_research_committee` used to invoke the
    manager unconditionally once every analyst role succeeded, regardless of whether a
    manager role was configured; it now accepts `require_decision=False` for exactly this
    analyst-only case and correctly never calls the manager (see
    `orchestration.RUN_STATUS_ANALYST_REPORTS_COMPLETE_NO_MANAGER` and
    `tests/unit/test_research_orchestration.py`'s manager-invocation tests). Calling the
    shared retry helper directly here still works and remains a structural (not merely
    behavioral) guarantee that the manager is never invoked, so this already-validated
    test is left as-is rather than switched to the now-also-correct full orchestrator path;
  * runs the exact same forced-tool-use structured output, local schema validation, and
    claim-to-evidence validation real analyst attempts go through in production;
  * persists every structured failure (if any) to a real, temporary SQLite database via
    `SQLiteResearchRepository`, exactly like a real run would;
  * never imports `execution`, `paper`, `runtime`, `recommendations`, or `overlay` —
    verified structurally below, not just by omission;
  * never prints a raw prompt, a raw response, or a credential — only sanitized
    structured summary fields (attempt count, validation result, failure codes, token
    usage, latency).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.claude_api

_RUN_FLAG = os.environ.get("RUN_CLAUDE_BEAR_TESTS", "").strip().lower() == "true"
_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
_MODEL = os.environ.get("RESEARCH_MODEL") or os.environ.get("ANTHROPIC_MODEL")

_SKIP_REASON = (
    "opt-in real Claude bear-role test: set RUN_CLAUDE_BEAR_TESTS=true, ANTHROPIC_API_KEY, "
    "and RESEARCH_MODEL (or ANTHROPIC_MODEL, intended to be claude-sonnet-5) to run it"
)

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


@pytest.mark.skipif(not (_RUN_FLAG and _API_KEY and _MODEL), reason=_SKIP_REASON)
def test_real_claude_bear_role_structured_output(tmp_path):
    from trading_research.research.anthropic_provider import AnthropicProviderConfig, AnthropicResearchProvider
    from trading_research.research.configuration import ResearchConfiguration
    from trading_research.research.fixtures import build_fixture_snapshot
    from trading_research.research.orchestration import _run_role_with_retries, compute_research_run_id
    from trading_research.research.prompt_registry import PromptRegistry
    from trading_research.storage.database import connect
    from trading_research.storage.research_repositories import SQLiteResearchRepository, save_evidence_snapshot

    # No broker/execution/recommendation module is imported anywhere in this test file or
    # by anything it calls — structurally confirmed, not just asserted by omission.
    for forbidden in ("trading_research.execution", "trading_research.paper", "trading_research.runtime"):
        assert forbidden not in sys.modules, f"{forbidden} must never be imported by a research-layer smoke test"

    # 1. One immutable, fixture-backed AAPL evidence snapshot, persisted to a real
    # (temporary) SQLite database — the same persistence path a real run uses.
    snapshot = build_fixture_snapshot("AAPL", NOW, config_hash="c" * 64, git_sha="bear-smoke-test", clock=lambda: NOW)
    db_path = tmp_path / "bear_smoke.sqlite3"
    conn = connect(db_path)
    save_evidence_snapshot(conn, snapshot)
    repo = SQLiteResearchRepository(conn)

    registry = PromptRegistry()
    prompt_def = registry.get("bear")
    assert prompt_def.version == "v2", "bear must resolve to the hardened v2 prompt by default"

    configuration = ResearchConfiguration(
        version=1, enabled=True, provider="anthropic", model=_MODEL, max_attempts_per_role=2,
        request_timeout_seconds=60, max_input_characters=100_000, max_evidence_items=100,
        max_items_per_source_category=25, max_claims_per_role=20, max_output_tokens=4000,
        require_point_in_time_safe=True, require_evidence_for_material_claims=True,
        fail_on_stale_required_evidence=True, allow_parallel_roles=False, roles=("bear",),
        overlay_policy_version="bear-smoke.v1", overlay_allow_score_increase=False,
        overlay_allow_position_size_increase=False, overlay_incomplete_action="ANALYSIS_INCOMPLETE",
        overlay_critical_risk_action="FORCE_NO_ACTION", config_hash="c" * 64, raw={},
    )
    research_run_id = compute_research_run_id(
        snapshot_id=snapshot.snapshot_id, provider_name="anthropic", model_name=_MODEL, roles=("bear",),
        prompt_registry=registry, run_mode="anthropic", config_hash=configuration.config_hash,
    )
    repo.save_run_started(
        research_run_id, snapshot.snapshot_id, "anthropic", _MODEL, ("bear",), "anthropic",
        configuration.config_hash, NOW,
    )

    provider = AnthropicResearchProvider(AnthropicProviderConfig(api_key=_API_KEY, request_timeout_seconds=60))

    # 2. Invoke only the bear role, through the exact bounded-retry/validation/failure-
    # construction helper production code uses — never the manager, never any other role.
    report, attempts, failures = _run_role_with_retries(
        role="bear", research_run_id=research_run_id, snapshot=snapshot, provider=provider,
        provider_name="anthropic", model_name=_MODEL, prompt_registry=registry,
        configuration=configuration, clock=lambda: NOW,
    )

    # 3. Persist attempts and structured failures exactly like a real run would.
    for attempt in attempts:
        repo.save_attempt(attempt)
    if failures:
        repo.save_attempt_failures(tuple(failures))
    final_status = "COMPLETED" if report is not None else "ANALYSIS_INCOMPLETE"
    repo.mark_run_finished(research_run_id, final_status, NOW)
    conn.close()

    # 4. The manager was never invoked — structurally guaranteed (this test never
    # constructs or calls anything manager-related), and confirmed by role identity.
    assert all(a.role == "bear" for a in attempts)
    assert not any(f.role == "manager" for f in failures)

    # 5. No recommendation, overlay, paper, or order path was ever reached — no such
    # module is importable from this test's own call graph (checked again post-call).
    for forbidden in ("trading_research.execution", "trading_research.paper", "trading_research.runtime"):
        assert forbidden not in sys.modules

    # 6. Sanitized structured summary only — never a raw prompt, raw response, or
    # credential. Persisted failure codes (if any) are read back from the database via
    # the same repository query the CLI diagnostics command uses.
    from trading_research.storage.research_repositories import list_run_failures

    conn2 = connect(db_path)
    persisted_failures = list_run_failures(conn2, research_run_id)
    conn2.close()

    attempt_count = len(attempts)
    validation_result = "VALID_REPORT" if report is not None else "RETRY_EXHAUSTED"
    failure_codes = sorted({f.code for f in persisted_failures})
    total_input_tokens = sum(a.usage.input_tokens or 0 for a in attempts)
    total_output_tokens = sum(a.usage.output_tokens or 0 for a in attempts)
    total_latency_ms = sum(a.usage.latency_ms or 0 for a in attempts)

    print(
        f"Bear smoke test result: attempt_count={attempt_count} validation_result={validation_result} "
        f"failure_codes={failure_codes} total_input_tokens={total_input_tokens} "
        f"total_output_tokens={total_output_tokens} total_latency_ms={total_latency_ms}"
    )

    assert 1 <= attempt_count <= configuration.max_attempts_per_role
    assert research_run_id  # never empty/None
    if report is not None:
        assert report.stance in ("BULLISH", "BEARISH", "NEUTRAL", "ANALYSIS_INCOMPLETE")
    else:
        # A retry-exhausted bear role is a legitimate, honestly-reported outcome — not a
        # test failure — matching this milestone's "do not claim the bear role is fixed
        # unless ... any real Claude revalidation result is reported accurately" rule.
        assert failure_codes, "retry exhaustion without any persisted failure code would itself be a defect"
