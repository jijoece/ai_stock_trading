"""Read-only operational status and provider-partitioned health queries."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from dashboard.models.view_models import (
    ProviderHealthSummary,
    SystemHealthView,
    SystemStatusSummary,
)
from dashboard.services.database import DashboardDatabaseError, connect_read_only
from trading_research.shadow.model_provider_health import (
    AUTHENTICATION_FAILURE_CODES,
    CONFIGURATION_FAILURE_CODES,
    FIXTURE_MODEL_PROVIDERS,
    PRODUCTION_MODEL_PROVIDERS,
    QUOTA_FAILURE_CODES,
    RATE_LIMIT_CODES,
    TIMEOUT_CODES,
)


MAX_PROVIDER_EVENTS = 2000


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _json_strings(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return ()
    return tuple(str(item)[:240] for item in parsed) if isinstance(parsed, list) else ()


def _streaks(successes_newest_first: list[bool], *, production: bool) -> tuple[int, int]:
    if not successes_newest_first or not production:
        return 0, 0
    latest = successes_newest_first[0]
    length = 0
    for success in successes_newest_first:
        if success is not latest:
            break
        length += 1
    return (0, length) if latest else (length, 0)


def _provider_status(success_rate: float | None, *, production: bool) -> str:
    if not production:
        return "NON_PRODUCTION"
    if success_rate is None:
        return "INSUFFICIENT_DATA"
    if success_rate >= 0.8:
        return "HEALTHY"
    if success_rate >= 0.5:
        return "DEGRADED"
    return "UNAVAILABLE"


class HealthService:
    def __init__(self, database_path: str | Path | None = None):
        self._database_path = database_path

    def load(self) -> SystemHealthView:
        try:
            with connect_read_only(self._database_path) as connection:
                status = self._status(connection)
                evidence = self._evidence_health(connection)
                models = self._model_health(connection)
        except sqlite3.Error as exc:
            raise DashboardDatabaseError("Dashboard system-health data is unavailable.") from exc
        return SystemHealthView(status=status, providers=tuple((*evidence, *models)))

    @staticmethod
    def _status(connection: sqlite3.Connection) -> SystemStatusSummary:
        pause = connection.execute(
            "SELECT state, created_at FROM shadow_pause_state WHERE is_current = 1 ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        activation = connection.execute(
            "SELECT new_state, created_at FROM paper_recurring_activation_events ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        shadow = connection.execute(
            "SELECT status, actual_finish_at, created_at FROM shadow_scheduler_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        recurring = connection.execute(
            "SELECT status, ended_at, created_at FROM paper_recurring_scheduler_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        latest_shadow_success = connection.execute(
            "SELECT actual_finish_at FROM shadow_scheduler_runs WHERE status = 'COMPLETED' "
            "AND actual_finish_at IS NOT NULL ORDER BY actual_finish_at DESC LIMIT 1"
        ).fetchone()
        latest_recurring_success = connection.execute(
            "SELECT ended_at FROM paper_recurring_scheduler_runs WHERE status = 'COMPLETED' "
            "AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1"
        ).fetchone()
        run_summary = connection.execute(
            "SELECT health_status, policy_version, created_at FROM shadow_run_summaries ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        hysteresis = connection.execute(
            "SELECT * FROM shadow_health_hysteresis_state ORDER BY last_evaluated_at DESC LIMIT 1"
        ).fetchone()
        budget = connection.execute(
            "SELECT status, created_at FROM shadow_budget_reservations ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        safety_rows = connection.execute("""
            SELECT latest.book_id, latest.reason_code FROM paper_book_safety_events latest
            WHERE latest.created_at = (
                SELECT MAX(candidate.created_at) FROM paper_book_safety_events candidate
                WHERE candidate.book_id = latest.book_id
            ) AND latest.state = 'PAUSED'
            ORDER BY latest.book_id
        """).fetchall()

        success_times = tuple(filter(None, (
            _datetime(latest_shadow_success["actual_finish_at"]) if latest_shadow_success else None,
            _datetime(latest_recurring_success["ended_at"]) if latest_recurring_success else None,
        )))
        as_of_times = tuple(filter(None, (
            _datetime(pause["created_at"]) if pause else None,
            _datetime(activation["created_at"]) if activation else None,
            _datetime(shadow["created_at"]) if shadow else None,
            _datetime(recurring["created_at"]) if recurring else None,
            _datetime(hysteresis["last_evaluated_at"]) if hysteresis else None,
        )))
        return SystemStatusSummary(
            as_of=max(as_of_times) if as_of_times else None,
            shadow_pause_state=pause["state"] if pause else None,
            recurring_activation_state=activation["new_state"] if activation else None,
            latest_shadow_scheduler_status=shadow["status"] if shadow else None,
            latest_recurring_scheduler_status=recurring["status"] if recurring else None,
            latest_successful_run_at=max(success_times) if success_times else None,
            health_status=run_summary["health_status"] if run_summary else None,
            hysteresis_status=hysteresis["decision"] if hysteresis else None,
            hysteresis_reasons=_json_strings(hysteresis["reasons_json"] if hysteresis else None),
            active_safety_pauses=tuple(
                f"{row['book_id']}: {row['reason_code']}" for row in safety_rows
            ),
            budget_status=budget["status"] if budget else None,
            active_policy_version=(hysteresis["policy_version"] if hysteresis else
                                   (run_summary["policy_version"] if run_summary else None)),
            active_policy_hash=hysteresis["policy_hash"] if hysteresis else None,
        )

    @staticmethod
    def _evidence_health(connection: sqlite3.Connection) -> tuple[ProviderHealthSummary, ...]:
        snapshots = connection.execute("""
            SELECT snapshot.* FROM evidence_provider_health_snapshots snapshot
            WHERE snapshot.created_at = (
                SELECT MAX(latest.created_at) FROM evidence_provider_health_snapshots latest
                WHERE latest.provider = snapshot.provider
            ) ORDER BY snapshot.provider
        """).fetchall()
        requests = connection.execute("""
            SELECT provider, success, error_code, transport_failure_category, rate_limited,
                   retry_count, created_at
            FROM evidence_provider_requests ORDER BY created_at DESC LIMIT ?
        """, (MAX_PROVIDER_EVENTS,)).fetchall()
        provenance_rows = connection.execute("""
            SELECT provider_name, provider_mode, is_fixture, is_real, created_at
            FROM research_cycle_provider_provenance ORDER BY created_at DESC LIMIT ?
        """, (MAX_PROVIDER_EVENTS,)).fetchall()
        modes: dict[str, tuple[str, bool]] = {}
        for row in provenance_rows:
            modes.setdefault(row["provider_name"], (row["provider_mode"], bool(row["is_real"] and not row["is_fixture"])))
        grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in requests:
            grouped[row["provider"]].append(row)

        result: list[ProviderHealthSummary] = []
        for snapshot in snapshots:
            provider = snapshot["provider"]
            rows = grouped.get(provider, [])
            mode, production = modes.get(provider, ("persisted-request", True))
            failure_streak, recovery_streak = _streaks(
                [bool(row["success"]) for row in rows], production=production
            )
            latest_failure = next((row["error_code"] for row in rows if not row["success"]), None)
            total = int(snapshot["total_requests"])
            successes = int(snapshot["success_count"])
            result.append(ProviderHealthSummary(
                provider_kind="EVIDENCE", provider=provider, model=None, mode=mode,
                is_production=production,
                status=snapshot["status"] if production else "NON_PRODUCTION",
                window_start=_datetime(snapshot["window_start"]), window_end=_datetime(snapshot["window_end"]),
                total_requests=total, successful_requests=successes,
                success_rate=(successes / total) if total else None,
                timeout_count=int(snapshot["timeout_count"]),
                rate_limited_count=int(snapshot["rate_limited_count"]),
                average_latency_ms=snapshot["average_latency_ms"], p95_latency_ms=snapshot["p95_latency_ms"],
                latest_error_code=latest_failure, failure_streak=failure_streak, recovery_streak=recovery_streak,
                authentication_failures=sum(row["transport_failure_category"] == "AUTHENTICATION_FAILURE" for row in rows),
                configuration_failures=sum(row["transport_failure_category"] == "CONFIGURATION_ERROR" for row in rows),
                timeout_failures=sum(row["transport_failure_category"] == "TIMEOUT" for row in rows),
                rate_limit_failures=sum(bool(row["rate_limited"]) for row in rows),
                quota_failures=sum(row["error_code"] == "QuotaExceededError" for row in rows),
            ))
        return tuple(result)

    @staticmethod
    def _model_health(connection: sqlite3.Connection) -> tuple[ProviderHealthSummary, ...]:
        rows = connection.execute("""
            SELECT a.attempt_id, a.success, a.failure_code, a.created_at,
                   run.provider, run.model_name, run.run_mode
            FROM research_attempts a
            JOIN research_committee_runs run ON run.research_run_id = a.research_run_id
            ORDER BY a.created_at DESC LIMIT ?
        """, (MAX_PROVIDER_EVENTS,)).fetchall()
        grouped: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            grouped[(row["provider"], row["model_name"], row["run_mode"])].append(row)

        result: list[ProviderHealthSummary] = []
        for (provider, model, mode), partition_rows in sorted(grouped.items()):
            production = provider in PRODUCTION_MODEL_PROVIDERS
            if provider in FIXTURE_MODEL_PROVIDERS:
                production = False
            successes = sum(bool(row["success"]) for row in partition_rows)
            total = len(partition_rows)
            success_rate = (successes / total) if total else None
            failure_streak, recovery_streak = _streaks(
                [bool(row["success"]) for row in partition_rows], production=production
            )
            codes = [
                row["failure_code"] for row in partition_rows
                if not row["success"] and row["failure_code"]
            ]
            result.append(ProviderHealthSummary(
                provider_kind="MODEL", provider=provider, model=model, mode=mode,
                is_production=production, status=_provider_status(success_rate, production=production),
                window_start=_datetime(partition_rows[-1]["created_at"]),
                window_end=_datetime(partition_rows[0]["created_at"]),
                total_requests=total, successful_requests=successes, success_rate=success_rate,
                timeout_count=sum(code in TIMEOUT_CODES for code in codes),
                rate_limited_count=sum(code in RATE_LIMIT_CODES for code in codes),
                average_latency_ms=None, p95_latency_ms=None,
                latest_error_code=next((code for code in codes), None),
                failure_streak=failure_streak, recovery_streak=recovery_streak,
                authentication_failures=sum(code in AUTHENTICATION_FAILURE_CODES for code in codes),
                configuration_failures=sum(code in CONFIGURATION_FAILURE_CODES for code in codes),
                timeout_failures=sum(code in TIMEOUT_CODES for code in codes),
                rate_limit_failures=sum(code in RATE_LIMIT_CODES for code in codes),
                quota_failures=sum(code in QUOTA_FAILURE_CODES for code in codes),
            ))
        return tuple(result)
