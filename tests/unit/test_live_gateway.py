from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading_research.execution.config import ExecutionConfigError, load_execution_config
from trading_research.execution.live_gateway import (
    ApprovedOrder,
    DisabledLiveExecutionGateway,
    HumanApproval,
    LiveTradingDisabledError,
)

NOW = datetime(2026, 7, 11, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def gateway():
    return DisabledLiveExecutionGateway()


@pytest.fixture
def approved_order():
    return ApprovedOrder(
        rec_id="rec-1", symbol="SOFI", side="BUY", quantity=70, order_type="MARKET",
        limit_price=None, approval_id="appr-1",
    )


@pytest.fixture
def human_approval():
    return HumanApproval(approval_id="appr-1", approved_by="jijoece@gmail.com", approved_at=NOW, payload_hash="a" * 64)


def test_disabled_gateway_rejects_review(gateway, approved_order):
    with pytest.raises(LiveTradingDisabledError):
        gateway.review_order(approved_order)


def test_disabled_gateway_rejects_placement(gateway, approved_order, human_approval):
    with pytest.raises(LiveTradingDisabledError):
        gateway.place_order(approved_order, human_approval)


def test_disabled_gateway_rejects_cancellation(gateway):
    with pytest.raises(LiveTradingDisabledError):
        gateway.cancel_order("broker-order-1")


def test_disabled_gateway_rejects_modification_via_cancel_reissue(gateway):
    # docs/milestone-3.md's illustrative LiveExecutionGateway Protocol has no
    # distinct "modify" method (only review/place/cancel/reconcile) — order
    # modification in this domain is cancel-then-reissue, so both halves of
    # that path must be blocked.
    with pytest.raises(LiveTradingDisabledError):
        gateway.cancel_order("broker-order-1")
    with pytest.raises(LiveTradingDisabledError):
        gateway.reconcile_order("broker-order-1")


def test_disabled_gateway_rejects_reconciliation(gateway):
    with pytest.raises(LiveTradingDisabledError):
        gateway.reconcile_order("broker-order-1")


def test_no_bypass_flag_exists_on_disabled_gateway(gateway):
    assert not hasattr(gateway, "live_trading_enabled")
    assert not hasattr(gateway, "force")
    assert not hasattr(gateway, "override")


def test_unknown_trading_mode_fails_closed(tmp_path):
    bad_config = tmp_path / "bad_execution.yaml"
    bad_config.write_text(
        "version: 1\n"
        "trading_mode: live\n"
        "live_trading_enabled: false\n"
        "human_approval_required: true\n"
        "kill_switch_enabled: false\n"
        "paper_execution:\n"
        "  policy_version: v1\n"
        "  execution_version: v1\n"
        "  recommendation_ttl_minutes: 60\n"
        "  max_price_staleness_seconds: 900\n"
    )
    with pytest.raises(ExecutionConfigError, match="not recognized"):
        load_execution_config(bad_config)


def test_live_trading_enabled_true_fails_closed(tmp_path):
    bad_config = tmp_path / "bad_execution2.yaml"
    bad_config.write_text(
        "version: 1\n"
        "trading_mode: paper\n"
        "live_trading_enabled: true\n"
        "human_approval_required: true\n"
        "kill_switch_enabled: false\n"
        "paper_execution:\n"
        "  policy_version: v1\n"
        "  execution_version: v1\n"
        "  recommendation_ttl_minutes: 60\n"
        "  max_price_staleness_seconds: 900\n"
    )
    with pytest.raises(ExecutionConfigError, match="not permitted"):
        load_execution_config(bad_config)


def test_missing_trading_mode_does_not_enable_live(tmp_path):
    bad_config = tmp_path / "missing_mode.yaml"
    bad_config.write_text(
        "version: 1\n"
        "live_trading_enabled: false\n"
        "human_approval_required: true\n"
        "kill_switch_enabled: false\n"
        "paper_execution:\n"
        "  policy_version: v1\n"
        "  execution_version: v1\n"
        "  recommendation_ttl_minutes: 60\n"
        "  max_price_staleness_seconds: 900\n"
    )
    with pytest.raises(ExecutionConfigError, match="missing keys"):
        load_execution_config(bad_config)


def test_default_repository_config_is_paper_mode():
    config = load_execution_config()
    assert config.trading_mode == "paper"
    assert config.live_trading_enabled is False


def test_environment_variable_cannot_override_trading_mode(monkeypatch):
    # execution/config.py deliberately never reads os.environ for
    # trading_mode/live_trading_enabled — confirm that holds even when an
    # operator sets a live-looking env var.
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    config = load_execution_config()
    assert config.trading_mode == "paper"
    assert config.live_trading_enabled is False


def test_real_orders_remains_write_blocked_by_live_gateway_module():
    import re

    import trading_research.execution.live_gateway as live_gateway_module

    sql_write_pattern = re.compile(r"(INSERT|UPDATE|DELETE)\s+(INTO\s+|FROM\s+)?real_orders", re.IGNORECASE)
    source = open(live_gateway_module.__file__).read()
    assert not sql_write_pattern.search(source)


def test_no_robinhood_mutating_tool_referenced_in_live_gateway():
    import trading_research.execution.live_gateway as live_gateway_module

    source = open(live_gateway_module.__file__).read().lower()
    for banned in ("place_equity_order", "place_option_order", "cancel_equity_order", "cancel_option_order"):
        assert banned not in source
