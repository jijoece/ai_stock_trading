"""Tests for paper_books/config.py (docs/milestone-8.md Step 3)."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from trading_research.paper_books.config import PaperBooksConfigError, load_paper_books_config


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "paper_books.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _valid_config() -> dict:
    return {
        "version": 1,
        "paper_books": {
            "enabled": False,
            "books": {
                "baseline": {"enabled": True, "book_id": "BASELINE", "starting_cash_usd": "100000.00"},
                "enhanced": {"enabled": False, "book_id": "ENHANCED", "starting_cash_usd": "100000.00"},
            },
            "execution": {
                "provider": "local_simulated", "allow_external_paper_broker": False, "allow_live_broker": False,
            },
            "risk": {
                "max_position_weight": "0.10", "max_order_notional_usd": "1000.00",
                "max_daily_new_notional_usd": "5000.00", "minimum_cash_buffer_weight": "0.10",
                "max_open_positions": 20, "max_symbol_concentration_weight": "0.10",
                "reject_stale_market_price_seconds": 900,
            },
            "valuation": {
                "price_source": "evidence_snapshot", "maximum_price_age_seconds": 900,
                "missing_price_policy": "MARK_UNVALUED",
            },
        },
    }


def test_shipped_config_loads_and_is_disabled_by_default():
    cfg = load_paper_books_config()
    assert cfg.enabled is False
    assert cfg.enhanced.enabled is False
    assert cfg.baseline.enabled is True
    assert cfg.execution.allow_live_broker is False
    assert cfg.execution.allow_external_paper_broker is False
    assert cfg.soak_campaign.enabled is False


def test_valid_config_round_trips(tmp_path):
    path = _write(tmp_path, _valid_config())
    cfg = load_paper_books_config(path)
    assert cfg.baseline.starting_cash_usd == pytest.approx(100000.00)
    assert cfg.config_hash


def test_allow_live_broker_true_fails_closed(tmp_path):
    data = _valid_config()
    data["paper_books"]["execution"]["allow_live_broker"] = True
    path = _write(tmp_path, data)
    with pytest.raises(PaperBooksConfigError):
        load_paper_books_config(path)


def test_duplicate_book_id_fails_closed(tmp_path):
    data = _valid_config()
    data["paper_books"]["books"]["enhanced"]["book_id"] = "BASELINE"
    path = _write(tmp_path, data)
    with pytest.raises(PaperBooksConfigError):
        load_paper_books_config(path)


def test_invalid_book_id_fails_closed(tmp_path):
    data = _valid_config()
    data["paper_books"]["books"]["baseline"]["book_id"] = "NOT_A_REAL_BOOK"
    path = _write(tmp_path, data)
    with pytest.raises(PaperBooksConfigError):
        load_paper_books_config(path)


def test_unknown_top_level_key_fails_closed(tmp_path):
    data = _valid_config()
    data["unexpected_top_level_key"] = "value"
    path = _write(tmp_path, data)
    with pytest.raises(PaperBooksConfigError):
        load_paper_books_config(path)


def test_unknown_nested_key_fails_closed(tmp_path):
    data = _valid_config()
    data["paper_books"]["risk"]["unexpected_key"] = "value"
    path = _write(tmp_path, data)
    with pytest.raises(PaperBooksConfigError):
        load_paper_books_config(path)


def test_percentage_out_of_bounds_fails_closed(tmp_path):
    data = _valid_config()
    data["paper_books"]["risk"]["max_position_weight"] = "1.5"
    path = _write(tmp_path, data)
    with pytest.raises(PaperBooksConfigError):
        load_paper_books_config(path)


def test_unknown_execution_provider_fails_closed(tmp_path):
    data = _valid_config()
    data["paper_books"]["execution"]["provider"] = "totally_unknown_provider"
    path = _write(tmp_path, data)
    with pytest.raises(PaperBooksConfigError):
        load_paper_books_config(path)


def test_external_paper_broker_provider_requires_explicit_allow(tmp_path):
    data = _valid_config()
    data["paper_books"]["execution"]["provider"] = "external_paper_broker"
    data["paper_books"]["execution"]["allow_external_paper_broker"] = False
    path = _write(tmp_path, data)
    with pytest.raises(PaperBooksConfigError):
        load_paper_books_config(path)


def test_negative_starting_cash_fails_closed(tmp_path):
    data = _valid_config()
    data["paper_books"]["books"]["baseline"]["starting_cash_usd"] = "-100.00"
    path = _write(tmp_path, data)
    with pytest.raises(PaperBooksConfigError):
        load_paper_books_config(path)


def test_missing_price_policy_unknown_value_fails_closed(tmp_path):
    data = _valid_config()
    data["paper_books"]["valuation"]["missing_price_policy"] = "SUBSTITUTE_ZERO"
    path = _write(tmp_path, data)
    with pytest.raises(PaperBooksConfigError):
        load_paper_books_config(path)


def test_is_book_enabled_requires_both_global_and_book_flag(tmp_path):
    data = _valid_config()
    data["paper_books"]["enabled"] = True
    data["paper_books"]["books"]["enhanced"]["enabled"] = False
    path = _write(tmp_path, data)
    cfg = load_paper_books_config(path)
    assert cfg.is_book_enabled("BASELINE") is True
    assert cfg.is_book_enabled("ENHANCED") is False


def test_soak_campaign_config_loads_and_contributes_to_hash(tmp_path):
    data = _valid_config()
    data["paper_books"]["soak_campaign"] = {
        "enabled": True, "minimum_market_days": 5, "minimum_completed_cycles": 10,
        "minimum_successful_real_provider_cycles": 5, "maximum_unresolved_warnings": 0,
        "stop_on_blocker": True,
    }
    cfg = load_paper_books_config(_write(tmp_path, data))
    first_hash = cfg.config_hash
    assert cfg.soak_campaign.enabled is True
    data["paper_books"]["soak_campaign"]["minimum_market_days"] = 6
    assert load_paper_books_config(_write(tmp_path, data)).config_hash != first_hash


def test_soak_campaign_unknown_key_fails_closed(tmp_path):
    data = _valid_config()
    data["paper_books"]["soak_campaign"] = {"enabled": False, "typo": 1}
    with pytest.raises(PaperBooksConfigError, match="unknown keys"):
        load_paper_books_config(_write(tmp_path, data))


def test_soak_campaign_booleans_are_strict(tmp_path):
    data = _valid_config()
    data["paper_books"]["soak_campaign"] = {"enabled": "false"}
    with pytest.raises(PaperBooksConfigError, match="must be a boolean"):
        load_paper_books_config(_write(tmp_path, data))


def test_soak_campaign_positive_thresholds_fail_closed(tmp_path):
    data = _valid_config()
    data["paper_books"]["soak_campaign"] = {"enabled": False, "minimum_market_days": 0}
    with pytest.raises(PaperBooksConfigError, match="> 0"):
        load_paper_books_config(_write(tmp_path, data))
