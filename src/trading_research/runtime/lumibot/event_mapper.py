"""Maps LumiBot's `Order.OrderStatus` values to this project's internal
`PaperExecutionEvent.event_type` (docs/milestone-3.md Step 5).

Only imports LumiBot for the `OrderStatus` enum reference in the module
docstring/tests — `map_order_status` itself takes a plain string, so it can
be unit-tested (including the fail-closed branch) without LumiBot installed.

LumiBot 4.5.74's `Order.OrderStatus` enum (confirmed via
`[s.value for s in Order.OrderStatus]` in this environment):
unprocessed, submitted, open, new, cancelling, canceled, fill, partial_fill,
cash_settled, assigned, exercised, error, expired, unknown.

Only equity, long-only, market/limit statuses relevant to this milestone are
mapped. `cash_settled`, `assigned`, `exercised` are options-settlement
concepts (options are an explicit non-goal, docs/milestone-3.md "Non-goals")
and `unknown` is LumiBot's own escape hatch — all three, and anything not
in `_STATUS_MAP`, raise `UnknownLumiBotStatusError` rather than being
silently mapped. Note LumiBot 4.5.74 has no distinct "rejected" status in
this enum; `error` is the closest available raw status and is mapped to our
internal `ERROR` (not `REJECTED` — `REJECTED` is reachable only through the
deterministic test adapter, which can script it directly). See
docs/milestone3-lumibot-paper-integration.md "Known limitations".
"""
from __future__ import annotations

from .errors import UnknownLumiBotStatusError

_STATUS_MAP: dict[str, str] = {
    "unprocessed": "SUBMITTED",
    "submitted": "SUBMITTED",
    "new": "ACCEPTED",
    "open": "ACCEPTED",
    "partial_fill": "PARTIALLY_FILLED",
    "fill": "FILLED",
    "canceled": "CANCELLED",
    "cancelling": "CANCELLED",
    "expired": "CANCELLED",
    "error": "ERROR",
}


def map_order_status(raw_status: str) -> str:
    """Return the internal `event_type` for a raw LumiBot order status.

    Raises `UnknownLumiBotStatusError` for anything not explicitly mapped —
    fail closed, never a best-guess default.
    """
    key = str(raw_status).strip().lower()
    if key not in _STATUS_MAP:
        raise UnknownLumiBotStatusError(
            f"unrecognized LumiBot order status {raw_status!r} — fail closed, requires reconciliation"
        )
    return _STATUS_MAP[key]
