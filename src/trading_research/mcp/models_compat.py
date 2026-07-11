"""Small adapters between the `mcp` SDK's typed results and this package's plain dicts."""
from __future__ import annotations

from typing import Any


def raw_tools_from_mcp_list(list_tools_result: Any) -> list[dict]:
    return [
        {"name": t.name, "description": t.description or ""}
        for t in list_tools_result.tools
    ]
