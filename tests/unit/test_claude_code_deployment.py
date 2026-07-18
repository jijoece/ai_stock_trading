from pathlib import Path

import yaml

from trading_research.research.configuration import load_research_config
from trading_research.research.scheduled_research_config import load_scheduled_research_config
from trading_research.shadow.config import load_shadow_operations_config
from trading_research.storage.database import connect


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_launch_wrapper_is_keychain_backed_hardened_and_research_only():
    text = (REPO_ROOT / "deploy/launchd/run_shadow_cycle.sh.example").read_text()
    assert "set -euo pipefail" in text
    assert "umask 077" in text
    assert "/usr/bin/security find-generic-password" in text
    assert "agentic-trading-desk-claude-oauth" in text
    assert 'unset ANTHROPIC_API_KEY' in text
    assert 'unset ANTHROPIC_AUTH_TOKEN' in text
    assert 'SYMBOLS=(AAPL)' in text
    assert "run-due-shadow-cycle" in text
    assert "--provider-mode real" in text
    assert "--research-config" in text
    assert 'exec "$PYTHON_BIN" "${ARGS[@]}"' in text
    assert "source " not in text
    assert "eval " not in text
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in (
        REPO_ROOT / "deploy/launchd/com.agentic-trading-desk.shadow.plist.example"
    ).read_text()
    assert "KeepAlive" not in (
        REPO_ROOT / "deploy/launchd/com.agentic-trading-desk.shadow.plist.example"
    ).read_text().replace("KeepAlive is", "")


def test_explicit_production_profiles_enable_research_but_never_submission():
    root = REPO_ROOT / "config/production"
    research = load_research_config(root / "research.yaml")
    scheduled = load_scheduled_research_config(root / "scheduled_research.yaml")
    shadow = load_shadow_operations_config(root / "shadow_operations.yaml")
    assert research.enabled is True
    assert research.provider == "claude_code"
    assert research.model == "sonnet"
    assert scheduled.enabled is True
    assert scheduled.submit_paper_orders is False
    assert scheduled.promotion_enabled is False
    assert scheduled.promotion.allow_live_promotion is False
    assert shadow.shadow_operations.enabled is True
    assert shadow.schedule.enabled is True
    assert shadow.shadow_operations.allow_baseline_paper_submission is False
    assert shadow.shadow_operations.allow_enhanced_submission is False


def test_safe_base_profiles_remain_disabled():
    research = load_research_config()
    scheduled = load_scheduled_research_config()
    shadow = load_shadow_operations_config()
    assert research.enabled is False
    assert scheduled.enabled is False
    assert scheduled.submit_paper_orders is False
    assert shadow.shadow_operations.enabled is False
    assert shadow.schedule.enabled is False


def test_usage_provenance_columns_are_migrated(tmp_path):
    conn = connect(tmp_path / "research.sqlite3")
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(research_attempts)").fetchall()}
        assert {
            "cost_estimate_basis", "configured_model_alias", "resolved_model_name", "claude_code_version",
        } <= columns
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version >= 3
    finally:
        conn.close()
