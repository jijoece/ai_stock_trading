"""Authoritative per-book daily-loss and drawdown state.

All persisted financial values are Decimal-backed TEXT.  Calculations fail
closed when the start-of-day baseline, complete valuation, or reconciliation
evidence is unavailable.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ..storage import paper_books_repositories as repo
from ..storage.transactions import transaction
from .models import VALUATION_COMPLETE

DAILY_RISK_POLICY_VERSION = "paper-book-daily-risk-v1"
RECONCILIATION_MATCHED = "MATCHED"
SAFETY_PAUSE_DAILY_LOSS = "DAILY_LOSS_LIMIT"
SAFETY_PAUSE_DRAWDOWN = "DRAWDOWN_LIMIT"


class DailyRiskStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class DailyRiskState:
    risk_state_id: str
    book_id: str
    market_date: date
    as_of: datetime
    start_of_day_equity: Decimal
    current_equity: Decimal
    realized_pnl_today: Decimal
    unrealized_pnl_today: Decimal
    total_pnl_today: Decimal
    net_external_cash_flow: Decimal
    daily_loss_fraction: Decimal
    historical_peak_equity: Decimal
    current_drawdown_fraction: Decimal
    valuation_status: str
    source_snapshot_ids: tuple[str, ...]
    reconciliation_status: str
    calculation_policy_version: str
    config_hash: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.created_at.tzinfo is None:
            raise DailyRiskStateError("as_of and created_at must be timezone-aware")
        if self.start_of_day_equity <= 0 or self.historical_peak_equity <= 0:
            raise DailyRiskStateError("equity baselines must be positive")
        if self.daily_loss_fraction != (
            self.current_equity - self.start_of_day_equity - self.net_external_cash_flow
        ) / self.start_of_day_equity:
            raise DailyRiskStateError("daily-loss fraction is inconsistent with authoritative formula")
        if self.total_pnl_today != self.realized_pnl_today + self.unrealized_pnl_today:
            raise DailyRiskStateError("daily P&L components do not reconcile")
        if self.current_drawdown_fraction != (
            self.current_equity - self.historical_peak_equity
        ) / self.historical_peak_equity:
            raise DailyRiskStateError("drawdown fraction is inconsistent with authoritative formula")


def calculate_daily_risk_values(
    *, start_of_day_equity: Decimal, current_equity: Decimal,
    realized_pnl_today: Decimal, unrealized_pnl_today: Decimal,
    net_external_cash_flow: Decimal, historical_peak_equity: Decimal,
) -> dict[str, Decimal]:
    """Pure authoritative formulas used by paper books and backtests."""
    if start_of_day_equity <= 0:
        raise DailyRiskStateError("start_of_day_equity must be known and positive")
    if historical_peak_equity <= 0:
        raise DailyRiskStateError("historical_peak_equity must be known and positive")
    total_pnl_today = current_equity - start_of_day_equity - net_external_cash_flow
    component_total = realized_pnl_today + unrealized_pnl_today
    if component_total != total_pnl_today:
        raise DailyRiskStateError(
            "realized and unrealized daily P&L do not reconcile to the equity/cash-flow change"
        )
    return {
        "total_pnl_today": total_pnl_today,
        "daily_loss_fraction": total_pnl_today / start_of_day_equity,
        "current_drawdown_fraction": (
            current_equity - historical_peak_equity
        ) / historical_peak_equity,
    }


def _snapshot_equity(snapshot: dict) -> Decimal:
    value = snapshot.get("net_liquidation_value_usd")
    if snapshot.get("valuation_status") != VALUATION_COMPLETE or value is None:
        raise DailyRiskStateError("a COMPLETE net-liquidation valuation is required")
    result = Decimal(value)
    if result <= 0:
        raise DailyRiskStateError("net-liquidation value must be positive")
    return result


def _state_id(book_id: str, market_date: date, as_of: datetime, config_hash: str) -> str:
    raw = f"{book_id}:{market_date.isoformat()}:{as_of.isoformat()}:{config_hash}:{DAILY_RISK_POLICY_VERSION}"
    return "pb-risk-state-" + hashlib.sha256(raw.encode()).hexdigest()[:40]


def calculate_and_persist_daily_risk_state(
    conn, *, book_id: str, market_date: date, as_of: datetime,
    config_hash: str, require_reconciled: bool = True,
) -> DailyRiskState:
    """Build and atomically append one risk-state observation from storage."""
    if as_of.tzinfo is None:
        raise DailyRiskStateError("as_of must be timezone-aware")
    if as_of.date() != market_date:
        raise DailyRiskStateError("market_date must equal as_of.date()")

    current = repo.latest_snapshot_before(conn, book_id, as_of)
    if current is None:
        raise DailyRiskStateError("current valuation snapshot is unavailable")
    current_equity = _snapshot_equity(current)

    day_start = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
    baseline = repo.latest_snapshot_before(conn, book_id, day_start)
    if baseline is None:
        # The first complete snapshot on the market date is an admissible SOD
        # baseline only if it is exactly at the day boundary.  This avoids
        # silently treating an intraday loss as the opening equity.
        if current["as_of"] == day_start.isoformat():
            baseline = current
        else:
            raise DailyRiskStateError("start-of-day equity baseline is unavailable")
    start_equity = _snapshot_equity(baseline)

    snapshots = [
        row for row in repo.list_snapshots(conn, book_id)
        if datetime.fromisoformat(row["as_of"]) <= as_of
    ]
    complete_equities = [
        Decimal(row["net_liquidation_value_usd"])
        for row in snapshots
        if row["valuation_status"] == VALUATION_COMPLETE
        and row["net_liquidation_value_usd"] is not None
    ]
    if not complete_equities:
        raise DailyRiskStateError("historical peak equity is unavailable")
    peak_equity = max(complete_equities)

    external_flow = sum(
        (
            Decimal(row["amount_usd"])
            for row in repo.list_cash_ledger_entries(conn, book_id)
            if row["event_type"] == "CASH_ADJUSTMENT"
            and day_start <= datetime.fromisoformat(row["event_timestamp"]) <= as_of
        ),
        Decimal("0"),
    )
    baseline_realized = Decimal(baseline["realized_pnl_usd"])
    current_realized = Decimal(current["realized_pnl_usd"])
    realized_today = current_realized - baseline_realized
    total_today = current_equity - start_equity - external_flow
    # Unrealized P&L is the reconciled residual.  This handles positions
    # opened/closed during the day without double-counting the snapshot's
    # lifetime realized-P&L accumulator.
    unrealized_today = total_today - realized_today
    values = calculate_daily_risk_values(
        start_of_day_equity=start_equity, current_equity=current_equity,
        realized_pnl_today=realized_today, unrealized_pnl_today=unrealized_today,
        net_external_cash_flow=external_flow, historical_peak_equity=peak_equity,
    )

    reconciliations = [
        row for row in repo.list_reconciliations(conn, book_id)
        if datetime.fromisoformat(row["as_of"]) <= as_of
    ]
    reconciliation_status = reconciliations[-1]["status"] if reconciliations else "UNAVAILABLE"
    if require_reconciled and reconciliation_status != RECONCILIATION_MATCHED:
        raise DailyRiskStateError(f"risk state is unreconciled: {reconciliation_status}")

    state = DailyRiskState(
        risk_state_id=_state_id(book_id, market_date, as_of, config_hash),
        book_id=book_id, market_date=market_date, as_of=as_of,
        start_of_day_equity=start_equity, current_equity=current_equity,
        realized_pnl_today=realized_today, unrealized_pnl_today=unrealized_today,
        total_pnl_today=values["total_pnl_today"], net_external_cash_flow=external_flow,
        daily_loss_fraction=values["daily_loss_fraction"], historical_peak_equity=peak_equity,
        current_drawdown_fraction=values["current_drawdown_fraction"],
        valuation_status=current["valuation_status"],
        source_snapshot_ids=(baseline["snapshot_id"], current["snapshot_id"]),
        reconciliation_status=reconciliation_status,
        calculation_policy_version=DAILY_RISK_POLICY_VERSION,
        config_hash=config_hash, created_at=as_of,
    )
    with transaction(conn):
        repo.save_daily_risk_state(conn, state, commit=False)
    return state


def risk_state_from_row(row: dict) -> DailyRiskState:
    return DailyRiskState(
        risk_state_id=row["risk_state_id"], book_id=row["book_id"],
        market_date=date.fromisoformat(row["market_date"]), as_of=datetime.fromisoformat(row["as_of"]),
        start_of_day_equity=Decimal(row["start_of_day_equity"]), current_equity=Decimal(row["current_equity"]),
        realized_pnl_today=Decimal(row["realized_pnl_today"]), unrealized_pnl_today=Decimal(row["unrealized_pnl_today"]),
        total_pnl_today=Decimal(row["total_pnl_today"]), net_external_cash_flow=Decimal(row["net_external_cash_flow"]),
        daily_loss_fraction=Decimal(row["daily_loss_fraction"]), historical_peak_equity=Decimal(row["historical_peak_equity"]),
        current_drawdown_fraction=Decimal(row["current_drawdown_fraction"]), valuation_status=row["valuation_status"],
        source_snapshot_ids=tuple(json.loads(row["source_snapshot_ids_json"])),
        reconciliation_status=row["reconciliation_status"],
        calculation_policy_version=row["calculation_policy_version"], config_hash=row["config_hash"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )
