from __future__ import annotations

import os

import pytest

from trading_paper_runtime.configuration import load_runtime_configuration


@pytest.fixture
def clean_env(monkeypatch):
    for key in (
        "ALPACA_API_KEY", "ALPACA_API_SECRET", "ALPACA_IS_PAPER", "ALPACA_BASE_URL",
        "PAPER_BROKER_PROVIDER", "PAPER_RUNTIME_ENV_FILE", "ANTHROPIC_API_KEY",
        "REDDIT_CLIENT_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch


def test_runtime_does_not_load_a_dotenv_in_the_working_directory(clean_env, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ALPACA_API_KEY=leaked-from-cwd-dotenv\n")
    monkeypatch.chdir(tmp_path)
    config = load_runtime_configuration()
    assert config.alpaca_api_key is None
    assert config.has_credentials is False


def test_runtime_ignores_a_dotenv_in_a_parent_directory(clean_env, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ALPACA_API_KEY=leaked-from-parent-dotenv\n")
    child = tmp_path / "nested" / "deeper"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)
    config = load_runtime_configuration()
    assert config.alpaca_api_key is None


def test_runtime_loads_only_an_explicitly_named_env_file(clean_env, tmp_path):
    dedicated = tmp_path / "alpaca-only.env"
    dedicated.write_text(
        "ALPACA_API_KEY=explicit-key\nALPACA_API_SECRET=explicit-secret\n"
        "ALPACA_IS_PAPER=true\n"
    )
    dedicated.chmod(0o600)
    os.environ["PAPER_RUNTIME_ENV_FILE"] = str(dedicated)
    try:
        config = load_runtime_configuration()
    finally:
        del os.environ["PAPER_RUNTIME_ENV_FILE"]
        for key in ("ALPACA_API_KEY", "ALPACA_API_SECRET", "ALPACA_IS_PAPER", "ANTHROPIC_API_KEY"):
            os.environ.pop(key, None)
    assert config.has_credentials is True
    assert config.alpaca_is_paper_flag is True


def test_runtime_rejects_the_whole_file_when_an_unknown_key_is_present(clean_env, tmp_path):
    """Milestone 11.2 Part 21: a single unknown key anywhere in the
    dedicated env file causes the entire file to be rejected — even the
    allowlisted keys alongside it are never loaded — so accidentally
    pointing PAPER_RUNTIME_ENV_FILE at the main repository's own `.env`
    (which carries ANTHROPIC_API_KEY, REDDIT_*, etc.) fails closed."""
    dedicated = tmp_path / "mixed.env"
    dedicated.write_text(
        "ALPACA_API_KEY=explicit-key\nALPACA_API_SECRET=explicit-secret\n"
        "ALPACA_IS_PAPER=true\nANTHROPIC_API_KEY=unrelated-secret-should-not-matter\n"
    )
    dedicated.chmod(0o600)
    os.environ["PAPER_RUNTIME_ENV_FILE"] = str(dedicated)
    try:
        config = load_runtime_configuration()
    finally:
        del os.environ["PAPER_RUNTIME_ENV_FILE"]
        for key in ("ALPACA_API_KEY", "ALPACA_API_SECRET", "ALPACA_IS_PAPER", "ANTHROPIC_API_KEY"):
            os.environ.pop(key, None)
    assert config.has_credentials is False
    assert config.alpaca_api_key is None


def test_runtime_rejects_a_relative_env_file_path(clean_env, tmp_path, monkeypatch):
    dedicated = tmp_path / "alpaca-only.env"
    dedicated.write_text("ALPACA_API_KEY=explicit-key\nALPACA_API_SECRET=explicit-secret\n")
    dedicated.chmod(0o600)
    monkeypatch.chdir(tmp_path)
    os.environ["PAPER_RUNTIME_ENV_FILE"] = "alpaca-only.env"  # relative, not absolute
    try:
        config = load_runtime_configuration()
    finally:
        del os.environ["PAPER_RUNTIME_ENV_FILE"]
        for key in ("ALPACA_API_KEY", "ALPACA_API_SECRET"):
            os.environ.pop(key, None)
    assert config.has_credentials is False


def test_runtime_rejects_a_group_writable_env_file(clean_env, tmp_path):
    dedicated = tmp_path / "alpaca-only.env"
    dedicated.write_text("ALPACA_API_KEY=explicit-key\nALPACA_API_SECRET=explicit-secret\n")
    dedicated.chmod(0o660)  # group-writable
    os.environ["PAPER_RUNTIME_ENV_FILE"] = str(dedicated)
    try:
        config = load_runtime_configuration()
    finally:
        del os.environ["PAPER_RUNTIME_ENV_FILE"]
        for key in ("ALPACA_API_KEY", "ALPACA_API_SECRET"):
            os.environ.pop(key, None)
    assert config.has_credentials is False


def test_real_environment_takes_precedence_over_the_named_dotenv_file(clean_env, tmp_path):
    dedicated = tmp_path / "alpaca-only.env"
    dedicated.write_text("ALPACA_API_KEY=from-dotenv\n")
    os.environ["PAPER_RUNTIME_ENV_FILE"] = str(dedicated)
    os.environ["ALPACA_API_KEY"] = "from-real-environment"
    try:
        config = load_runtime_configuration()
    finally:
        del os.environ["PAPER_RUNTIME_ENV_FILE"]
        os.environ.pop("ALPACA_API_KEY", None)
    assert config.alpaca_api_key == "from-real-environment"


def test_health_output_exposes_presence_booleans_only():
    from trading_paper_runtime.dispatcher import Dispatcher
    from trading_paper_runtime.deterministic_gateway import DeterministicBrokerGateway
    from trading_paper_runtime.configuration import RuntimeConfiguration
    from trading_paper_runtime.protocol import RequestEnvelope
    from datetime import datetime, timezone

    config = RuntimeConfiguration(
        broker_provider="alpaca", alpaca_api_key="super-secret-key",
        alpaca_api_secret="super-secret-value", alpaca_is_paper_flag=True,
    )
    dispatcher = Dispatcher(gateway=DeterministicBrokerGateway(), config=config)
    payload = dispatcher.handle(RequestEnvelope(
        protocol_version="paper-runtime.v2", request_id="req-1", operation="health",
        sent_at=datetime.now(timezone.utc).isoformat(), payload={},
    ))
    serialized = str(payload)
    assert "super-secret-key" not in serialized
    assert "super-secret-value" not in serialized
    assert payload["has_api_key"] is True
    assert payload["has_api_secret"] is True
