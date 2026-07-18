"""Typed configuration loading from environment variables / .env.

Secrets (API keys, tokens) are never logged. Use redact_secrets() in
logging_config.py to scrub any string that might contain a config value.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]

_REQUIRED_FOR_BATCH = ("ANTHROPIC_API_KEY",)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str | None
    anthropic_model: str
    anthropic_batch_poll_interval_seconds: int

    research_data_dir: Path
    research_database_path: Path

    reddit_mcp_mode: str
    reddit_mcp_command: str
    reddit_mcp_url: str | None
    reddit_mcp_auth_token: str | None
    reddit_client_id: str | None
    reddit_client_secret: str | None

    robinhood_mcp_url: str

    log_level: str

    # Milestone 6 real market-data provider — deliberately distinct from
    # ALPACA_API_KEY/ALPACA_API_SECRET (Milestone 4), which remain read
    # exclusively by the isolated paper_runtime process. See .env.example.
    alpaca_market_data_api_key: str | None = None
    alpaca_market_data_api_secret: str | None = None

    secret_fields: tuple[str, ...] = field(
        default=(
            "anthropic_api_key", "reddit_mcp_auth_token", "alpaca_market_data_api_key",
            "alpaca_market_data_api_secret", "reddit_client_id", "reddit_client_secret",
        ),
        repr=False,
    )

    def require_anthropic_key(self) -> str:
        if not self.anthropic_api_key:
            raise ConfigError(
                "ANTHROPIC_API_KEY is not set. Batch submission requires a real "
                "Anthropic API key; export it or add it to .env before running "
                "submit-batch / run-research."
            )
        return self.anthropic_api_key

    def secrets(self) -> list[str]:
        """Return the actual secret values currently loaded, for log redaction."""
        values = []
        for f in self.secret_fields:
            v = getattr(self, f, None)
            if v:
                values.append(v)
        return values


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def load_config(env_file: str | Path | None = None, *, require_anthropic: bool = False) -> Config:
    """Load configuration from the environment (and an optional .env file).

    Does not raise for missing secrets by default — callers that need the
    Anthropic key should call require_anthropic=True or Config.require_anthropic_key().
    """
    dotenv_path = Path(env_file) if env_file else REPO_ROOT / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path, override=False)

    data_dir = Path(os.environ.get("RESEARCH_DATA_DIR") or str(REPO_ROOT / "data"))
    db_path = Path(
        os.environ.get("RESEARCH_DATABASE_PATH", str(data_dir / "research.sqlite3"))
    )

    reddit_mode = os.environ.get("REDDIT_MCP_MODE", "stdio").strip().lower()
    if reddit_mode not in ("stdio", "http"):
        raise ConfigError(f"REDDIT_MCP_MODE must be 'stdio' or 'http', got {reddit_mode!r}")

    cfg = Config(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
        anthropic_batch_poll_interval_seconds=_int_env(
            "ANTHROPIC_BATCH_POLL_INTERVAL_SECONDS", 30
        ),
        research_data_dir=data_dir,
        research_database_path=db_path,
        reddit_mcp_mode=reddit_mode,
        reddit_mcp_command=os.environ.get("REDDIT_MCP_COMMAND", "npx -y reddit-mcp-server"),
        reddit_mcp_url=os.environ.get("REDDIT_MCP_URL") or None,
        reddit_mcp_auth_token=os.environ.get("REDDIT_MCP_AUTH_TOKEN") or None,
        reddit_client_id=os.environ.get("REDDIT_CLIENT_ID") or None,
        reddit_client_secret=os.environ.get("REDDIT_CLIENT_SECRET") or None,
        robinhood_mcp_url=os.environ.get(
            "ROBINHOOD_MCP_URL", "https://agent.robinhood.com/mcp/trading"
        ),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        alpaca_market_data_api_key=os.environ.get("ALPACA_MARKET_DATA_API_KEY") or None,
        alpaca_market_data_api_secret=os.environ.get("ALPACA_MARKET_DATA_API_SECRET") or None,
    )

    if require_anthropic:
        cfg.require_anthropic_key()

    if cfg.reddit_mcp_mode == "http" and not cfg.reddit_mcp_url:
        raise ConfigError("REDDIT_MCP_MODE=http requires REDDIT_MCP_URL to be set")
    if cfg.reddit_mcp_mode == "http" and cfg.reddit_mcp_url and not cfg.reddit_mcp_url.startswith(
        "https://"
    ):
        raise ConfigError("REDDIT_MCP_URL must use HTTPS")

    # Milestone 11.3 Part 28: no filesystem mutation on config load — the
    # database directory is created at first use by
    # `storage/database.py::connect()`, and `research_data_dir` has no
    # current write consumer that needs pre-creation.
    return cfg
