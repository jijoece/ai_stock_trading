from __future__ import annotations


class LumiBotAdapterError(RuntimeError):
    """Base error for the LumiBot paper-execution adapter boundary."""


class UnknownLumiBotStatusError(LumiBotAdapterError):
    """A LumiBot order status this adapter does not recognize — fail closed
    rather than silently mapping it to some internal event type."""
