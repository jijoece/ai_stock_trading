import pytest

from trading_research.runtime.paper_runtime_config import (
    PaperRuntimeConfigError,
    load_paper_runtime_config,
)

VALID_YAML = """
version: 1
paper_runtime:
  protocol_version: paper-runtime.v1
  transport: stdio
  command: [python3, -m, trading_paper_runtime]
  startup_timeout_seconds: 15
  request_timeout_seconds: 30
paper_broker:
  provider: alpaca
  mode: paper
  real_money_enabled: false
  asset_types: [equity]
  allowed_sides: [BUY]
  allowed_order_types: [LIMIT, MARKET]
  allow_fractional: false
  allow_shorting: false
  allow_margin: false
  allow_extended_hours: false
order_monitoring:
  poll_interval_seconds: 10
  max_poll_attempts: 30
  stale_order_minutes: 390
evaluation:
  benchmark: SPY
  horizons_trading_days: [1, 5, 10, 20, 60]
"""


def _write(tmp_path, content: str):
    path = tmp_path / "paper_runtime.yaml"
    path.write_text(content)
    return path


def test_default_repo_config_loads_and_validates():
    config = load_paper_runtime_config()
    assert config.broker_mode == "paper"
    assert config.real_money_enabled is False
    assert config.evaluation_benchmark == "SPY"
    assert config.evaluation_horizons_trading_days == (1, 5, 10, 20, 60)


def test_valid_custom_config_loads(tmp_path):
    path = _write(tmp_path, VALID_YAML)
    config = load_paper_runtime_config(path)
    assert config.broker_provider == "alpaca"


def test_unrecognized_broker_mode_fails_closed(tmp_path):
    path = _write(tmp_path, VALID_YAML.replace("mode: paper", "mode: live"))
    with pytest.raises(PaperRuntimeConfigError):
        load_paper_runtime_config(path)


def test_real_money_enabled_true_fails_closed(tmp_path):
    path = _write(tmp_path, VALID_YAML.replace("real_money_enabled: false", "real_money_enabled: true"))
    with pytest.raises(PaperRuntimeConfigError):
        load_paper_runtime_config(path)


@pytest.mark.parametrize(
    "field", ["allow_fractional", "allow_shorting", "allow_margin", "allow_extended_hours"],
)
def test_disallowed_capability_flags_fail_closed(tmp_path, field):
    path = _write(tmp_path, VALID_YAML.replace(f"{field}: false", f"{field}: true"))
    with pytest.raises(PaperRuntimeConfigError):
        load_paper_runtime_config(path)


def test_missing_top_level_section_fails_closed(tmp_path):
    broken = VALID_YAML.replace("evaluation:\n  benchmark: SPY\n  horizons_trading_days: [1, 5, 10, 20, 60]\n", "")
    path = _write(tmp_path, broken)
    with pytest.raises(PaperRuntimeConfigError):
        load_paper_runtime_config(path)


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(PaperRuntimeConfigError):
        load_paper_runtime_config(tmp_path / "does-not-exist.yaml")
