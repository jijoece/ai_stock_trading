"""Robinhood MCP capability inventory.

The hosted Robinhood MCP (https://agent.robinhood.com/mcp/trading) uses
per-user OAuth with no service-account/sandbox credential, so there is no
headless way to call tools/list from a standalone script in this
environment. inventory() therefore loads a dated snapshot captured from a
live, already-authenticated Claude Code session (robinhood_tools_snapshot.json)
and runs it through the same deterministic classifier as every other server.
This is documented, not hidden — see the "capture_method" field in the
snapshot and the warning this function attaches to the inventory.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import Config
from ..models.capability_models import Authentication, Transport
from .capability_inventory import build_inventory

SNAPSHOT_PATH = Path(__file__).with_name("robinhood_tools_snapshot.json")


def _redact_endpoint(url: str) -> str:
    """Keep scheme+host, drop any path/query that could carry account context."""
    if "://" not in url:
        return "redacted"
    scheme, rest = url.split("://", 1)
    host = rest.split("/", 1)[0]
    return f"{scheme}://{host}"


def load_snapshot() -> dict:
    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def inventory(config: Config):
    snapshot = load_snapshot()
    warnings = [
        f"Tool list sourced from a live-session snapshot captured {snapshot['captured_at']} "
        "(capture_method=live_session_tool_list), not a live OAuth-authenticated fetch — "
        "this environment has no Robinhood OAuth credential to call tools/list headlessly.",
    ]
    return build_inventory(
        server_name="robinhood",
        transport=Transport.STREAMABLE_HTTP,
        endpoint=_redact_endpoint(config.robinhood_mcp_url),
        authentication=Authentication.OAUTH,
        raw_tools=snapshot["tools"],
        warnings=warnings,
    )
