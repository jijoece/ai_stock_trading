"""Tests for the operator-supplied `--paper-books-config` override on
`paper-book-run-cycle` / `paper-book-integrate-cycle` / `paper-book-lifecycle-run`
(Milestone 22 operational-validation follow-up). The tracked
`config/paper_books.yaml` ships `paper_books.enabled: false` and is never
edited by this feature — an operator who wants to exercise the live CLI path
supplies an explicit external file instead. The override must fail closed
(never silently fall back to the tracked default) on a missing file,
malformed YAML, or an otherwise-invalid configuration.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from trading_research.paper_books import cli_support


def _valid_enabled_config() -> dict:
    return {
        "version": 1,
        "paper_books": {
            "enabled": True,
            "books": {
                "baseline": {"enabled": True, "book_id": "BASELINE", "starting_cash_usd": "100000.00"},
                "enhanced": {"enabled": False, "book_id": "ENHANCED", "starting_cash_usd": "100000.00"},
            },
            "execution": {
                "provider": "local_simulated", "allow_external_paper_broker": False, "allow_live_broker": False,
            },
            "risk": {
                "max_position_weight": "0.50", "max_order_notional_usd": "100000.00",
                "max_daily_new_notional_usd": "100000.00", "minimum_cash_buffer_weight": "0.05",
                "max_open_positions": 20, "max_symbol_concentration_weight": "0.50",
                "reject_stale_market_price_seconds": 900,
            },
            "valuation": {
                "price_source": "evidence_snapshot", "maximum_price_age_seconds": 900,
                "missing_price_policy": "MARK_UNVALUED",
            },
        },
    }


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def db_path(tmp_dir):
    return tmp_dir / "cli_override_test.db"


def _run_cycle_kwargs(db_path: Path, *, config_path=None) -> dict:
    return dict(
        db_path=db_path, cycle_id="cyc-override-1", experiment_policy="BASELINE_ONLY", symbol="AAPL",
        quantity_hint="1", reference_price="150.00", bid="149.90", ask="150.10",
        recommendation_id_baseline="rec-override-1", config_path=config_path,
    )


def test_no_override_uses_tracked_disabled_config(db_path):
    outcome = cli_support.paper_book_run_cycle_cli(**_run_cycle_kwargs(db_path))
    assert "error" in outcome
    assert "paper_books.enabled is false" in outcome["error"]


def test_external_enabled_config_reaches_execution_path(tmp_dir, db_path):
    cfg_path = tmp_dir / "paper-books-local.yaml"
    cfg_path.write_text(yaml.safe_dump(_valid_enabled_config()))

    outcome = cli_support.paper_book_run_cycle_cli(**_run_cycle_kwargs(db_path, config_path=cfg_path))
    assert "error" not in outcome
    assert outcome["results"]["BASELINE"]["risk_decision"] == "APPROVED"
    assert outcome["results"]["BASELINE"]["paper_order_intent_id"]


def test_missing_override_file_fails_closed(db_path, tmp_dir):
    missing_path = tmp_dir / "does-not-exist.yaml"
    outcome = cli_support.paper_book_run_cycle_cli(**_run_cycle_kwargs(db_path, config_path=missing_path))
    assert "error" in outcome
    assert "does not exist" in outcome["error"]


def test_malformed_yaml_override_fails_closed(db_path, tmp_dir):
    cfg_path = tmp_dir / "malformed.yaml"
    cfg_path.write_text("paper_books: [this is not, a mapping: :::")
    outcome = cli_support.paper_book_run_cycle_cli(**_run_cycle_kwargs(db_path, config_path=cfg_path))
    assert "error" in outcome


def test_invalid_config_override_fails_closed(db_path, tmp_dir):
    invalid = _valid_enabled_config()
    invalid["paper_books"]["execution"]["allow_live_broker"] = True
    cfg_path = tmp_dir / "invalid.yaml"
    cfg_path.write_text(yaml.safe_dump(invalid))

    outcome = cli_support.paper_book_run_cycle_cli(**_run_cycle_kwargs(db_path, config_path=cfg_path))
    assert "error" in outcome
    assert "allow_live_broker" in outcome["error"]


def test_explicit_invalid_override_does_not_fall_back_to_tracked_default(db_path, tmp_dir):
    missing_path = tmp_dir / "does-not-exist.yaml"
    outcome = cli_support.paper_book_run_cycle_cli(**_run_cycle_kwargs(db_path, config_path=missing_path))
    # A silent fallback would surface the tracked default's own
    # "paper_books.enabled is false" error instead of the override-specific one.
    assert "paper_books.enabled is false" not in outcome.get("error", "")
    assert "does not exist" in outcome["error"]


def test_all_three_cli_commands_use_the_same_loader(tmp_dir, db_path):
    cfg_path = tmp_dir / "paper-books-local.yaml"
    cfg_path.write_text(yaml.safe_dump(_valid_enabled_config()))
    missing_path = tmp_dir / "does-not-exist.yaml"

    run_cycle_outcome = cli_support.paper_book_run_cycle_cli(**_run_cycle_kwargs(db_path, config_path=missing_path))
    integrate_outcome = cli_support.paper_book_integrate_cycle_cli(
        db_path, cycle_id="cyc-x", experiment_policy="BOTH_SEPARATE_PAPER_BOOKS", config_path=missing_path,
    )
    lifecycle_outcome = cli_support.paper_book_lifecycle_run_cli(
        db_path, as_of=datetime(2026, 7, 19, tzinfo=timezone.utc), config_path=missing_path,
    )

    for outcome in (run_cycle_outcome, integrate_outcome, lifecycle_outcome):
        assert "error" in outcome
        assert "does not exist" in outcome["error"]


def test_reconcile_cli_uses_the_same_loader(tmp_dir, db_path):
    cfg_path = tmp_dir / "paper-books-local.yaml"
    cfg_path.write_text(yaml.safe_dump(_valid_enabled_config()))
    missing_path = tmp_dir / "does-not-exist.yaml"

    cli_support.paper_book_run_cycle_cli(**_run_cycle_kwargs(db_path, config_path=cfg_path))
    reconcile_outcome = cli_support.paper_book_reconcile_cli(
        db_path, "BASELINE", config_path=cfg_path,
    )
    assert "error" not in reconcile_outcome

    missing_outcome = cli_support.paper_book_reconcile_cli(db_path, "BASELINE", config_path=missing_path)
    assert "does not exist" in missing_outcome["error"]


def test_tracked_config_file_remains_unchanged():
    cfg, error = cli_support._load_config_or_error()
    assert error is None
    assert cfg.enabled is False
