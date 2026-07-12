"""LumiBot-adapter-local configuration (docs/milestone-3.md Step 5/9).

Separate from `execution/config.py` (repository-wide trading-mode policy)
because this is purely about how the LumiBot boundary identifies itself to
LumiBot's own `Order`/`Strategy` machinery — it never decides whether paper
or live execution is permitted; `execution/config.py::ExecutionConfig` and
`execution/live_gateway.py` are the sole authorities for that.
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import LumiBotAdapterError

STRATEGY_NAME = "agentic-trading-desk-paper"
TIME_IN_FORCE = "day"


@dataclass(frozen=True)
class LumiBotAdapterConfig:
    strategy_name: str = STRATEGY_NAME
    time_in_force: str = TIME_IN_FORCE
    mode: str = "paper"

    def __post_init__(self) -> None:
        if self.mode != "paper":
            raise LumiBotAdapterError(f"LumiBotAdapterConfig only supports paper mode — got {self.mode!r}")
