"""Tests the real LumiBot boundary: `runtime.lumibot.adapter` translates a
`PaperOrderIntent` into a genuine `lumibot.entities.order.Order` and maps a
scripted broker-callback stream into internal `PaperExecutionEvent`s.

Guarded with `pytest.importorskip("lumibot")` — LumiBot is the optional
`paper` extra (see pyproject.toml), not a base dependency (see
docs/milestone3-lumibot-paper-integration.md "Known limitations": lumibot
pulls ~140 unrelated transitive packages and downgrades this repo's pinned
jsonschema/python-dotenv floor). The default 169+N test baseline must pass
whether or not this extra is installed; only this file (and nothing it
imports transitively at module scope) is skipped without it.

The `broker_gateway` here is a hand-written fake — see
`runtime/lumibot/adapter.py`'s module docstring for why: LumiBot itself has
no bundled "simulate a fill with no credentials/network" broker, so a real
end-to-end broker round trip cannot be exercised offline. What IS real and
tested here is the intent -> LumiBot Order translation and the
status-mapping/event-construction logic around whatever the gateway yields.
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

lumibot = pytest.importorskip("lumibot", reason="lumibot is an optional 'paper' extra — see pyproject.toml")

from trading_research.execution.config import load_execution_config  # noqa: E402
from trading_research.execution.intent_builder import build_paper_order_intent  # noqa: E402
from trading_research.runtime.lumibot.adapter import LumiBotPaperExecutionAdapter, _translate_intent  # noqa: E402
from trading_research.runtime.lumibot.configuration import LumiBotAdapterConfig  # noqa: E402
from trading_research.runtime.lumibot.errors import LumiBotAdapterError, UnknownLumiBotStatusError  # noqa: E402

from tests.support.execution_fixtures import buy_candidate_payload  # noqa: E402

NOW = datetime(2026, 7, 11, 15, 0, tzinfo=timezone.utc)
CONFIG = load_execution_config()


@pytest.fixture
def intent():
    payload = buy_candidate_payload(shares=70, entry_price=14.25)
    return build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")


class ScriptedGateway:
    """Fake `PaperBrokerGateway`: yields a pre-scripted sequence of
    (raw_status, filled_qty, price, broker_order_id) callbacks."""

    def __init__(self, script, snapshot=None):
        self.script = script
        self._snapshot = snapshot
        self.submitted_orders = []

    def submit_order(self, order):
        self.submitted_orders.append(order)
        return iter(self.script)

    def snapshot(self, broker_order_id):
        return self._snapshot


def test_correct_intent_to_order_translation(intent):
    from lumibot.entities.order import Order as LumiBotOrder

    order = _translate_intent(intent, LumiBotAdapterConfig())
    assert order.asset.symbol == "SOFI"
    assert order.quantity == 70
    assert order.side == LumiBotOrder.OrderSide.BUY
    assert order.order_type == LumiBotOrder.OrderType.MARKET
    assert order.identifier == intent.intent_id


def test_limit_order_translation_carries_limit_price():
    from dataclasses import replace

    payload = buy_candidate_payload(shares=70, entry_price=14.25)
    intent = build_paper_order_intent(payload, config=CONFIG, git_sha="abc1234")
    limit_intent = replace(
        intent, order_type="LIMIT", limit_price=Decimal("14.50"),
    )
    order = _translate_intent(limit_intent, LumiBotAdapterConfig())
    # Decimal -> float: the one unavoidable, documented conversion at the boundary.
    assert order.limit_price == pytest.approx(14.50)
    assert isinstance(order.limit_price, float)


def test_known_status_mappings_produce_correct_events(intent):
    gateway = ScriptedGateway(
        script=[
            ("submitted", 0, None, "broker-order-1"),
            ("fill", 70, 14.30, "broker-order-1"),
        ],
    )
    adapter = LumiBotPaperExecutionAdapter(broker_gateway=gateway, clock=lambda: NOW)
    events, result = adapter.submit(intent)
    assert [e.event_type for e in events] == ["SUBMITTED", "FILLED"]
    assert result.final_status == "FILLED"
    assert result.filled_quantity == 70


def test_float_to_decimal_boundary_is_explicit(intent):
    gateway = ScriptedGateway(script=[("fill", 70, 14.30, "broker-order-1")])
    adapter = LumiBotPaperExecutionAdapter(broker_gateway=gateway, clock=lambda: NOW)
    events, result = adapter.submit(intent)
    assert isinstance(events[0].fill_price, Decimal)
    assert events[0].fill_price == Decimal("14.3")
    assert isinstance(result.average_fill_price, Decimal)


def test_unknown_status_fails_closed(intent):
    gateway = ScriptedGateway(script=[("cash_settled", 0, None, "broker-order-1")])
    adapter = LumiBotPaperExecutionAdapter(broker_gateway=gateway, clock=lambda: NOW)
    with pytest.raises(UnknownLumiBotStatusError):
        adapter.submit(intent)


def test_callbacks_map_to_internal_events(intent):
    gateway = ScriptedGateway(
        script=[
            ("submitted", 0, None, "broker-order-1"),
            ("partial_fill", 40, 14.28, "broker-order-1"),
            ("fill", 30, 14.32, "broker-order-1"),
        ],
    )
    adapter = LumiBotPaperExecutionAdapter(broker_gateway=gateway, clock=lambda: NOW)
    events, result = adapter.submit(intent)
    assert [e.event_type for e in events] == ["SUBMITTED", "PARTIALLY_FILLED", "FILLED"]
    assert result.filled_quantity == 70
    assert all(e.source == "LUMIBOT_PAPER" for e in events)


def test_reconcile_returns_broker_snapshot(intent):
    gateway = ScriptedGateway(script=[], snapshot=(70, 1001.0, "fill"))
    adapter = LumiBotPaperExecutionAdapter(broker_gateway=gateway, clock=lambda: NOW)
    snapshot = adapter.reconcile(intent.intent_id)
    assert snapshot.broker_quantity == 70
    assert snapshot.broker_notional == Decimal("1001.0")


def test_adapter_cannot_operate_in_live_mode():
    with pytest.raises(LumiBotAdapterError, match="paper mode"):
        LumiBotPaperExecutionAdapter(
            broker_gateway=ScriptedGateway(script=[]), config=LumiBotAdapterConfig(mode="live"),
        )


def test_lumibot_objects_do_not_reach_repositories():
    import re

    import trading_research.storage.execution_repositories as repo_module

    source = open(repo_module.__file__).read()
    assert "lumibot" not in source.lower()


def test_no_lumibot_import_outside_runtime_package():
    import ast
    import pathlib

    src_root = pathlib.Path(__file__).resolve().parents[2] / "src" / "trading_research"
    offenders = []
    for path in src_root.rglob("*.py"):
        if "runtime" in path.parts:
            continue  # the one package that is allowed to import lumibot
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "lumibot" for a in node.names):
                offenders.append(str(path))
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "lumibot":
                offenders.append(str(path))
    assert offenders == []
