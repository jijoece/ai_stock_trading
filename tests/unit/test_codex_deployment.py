"""Codex deployment/config-profile and architectural safety tests (Milestone 12)."""
from __future__ import annotations

import ast
from pathlib import Path

from trading_research.research.configuration import load_research_config
from trading_research.research.scheduled_research_config import load_scheduled_research_config
from trading_research.shadow.config import load_shadow_operations_config
from trading_research.storage.database import connect

REPO_ROOT = Path(__file__).resolve().parents[2]

_FORBIDDEN_IMPORT_PREFIXES = (
    "trading_research.paper_books", "trading_research.runtime", "paper_books", "runtime",
)


def _imported_module_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_codex_provider_never_imports_broker_or_execution_modules():
    for filename in ("codex_provider.py", "codex_jsonl_adapter.py", "bounded_subprocess.py"):
        source = (REPO_ROOT / "src/trading_research/research" / filename).read_text()
        imported = _imported_module_names(source)
        for module in imported:
            for prefix in _FORBIDDEN_IMPORT_PREFIXES:
                assert not module.startswith(prefix), f"{filename} imports forbidden module {module!r}"


def test_codex_provider_never_uses_openai_api_key_or_dangerous_flags():
    """`OPENAI_API_KEY` is never read as a credential: it is absent from the
    environment allowlist (`_ENV_ALLOWLIST`), so a value present in the
    parent process's environment can never reach the Codex subprocess."""
    from trading_research.research import codex_provider as mod

    assert "OPENAI_API_KEY" not in mod._ENV_ALLOWLIST
    assert "OPENAI_API_KEY" not in mod.MINIMAL_PATH

    source = (REPO_ROOT / "src/trading_research/research/codex_provider.py").read_text()
    for flag in (
        "--yolo", "--dangerously-bypass-approvals-and-sandbox", "workspace-write", "danger-full-access",
        "--add-dir",
    ):
        assert flag not in source


def test_dormant_codex_production_profile_enables_research_but_never_submission():
    root = REPO_ROOT / "config/production"
    research = load_research_config(root / "research-codex.yaml")
    scheduled = load_scheduled_research_config(root / "scheduled_research.yaml")
    shadow = load_shadow_operations_config(root / "shadow_operations.yaml")
    assert research.enabled is True
    assert research.provider == "codex"
    assert research.model  # explicit model required
    assert research.codex is not None
    assert research.allow_parallel_roles is False
    assert scheduled.submit_paper_orders is False
    assert scheduled.promotion_enabled is False
    assert scheduled.promotion.allow_live_promotion is False
    assert shadow.shadow_operations.allow_baseline_paper_submission is False
    assert shadow.shadow_operations.allow_enhanced_submission is False


def test_dormant_codex_profile_does_not_replace_claude_code_production_profile():
    root = REPO_ROOT / "config/production"
    claude_code_research = load_research_config(root / "research.yaml")
    assert claude_code_research.provider == "claude_code"
    assert (root / "research-codex.yaml").exists()


def test_safe_base_profile_remains_disabled_and_does_not_default_to_codex():
    research = load_research_config()
    assert research.enabled is False
    assert research.provider != "codex"


def test_codex_requires_pricing_like_every_other_real_provider():
    from trading_research.shadow.budget import PRICING_EXEMPT_PROVIDERS, REAL_CLAUDE_PROVIDERS

    assert "codex" in REAL_CLAUDE_PROVIDERS
    assert "codex" not in PRICING_EXEMPT_PROVIDERS


def test_no_silent_fallback_from_codex_in_cli_source():
    """The scheduled-cycle real-provider dispatch in cli.py must select
    Codex/Claude Code/Anthropic explicitly and never fall through to a
    different provider on failure — verified by exact source structure
    rather than a runtime probe, since a fallback would be a code-shape
    change (a new `except`/`else` branch constructing a different
    provider), not a data-driven one."""
    source = (REPO_ROOT / "src/trading_research/cli.py").read_text()
    # The real-provider preflight `except Exception` blocks only pause
    # scheduling and return a PROVIDER_PREFLIGHT_FAILED error — they must
    # never go on to construct a different provider inside the same except.
    assert "real_research_provider = ClaudeCodeResearchProvider" in source
    assert "real_research_provider = CodexResearchProvider" in source
    assert "real_research_provider = DeterministicResearchProvider" not in source
    assert "real_research_provider = AnthropicResearchProvider" not in source


def test_provider_cli_version_column_is_migrated(tmp_path):
    conn = connect(tmp_path / "research.sqlite3")
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(research_attempts)").fetchall()}
        assert "provider_cli_version" in columns
        # Prior Claude Code provenance columns are preserved, not renamed.
        assert "claude_code_version" in columns
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version >= 4
    finally:
        conn.close()
