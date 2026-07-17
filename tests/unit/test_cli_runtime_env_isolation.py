"""Part 11: the isolated paper runtime subprocess must receive an explicit,
minimal, allowlisted environment — never the main process's full os.environ,
and never unrelated application secrets (Anthropic/Reddit/Robinhood/database).
"""
from __future__ import annotations

import pytest

from trading_research.cli import _paper_runtime_command_env

_UNRELATED_SECRETS = (
    "ANTHROPIC_API_KEY", "REDDIT_CLIENT_SECRET", "REDDIT_MCP_AUTH_TOKEN",
    "ROBINHOOD_MCP_URL", "RESEARCH_DATABASE_PATH", "ALPACA_MARKET_DATA_API_KEY",
)
_ALLOWLISTED_SECRETS = (
    "ALPACA_API_KEY", "ALPACA_API_SECRET", "ALPACA_IS_PAPER", "ALPACA_BASE_URL",
    "PAPER_BROKER_PROVIDER", "PAPER_RUNTIME_ENV_FILE",
)


@pytest.fixture
def full_environment(monkeypatch):
    for key in _UNRELATED_SECRETS:
        monkeypatch.setenv(key, f"secret-value-for-{key}")
    for key in _ALLOWLISTED_SECRETS:
        monkeypatch.setenv(key, f"allowlisted-value-for-{key}")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    yield monkeypatch


def test_unrelated_application_secrets_are_never_forwarded(full_environment):
    env = _paper_runtime_command_env()
    for key in _UNRELATED_SECRETS:
        assert key not in env


def test_allowlisted_alpaca_and_provider_keys_pass_through_verbatim(full_environment):
    env = _paper_runtime_command_env()
    for key in _ALLOWLISTED_SECRETS:
        assert env[key] == f"allowlisted-value-for-{key}"


def test_env_contains_only_allowlisted_keys(full_environment):
    env = _paper_runtime_command_env()
    assert set(env) <= {"PATH", "PYTHONPATH", *_ALLOWLISTED_SECRETS}


def test_missing_optional_keys_are_simply_absent(monkeypatch):
    for key in (*_UNRELATED_SECRETS, *_ALLOWLISTED_SECRETS, "PATH"):
        monkeypatch.delenv(key, raising=False)
    env = _paper_runtime_command_env()
    for key in _ALLOWLISTED_SECRETS:
        assert key not in env
    assert "PATH" not in env
