"""Builds McpCapabilityInventory objects from a raw (name, description) tool list.

This module contains no network code — callers (robinhood_inventory.py,
reddit_adapter.py) are responsible for obtaining the raw tool list; this
module only classifies it deterministically and shapes the sanitized report.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..logging_config import get_logger
from ..models.capability_models import (
    Authentication,
    McpCapabilityInventory,
    Transport,
)
from .tool_classifier import classify_tool, load_policy

log = get_logger("mcp.capability_inventory")


def build_inventory(
    server_name: str,
    transport: Transport,
    endpoint: str | None,
    authentication: Authentication,
    raw_tools: list[dict],
    warnings: list[str] | None = None,
) -> McpCapabilityInventory:
    policy = load_policy()
    tools = [
        classify_tool(server_name, t["name"], t.get("description", ""), policy)
        for t in raw_tools
    ]
    unknown_count = sum(1 for t in tools if t.reason.endswith("prohibited by default (fail closed) until reviewed and added to the allowlist.") or "default to prohibited" in t.reason)
    inventory = McpCapabilityInventory(
        server_name=server_name,
        transport=transport,
        endpoint=endpoint,
        authentication=authentication,
        tools=tools,
        warnings=list(warnings or []),
    )
    if unknown_count:
        inventory.warnings.append(
            f"{unknown_count} tool(s) classified UNKNOWN and prohibited by default — review config/tool_policy.yaml."
        )
    write_allowed = [t.name for t in tools if t.allowed_for_research and t.classification.value == "write"]
    if write_allowed:
        # Should be unreachable given the classifier, but fail loudly if the policy ever regresses.
        raise RuntimeError(f"Policy violation: write tool(s) marked allowed_for_research: {write_allowed}")
    log.info(
        "Built capability inventory for %s: %d tools, %d allowed for research",
        server_name,
        len(tools),
        sum(1 for t in tools if t.allowed_for_research),
    )
    return inventory


def write_inventory_json(inventory: McpCapabilityInventory, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{inventory.server_name}-tools.json"
    path.write_text(json.dumps(inventory.to_dict(), indent=2), encoding="utf-8")
    return path


def render_summary_markdown(inventories: list[McpCapabilityInventory]) -> str:
    lines = ["# MCP Capability Inventory Summary", ""]
    for inv in inventories:
        allowed = [t for t in inv.tools if t.allowed_for_research]
        prohibited = [t for t in inv.tools if not t.allowed_for_research]
        lines += [
            f"## {inv.server_name}",
            "",
            f"- Transport: `{inv.transport.value}`",
            f"- Authentication: `{inv.authentication.value}`",
            f"- Endpoint: `{inv.endpoint or 'n/a'}`",
            f"- Captured: {inv.inventory_timestamp}",
            f"- Tools: {len(inv.tools)} total, **{len(allowed)} allowed for research**, {len(prohibited)} prohibited",
            "",
        ]
        if inv.warnings:
            lines.append("**Warnings:**")
            for w in inv.warnings:
                lines.append(f"- {w}")
            lines.append("")
        lines.append("### Allowed (read-only, research-eligible)")
        lines.append("")
        lines.append("| Tool | Reason |")
        lines.append("|---|---|")
        for t in sorted(allowed, key=lambda t: t.name):
            lines.append(f"| `{t.name}` | {t.reason} |")
        lines.append("")
        lines.append("### Prohibited")
        lines.append("")
        lines.append("| Tool | Risk | Reason |")
        lines.append("|---|---|---|")
        for t in sorted(prohibited, key=lambda t: t.name):
            lines.append(f"| `{t.name}` | {t.risk.value} | {t.reason} |")
        lines.append("")
    return "\n".join(lines)


def write_summary_markdown(inventories: list[McpCapabilityInventory], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "mcp-inventory-summary.md"
    path.write_text(render_summary_markdown(inventories), encoding="utf-8")
    return path
