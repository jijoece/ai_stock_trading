from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from trading_research.storage import paper_books_repositories as repo
from trading_research.storage.database import SQLITE_BUSY_TIMEOUT_MS, connect


NOW = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)


def _campaign():
    return {
        "campaign_id": "sqlite-campaign", "manifest_hash": "manifest", "config_hash": "config",
        "start_as_of": NOW, "end_as_of": NOW, "requested_date_count": 1, "requested_cycle_count": 0,
        "status": "DEFINED", "created_at": NOW,
    }


def _attempt():
    return {
        "campaign_attempt_id": "attempt-1", "campaign_id": "sqlite-campaign",
        "manifest_hash": "manifest", "config_hash": "config", "attempt_number": 1,
        "continue_after_blocker": False, "status": "RUNNING", "started_at": NOW, "created_at": NOW,
    }


def test_campaign_connection_hardening_and_transaction_control():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "campaign.db"
        conn = connect(path)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == SQLITE_BUSY_TIMEOUT_MS
        repo.save_soak_campaign(conn, _campaign())
        repo.save_soak_campaign_attempt(conn, _attempt())
        conn.execute("BEGIN")
        repo.save_soak_campaign_attempt_day(conn, {
            "campaign_attempt_id": "attempt-1", "campaign_id": "sqlite-campaign", "as_of": NOW,
            "requested_cycle_ids": [], "controlled_readiness_status": "READY", "day_status": "COMPLETED",
            "created_at": NOW,
        }, commit=False)
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM paper_soak_campaign_attempt_days").fetchone()[0] == 0
        conn.close()


def test_duplicate_attempt_identity_is_not_inserted_twice():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "campaign.db"
        first = connect(path)
        second = connect(path)
        repo.save_soak_campaign(first, _campaign())
        assert repo.save_soak_campaign_attempt(first, _attempt()) is True
        assert repo.save_soak_campaign_attempt(second, _attempt()) is False
        assert second.execute("SELECT COUNT(*) FROM paper_soak_campaign_attempts").fetchone()[0] == 1
        first.close()
        second.close()
