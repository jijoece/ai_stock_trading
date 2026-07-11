"""Deterministic MCP tool classification.

Precedence: denylist pattern match (substring, case-insensitive) always wins
over allowlist. Then explicit allowlist (exact name match). Then a read-name
heuristic that only *explains* a guess, never grants research access on its
own. Anything left is UNKNOWN and is always prohibited — this project fails
closed, not open.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from ..config import REPO_ROOT
from ..models.capability_models import Classification, Risk, ToolCapability

DEFAULT_POLICY_PATH = REPO_ROOT / "config" / "tool_policy.yaml"


@lru_cache(maxsize=4)
def load_policy(policy_path: str | Path = DEFAULT_POLICY_PATH) -> dict:
    with open(policy_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def classify_tool(
    server: str,
    name: str,
    description: str,
    policy: dict | None = None,
) -> ToolCapability:
    policy = policy or load_policy()
    server_policy = policy.get(server, {})
    allowlist = set(server_policy.get("allowlist", []))
    denylist_patterns = [p.lower() for p in server_policy.get("denylist_patterns", [])]
    read_patterns = [p.lower() for p in policy.get("read_name_patterns", [])]

    lowered = name.lower()

    # Denylist matching is name-only, deliberately. Descriptions frequently
    # cross-reference other tool names in prose (e.g. a read-only tool's
    # description saying "use edit_comment to change a reply you already
    # posted") — matching against description text produced false positives
    # on tools that merely mention a write tool rather than being one.
    for pattern in denylist_patterns:
        if pattern in lowered:
            return ToolCapability(
                name=name,
                description=description,
                classification=Classification.WRITE,
                risk=Risk.PROHIBITED,
                allowed_for_research=False,
                reason=f"Matched denylist pattern '{pattern}' for server '{server}' — treated as a write/mutating tool.",
            )

    if name in allowlist:
        return ToolCapability(
            name=name,
            description=description,
            classification=Classification.READ,
            risk=Risk.LOW,
            allowed_for_research=True,
            reason=f"Explicitly allowlisted as a read-only tool for server '{server}'.",
        )

    looks_read = any(lowered.startswith(p) for p in read_patterns)
    if looks_read:
        return ToolCapability(
            name=name,
            description=description,
            classification=Classification.UNKNOWN,
            risk=Risk.PROHIBITED,
            allowed_for_research=False,
            reason=(
                f"Name suggests a read operation (matches heuristic pattern) but '{name}' "
                f"is not on the explicit allowlist for '{server}' — classified UNKNOWN and "
                "prohibited by default (fail closed) until reviewed and added to the allowlist."
            ),
        )

    return ToolCapability(
        name=name,
        description=description,
        classification=Classification.UNKNOWN,
        risk=Risk.PROHIBITED,
        allowed_for_research=False,
        reason=f"Not on the allowlist or denylist for server '{server}' — unknown tools default to prohibited.",
    )
