"""Data models for MCP capability inventories (schemas/mcp_capability_inventory.schema.json)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Classification(str, Enum):
    READ = "read"
    WRITE = "write"
    UNKNOWN = "unknown"


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    PROHIBITED = "prohibited"


class Transport(str, Enum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"
    SSE = "sse"
    UNKNOWN = "unknown"


class Authentication(str, Enum):
    OAUTH = "oauth"
    BEARER = "bearer"
    NONE = "none"
    UNKNOWN = "unknown"


@dataclass
class ToolCapability:
    name: str
    description: str
    classification: Classification
    risk: Risk
    allowed_for_research: bool
    reason: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["classification"] = self.classification.value
        d["risk"] = self.risk.value
        return d


@dataclass
class McpCapabilityInventory:
    server_name: str
    transport: Transport
    endpoint: str | None  # redacted or null — never a raw authenticated URL
    authentication: Authentication
    tools: list[ToolCapability] = field(default_factory=list)
    inventory_timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "server_name": self.server_name,
            "transport": self.transport.value,
            "endpoint": self.endpoint,
            "authentication": self.authentication.value,
            "tools": [t.to_dict() for t in self.tools],
            "inventory_timestamp": self.inventory_timestamp,
            "warnings": self.warnings,
        }

    def read_only_tool_names(self) -> list[str]:
        return [t.name for t in self.tools if t.allowed_for_research]
