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
from dataclasses import dataclass, field

ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"


class ConfigurationError(RuntimeError):
    """Configuration is missing or cannot prove paper mode — fail closed."""


@dataclass(frozen=True)
class RuntimeConfiguration:
    broker_provider: str
    alpaca_api_key: str | None = field(repr=False)
    alpaca_api_secret: str | None = field(repr=False)
    alpaca_is_paper_flag: bool
    alpaca_base_url: str = ALPACA_PAPER_BASE_URL

    @property
    def has_api_key(self) -> bool:
        return bool(self.alpaca_api_key)

    @property
    def has_api_secret(self) -> bool:
        return bool(self.alpaca_api_secret)

    @property
    def has_credentials(self) -> bool:
        return self.has_api_key and self.has_api_secret

    @property
    def paper_endpoint_configured(self) -> bool:
        return self.broker_provider == "alpaca" and self.alpaca_base_url == ALPACA_PAPER_BASE_URL



# Milestone 11.2 Part 21: the only keys a dedicated `PAPER_RUNTIME_ENV_FILE`
# may contain. Any other key present anywhere in the file causes the whole
# file to be rejected (fail closed — none of its values are loaded), which
# is also what makes pointing this at the main repository's own `.env`
# (Anthropic/Reddit/Robinhood/database secrets, none allowlisted here) fail
# closed without needing to hard-code a path to that file.
_ALLOWED_ENV_FILE_KEYS = frozenset({
    "ALPACA_API_KEY", "ALPACA_API_SECRET", "ALPACA_IS_PAPER", "ALPACA_BASE_URL", "PAPER_BROKER_PROVIDER",
})


def _load_dotenv_if_present() -> None:
    """Loads credentials only from a dotenv file the operator explicitly
    named via `PAPER_RUNTIME_ENV_FILE` — never by scanning the filesystem.

    This process is spawned with a minimal, explicitly-constructed
    environment (see `cli.py::_paper_runtime_command_env`) and a `cwd` of
    the main repository root (so relative script invocation keeps working).
    An earlier version called `find_dotenv(usecwd=True)`, which searches
    *upward from cwd* — that silently discovered and loaded the main
    repository's own `.env` (Anthropic/Reddit/Robinhood/database secrets,
    none of which this runtime should ever see) purely because this
    process's cwd happens to be the repo root. `PAPER_RUNTIME_ENV_FILE`
    must name one dedicated, Alpaca-only file outside the repository (or be
    left unset, in which case credentials come only from the subprocess
    environment / a deployment secret manager). Never overrides a variable
    already set in the real environment (`override=False`); never raises —
    an invalid, unreadable, or unsafe env file simply means none of its
    values are loaded, which leaves credentials absent and the environment
    correctly disabled rather than crashing the process (`health`/
    `capabilities` must always be answerable).

    Validation performed before anything from the file is applied to
    `os.environ`:
    * the path must be absolute (a relative path is ambiguous about which
      directory it's relative to once this process's cwd is fixed by the
      main process — rejected rather than silently resolved against an
      assumption that could be wrong);
    * symlinks are resolved (`Path.resolve()`) before every other check, so
      a symlink cannot be used to point at a file that would otherwise be
      rejected;
    * on POSIX, a file writable by group or other is rejected (a dedicated
      credential file should be operator-readable only);
    * every key in the file must be in `_ALLOWED_ENV_FILE_KEYS` — a single
      unknown key rejects the *entire* file (fail closed), not just that
      key. Duplicate keys within the file are resolved deterministically by
      `dotenv_values` (last occurrence wins), matching `python-dotenv`'s own
      documented behavior.
    """
    explicit_path = os.environ.get("PAPER_RUNTIME_ENV_FILE")
    if not explicit_path:
        return
    try:
        from dotenv import dotenv_values
    except ImportError:
        return
    from pathlib import Path

    raw_path = Path(explicit_path)
    if not raw_path.is_absolute():
        return  # ambiguous relative path — fail closed, load nothing
    resolved = raw_path.resolve()
    if not resolved.is_file():
        return
    try:
        mode = resolved.stat().st_mode
        if mode & 0o022:  # group- or other-writable
            return
    except OSError:
        return
    try:
        values = dotenv_values(resolved)
    except Exception:
        return
    unknown_keys = set(values) - _ALLOWED_ENV_FILE_KEYS
    if unknown_keys:
        return  # entire file rejected — fail closed
    for key, value in values.items():
        if value is not None and key not in os.environ:
            os.environ[key] = value


def load_runtime_configuration() -> RuntimeConfiguration:
    """Read configuration from the environment. Never raises — even total
    absence of credentials is a valid (if unusable-for-submission) state,
    since `health`/`capabilities` must still be answerable so the main
    process can observe *why* submission is unavailable."""
    _load_dotenv_if_present()
    is_paper_raw = os.environ.get("ALPACA_IS_PAPER", "")
    return RuntimeConfiguration(
        broker_provider=os.environ.get("PAPER_BROKER_PROVIDER", "alpaca").strip().lower(),
        alpaca_api_key=os.environ.get("ALPACA_API_KEY") or None,
        alpaca_api_secret=os.environ.get("ALPACA_API_SECRET") or None,
        alpaca_is_paper_flag=is_paper_raw.strip().lower() == "true",
        alpaca_base_url=os.environ.get("ALPACA_BASE_URL", ALPACA_PAPER_BASE_URL).strip(),
    )
