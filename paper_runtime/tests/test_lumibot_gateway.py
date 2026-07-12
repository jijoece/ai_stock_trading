"""LumiBot/Alpaca translation tests (docs/milestone-4.md Step 16.D).

Guarded with `pytest.importorskip("lumibot")` since lumibot is this
package's base dependency but these specific tests exercise real LumiBot
entity construction. No network call and no real credentials are used or
required — construction-time paper-mode/credential verification is tested
without ever reaching Alpaca's servers.
"""
from __future__ import annotations

import pytest

pytest.importorskip("lumibot")

from trading_paper_runtime.configuration import RuntimeConfiguration
from trading_paper_runtime.lumibot_gateway import LumiBotAlpacaPaperGateway


def test_missing_credentials_fails_closed():
    config = RuntimeConfiguration(
        broker_provider="alpaca", alpaca_api_key=None, alpaca_api_secret=None, alpaca_is_paper_flag=True,
    )
    gateway = LumiBotAlpacaPaperGateway(config=config)
    assert gateway.is_paper_mode_verified() is False
    assert gateway._init_error is not None


def test_missing_explicit_paper_flag_fails_closed_even_with_credentials():
    config = RuntimeConfiguration(
        broker_provider="alpaca", alpaca_api_key="fake-key", alpaca_api_secret="fake-secret",
        alpaca_is_paper_flag=False,
    )
    gateway = LumiBotAlpacaPaperGateway(config=config)
    assert gateway.is_paper_mode_verified() is False
    assert "ALPACA_IS_PAPER" in gateway._init_error


def test_submit_order_refuses_when_not_verified():
    from trading_paper_runtime.errors import ErrorCode, RuntimeOperationError
    from trading_paper_runtime.models import OrderIntentPayload

    config = RuntimeConfiguration(
        broker_provider="alpaca", alpaca_api_key=None, alpaca_api_secret=None, alpaca_is_paper_flag=False,
    )
    gateway = LumiBotAlpacaPaperGateway(config=config)
    intent = OrderIntentPayload(
        intent_id="intent-1", recommendation_id="rec-1", symbol="AAPL", side="BUY", quantity=1,
        order_type="MARKET", limit_price=None, reference_price="150.00",
        expires_at="2099-01-01T00:00:00+00:00", idempotency_key="intent-1",
    )
    with pytest.raises(RuntimeOperationError) as exc:
        gateway.submit_order(intent)
    assert exc.value.code == ErrorCode.NOT_PAPER_MODE


def test_asset_validation_uses_genuine_lumibot_entity():
    from lumibot.entities import Asset

    config = RuntimeConfiguration(
        broker_provider="alpaca", alpaca_api_key=None, alpaca_api_secret=None, alpaca_is_paper_flag=False,
    )
    gateway = LumiBotAlpacaPaperGateway(config=config)
    asset = Asset(symbol="AAPL", asset_type=Asset.AssetType.STOCK)
    assert asset.symbol == "AAPL"
    # Exercises the same code path submit_order would call before ever
    # reaching the network.
    gateway._validate_asset("AAPL")


def test_alpaca_status_map_covers_no_options_settlement_statuses():
    from trading_paper_runtime.lumibot_gateway import _ALPACA_STATUS_MAP

    for options_only_status in ("cash_settled", "assigned", "exercised"):
        assert options_only_status not in _ALPACA_STATUS_MAP
