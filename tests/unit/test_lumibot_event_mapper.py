"""Tests `runtime.lumibot.event_mapper.map_order_status` directly against
plain strings — deliberately does NOT require LumiBot to be installed (no
`pytest.importorskip`), since `map_order_status` takes a string, not a
LumiBot object. This keeps the fail-closed status-mapping behavior covered
by the default (169+N) test baseline even when the optional `paper` extra
is absent — only `test_lumibot_adapter.py` (real Order/Asset construction)
needs the importorskip guard.
"""
import pytest

from trading_research.runtime.lumibot.errors import UnknownLumiBotStatusError
from trading_research.runtime.lumibot.event_mapper import map_order_status


@pytest.mark.parametrize(
    "raw_status,expected",
    [
        ("unprocessed", "SUBMITTED"),
        ("submitted", "SUBMITTED"),
        ("new", "ACCEPTED"),
        ("open", "ACCEPTED"),
        ("partial_fill", "PARTIALLY_FILLED"),
        ("fill", "FILLED"),
        ("canceled", "CANCELLED"),
        ("cancelling", "CANCELLED"),
        ("expired", "CANCELLED"),
        ("error", "ERROR"),
        ("FILL", "FILLED"),  # case-insensitive
        ("  fill  ", "FILLED"),  # whitespace-tolerant
    ],
)
def test_known_status_mappings(raw_status, expected):
    assert map_order_status(raw_status) == expected


@pytest.mark.parametrize("raw_status", ["cash_settled", "assigned", "exercised", "unknown", "totally_bogus", ""])
def test_unknown_status_fails_closed(raw_status):
    with pytest.raises(UnknownLumiBotStatusError):
        map_order_status(raw_status)
