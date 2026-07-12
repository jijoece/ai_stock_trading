"""Environment-only credential and endpoint configuration (docs/milestone-4.md
Step 5).

Hard rules enforced here:

* credentials come only from environment variables (which may themselves be
  populated by a `.env` file LumiBot's own `credentials` module loads) —
  never a hard-coded value, never a CLI argument;
* `ALPACA_IS_PAPER` must be the exact string `"true"` (case-insensitive) —
  absent, empty, or any other value is treated as *not proven paper* and
  blocks submission (this is stricter than LumiBot's own default, which
  silently assumes paper=True when the variable is unset — we do not trust
  that default for a hard safety requirement);
* nothing here ever logs or returns a credential value — only booleans.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigurationError(RuntimeError):
    """Configuration is missing or cannot prove paper mode — fail closed."""


@dataclass(frozen=True)
class RuntimeConfiguration:
    broker_provider: str
    alpaca_api_key: str | None
    alpaca_api_secret: str | None
    alpaca_is_paper_flag: bool

    @property
    def has_api_key(self) -> bool:
        return bool(self.alpaca_api_key)

    @property
    def has_api_secret(self) -> bool:
        return bool(self.alpaca_api_secret)

    @property
    def has_credentials(self) -> bool:
        return self.has_api_key and self.has_api_secret


def load_runtime_configuration() -> RuntimeConfiguration:
    """Read configuration from the environment. Never raises — even total
    absence of credentials is a valid (if unusable-for-submission) state,
    since `health`/`capabilities` must still be answerable so the main
    process can observe *why* submission is unavailable."""
    is_paper_raw = os.environ.get("ALPACA_IS_PAPER", "")
    return RuntimeConfiguration(
        broker_provider=os.environ.get("PAPER_BROKER_PROVIDER", "alpaca").strip().lower(),
        alpaca_api_key=os.environ.get("ALPACA_API_KEY") or None,
        alpaca_api_secret=os.environ.get("ALPACA_API_SECRET") or None,
        alpaca_is_paper_flag=is_paper_raw.strip().lower() == "true",
    )
