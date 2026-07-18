"""Unit tests for `cli.py::run_due_shadow_cycle_cli`'s explicit fixture/real
provider-mode wiring (docs/milestone-7.1.md Step 20).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from trading_research import cli as cli_mod
from trading_research.research import configuration as research_configuration_mod


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "cli_provider_mode.db"


def test_unknown_provider_mode_fails_closed(db_path):
    result = cli_mod.run_due_shadow_cycle_cli(db_path, provider_mode="bogus")
    assert result["status"] == "INTERNAL_ERROR"
    assert "provider-mode" in result["error"]


def test_fixture_mode_is_the_documented_default_and_disabled_no_op(db_path):
    """Shipped `config/shadow_operations.yaml` ships `enabled: false` — the
    fixture-mode default no-op path must still work end-to-end without any
    operator configuration."""
    result = cli_mod.run_due_shadow_cycle_cli(db_path)  # no provider_mode kwarg — default
    assert result["provider_mode"] == "fixture"
    assert result["status"] == "DISABLED"
    assert result["is_successful_no_op"] is True


def test_real_mode_without_symbols_fails_closed_before_any_session(db_path):
    result = cli_mod.run_due_shadow_cycle_cli(db_path, provider_mode="real", symbols=[])
    assert result["status"] == "INTERNAL_ERROR"
    assert "candidate symbols" in result["error"]
    assert not db_path.exists()


def test_real_mode_anthropic_missing_pricing_fails_closed(db_path, monkeypatch):
    class _Stub:
        provider = "anthropic"
        model = "claude-unknown-model"
        request_timeout_seconds = 30
        roles = ("fundamental", "manager")

        def require_ready(self):
            return None

    class _CfgStub:
        anthropic_api_key = "sk-test-present"

    monkeypatch.setattr(research_configuration_mod, "load_research_config", lambda: _Stub())
    monkeypatch.setattr(cli_mod, "load_config", lambda *a, **k: _CfgStub())
    result = cli_mod.run_due_shadow_cycle_cli(db_path, provider_mode="real", symbols=["AAPL"])
    assert result["status"] == "PRICING_NOT_CONFIGURED"
    assert not db_path.exists()


def test_real_mode_anthropic_missing_credentials_fails_closed(db_path, monkeypatch, tmp_path):
    import trading_research.research.usage as usage_mod

    class _Stub:
        provider = "anthropic"
        model = "claude-test-model"
        request_timeout_seconds = 30
        roles = ("fundamental", "manager")

        def require_ready(self):
            return None

    pricing_path = tmp_path / "pricing.yaml"
    pricing_path.write_text(
        "version: 1\npricing:\n  - provider: anthropic\n    model: claude-test-model\n"
        "    effective_date: \"2020-01-01\"\n    currency: USD\n    input_price_per_million: \"1\"\n"
        "    output_price_per_million: \"1\"\n    pricing_version: v1\n",
    )
    monkeypatch.setattr(research_configuration_mod, "load_research_config", lambda: _Stub())
    monkeypatch.setattr(usage_mod, "DEFAULT_PRICING_PATH", pricing_path)

    class _CfgStub:
        anthropic_api_key = None

    monkeypatch.setattr(cli_mod, "load_config", lambda *a, **k: _CfgStub())

    result = cli_mod.run_due_shadow_cycle_cli(db_path, provider_mode="real", symbols=["AAPL"])
    assert result["status"] == "MISSING_CREDENTIALS"
    assert not db_path.exists()


def test_no_live_trading_flag_exists_on_the_cli():
    """Structural guard: `run-due-shadow-cycle`'s argparse subparser never
    exposes a live-trading option (docs/milestone-7.1.md hard boundary)."""
    import inspect

    source = inspect.getsource(cli_mod)
    run_due_index = source.index('sub.add_parser("run-due-shadow-cycle"')
    # Look at the next ~400 chars for this subparser's own argument registrations.
    segment = source[run_due_index:run_due_index + 500]
    assert "live" not in segment.lower()
