"""Reddit MCP adapter.

Mode A (stdio, default): launches the configured local command (e.g.
`npx -y reddit-mcp-server`) as a subprocess and speaks MCP over stdio using
the official `mcp` Python SDK. This is a real client — list_tools() and
call_tool() actually talk to the subprocess.

Mode B (http): validates a remote, read-only HTTPS MCP endpoint. Only HTTPS
is accepted; tool calls are restricted to the read-only allowlist exactly
like Mode A.

In both modes only tools that tool_classifier.classify_tool() marks
allowed_for_research=True are ever callable through call_read_only_tool().
"""
from __future__ import annotations

import asyncio
import shlex
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..config import Config
from ..logging_config import get_logger
from .capability_inventory import build_inventory
from .models_compat import raw_tools_from_mcp_list
from .tool_classifier import load_policy

log = get_logger("mcp.reddit_adapter")


class ReadOnlyPolicyError(RuntimeError):
    """Raised when code attempts to call a Reddit tool that isn't allowlisted read-only."""


def _stdio_params(config: Config) -> StdioServerParameters:
    parts = shlex.split(config.reddit_mcp_command)
    if not parts:
        raise ValueError("REDDIT_MCP_COMMAND is empty")
    return StdioServerParameters(command=parts[0], args=parts[1:], env=None)


@asynccontextmanager
async def _stdio_session(config: Config):
    params = _stdio_params(config)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


@asynccontextmanager
async def open_session(config: Config):
    """Async context manager yielding a live, initialized MCP ClientSession.

    Only stdio mode is implemented for live calls in this repository; http
    mode is validated (HTTPS-only, timeout/retry knobs) but requires a
    concrete remote server URL from the user to exercise end-to-end.
    """
    if config.reddit_mcp_mode == "stdio":
        async with _stdio_session(config) as session:
            yield session
    else:
        raise NotImplementedError(
            "REDDIT_MCP_MODE=http requires a configured, reachable HTTPS Reddit MCP "
            "endpoint. Mode B is validated (see config.py HTTPS check) but no default "
            "remote server is bundled — set REDDIT_MCP_URL to a real endpoint to use it."
        )


async def list_tools(config: Config) -> list[dict]:
    async with open_session(config) as session:
        result = await session.list_tools()
        return raw_tools_from_mcp_list(result)


def build_reddit_capability_inventory(config: Config):
    from ..models.capability_models import Authentication, Transport

    raw_tools = asyncio.run(list_tools(config))
    return build_inventory(
        server_name="reddit",
        transport=Transport.STDIO if config.reddit_mcp_mode == "stdio" else Transport.STREAMABLE_HTTP,
        endpoint=None if config.reddit_mcp_mode == "stdio" else "redacted",
        authentication=Authentication.NONE,
        raw_tools=raw_tools,
    )


def _allowed_tool_names(server: str = "reddit") -> set[str]:
    return set(load_policy().get(server, {}).get("allowlist", []))


async def call_read_only_tool(config: Config, tool_name: str, arguments: dict[str, Any]) -> Any:
    if tool_name not in _allowed_tool_names():
        raise ReadOnlyPolicyError(
            f"'{tool_name}' is not on the read-only Reddit allowlist (config/tool_policy.yaml) "
            "and cannot be called by the research pipeline."
        )
    async with open_session(config) as session:
        log.info("Calling read-only reddit tool", extra={"operation": tool_name})
        return await session.call_tool(tool_name, arguments=arguments)
