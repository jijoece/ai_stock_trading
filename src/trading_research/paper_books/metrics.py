"""Per-book performance metrics (docs/milestone-8.md Step 19).

Every metric is computed only when its required data is available within
the explicit `[window_start, window_end]` sample window — missing data stays
`None`, never a fabricated zero. No Sharpe or other annualized ratio is
computed (explicitly out of scope). All money arithmetic is Decimal.
"""
from __future__ import annotations

import statistics
from datetime import datetime
from decimal import Decimal

from ..storage import paper_books_repositories as repo

METRIC_VERSION = "paper-books-metrics-v1"


def _in_window(iso_ts: str, window_start: datetime, window_end: datetime) -> bool:
    dt = datetime.fromisoformat(iso_ts)
    return window_start <= dt <= window_end


def _compute_trade_pnls(fills: list[dict]) -> list[Decimal]:
    """Recomputes one realized-P&L value per SELL fill via FIFO consumption
    over the fill history — self-contained (does not depend on any
    previously-persisted realized_pnl side channel), mirroring
    `reconciliation.py`'s own independent-recomputation pattern."""
    by_symbol: dict[str, list[dict]] = {}
    for f in fills:
        by_symbol.setdefault(f["symbol"], []).append(f)

    trade_pnls: list[Decimal] = []
    for symbol, flist in by_symbol.items():
        lots: list[list[Decimal]] = []  # [remaining_qty, cost_per_share]
        for f in sorted(flist, key=lambda row: row["fill_timestamp"]):
            qty = Decimal(f["fill_quantity"])
            price = Decimal(f["fill_price"])
            if f["side"] == "BUY":
                lots.append([qty, price])
            else:
                remaining_to_sell = qty
                pnl = Decimal("0")
                for lot in lots:
                    if remaining_to_sell <= 0:
                        break
                    consumed = min(lot[0], remaining_to_sell)
                    pnl += (price - lot[1]) * consumed
                    lot[0] -= consumed
                    remaining_to_sell -= consumed
                trade_pnls.append(pnl)
    return trade_pnls


def compute_book_metrics(conn, book_id: str, window_start: datetime, window_end: datetime) -> dict:
    book = repo.load_book(conn, book_id)
    if book is None:
        raise ValueError(f"unknown book_id {book_id!r}")

    all_snapshots = repo.list_snapshots(conn, book_id)
    snapshots = [s for s in all_snapshots if _in_window(s["as_of"], window_start, window_end)]
    snapshots.sort(key=lambda s: s["as_of"])

    all_fills = repo.list_fills(conn, book_id)
    fills = [f for f in all_fills if _in_window(f["fill_timestamp"], window_start, window_end)]

    metrics: dict = {
        "book_id": book_id, "metric_version": METRIC_VERSION,
        "window_start": window_start.isoformat(), "window_end": window_end.isoformat(),
        "starting_cash_usd": book.starting_cash_usd,
    }

    last_snapshot = snapshots[-1] if snapshots else None
    metrics["ending_cash_usd"] = (
        Decimal(last_snapshot["cash_available_usd"]) + Decimal(last_snapshot["cash_reserved_usd"])
        if last_snapshot else None
    )
    metrics["net_liquidation_value_usd"] = (
        Decimal(last_snapshot["net_liquidation_value_usd"])
        if last_snapshot and last_snapshot["net_liquidation_value_usd"] is not None else None
    )
    metrics["realized_pnl_usd"] = (
        Decimal(last_snapshot["realized_pnl_usd"]) if last_snapshot else None
    )
    metrics["unrealized_pnl_usd"] = (
        Decimal(last_snapshot["unrealized_pnl_usd"])
        if last_snapshot and last_snapshot["unrealized_pnl_usd"] is not None else None
    )

    nlv = metrics["net_liquidation_value_usd"]
    starting_cash = book.starting_cash_usd
    metrics["total_return"] = (
        (nlv - starting_cash) / starting_cash if nlv is not None and starting_cash > 0 else None
    )

    daily_returns: list[Decimal] = []
    for prev, curr in zip(snapshots, snapshots[1:]):
        prev_nlv = Decimal(prev["net_liquidation_value_usd"]) if prev["net_liquidation_value_usd"] is not None else None
        curr_nlv = Decimal(curr["net_liquidation_value_usd"]) if curr["net_liquidation_value_usd"] is not None else None
        if prev_nlv is not None and curr_nlv is not None and prev_nlv > 0:
            daily_returns.append((curr_nlv - prev_nlv) / prev_nlv)

    metrics["daily_returns"] = daily_returns if daily_returns else None
    if daily_returns:
        cumulative = Decimal("1")
        for r in daily_returns:
            cumulative *= (Decimal("1") + r)
        metrics["cumulative_return"] = cumulative - Decimal("1")
    else:
        metrics["cumulative_return"] = None

    metrics["volatility"] = (
        Decimal(str(statistics.stdev([float(r) for r in daily_returns]))) if len(daily_returns) >= 2 else None
    )

    equity_curve = [
        Decimal(s["net_liquidation_value_usd"]) for s in snapshots if s["net_liquidation_value_usd"] is not None
    ]
    if equity_curve:
        peak = equity_curve[0]
        max_dd = Decimal("0")
        for value in equity_curve:
            peak = max(peak, value)
            if peak > 0:
                dd = (value - peak) / peak
                max_dd = min(max_dd, dd)
        metrics["maximum_drawdown"] = max_dd
    else:
        metrics["maximum_drawdown"] = None

    metrics["trade_count"] = len(fills)
    metrics["turnover"] = (
        sum((Decimal(f["fill_price"]) * Decimal(f["fill_quantity"]) for f in fills), Decimal("0")) / nlv
        if fills and nlv is not None and nlv > 0 else None
    )
    metrics["fees_usd"] = sum((Decimal(f["fees_usd"]) for f in fills), Decimal("0")) if fills else None
    metrics["slippage_usd"] = sum((Decimal(f["slippage_usd"]) for f in fills), Decimal("0")) if fills else None

    trade_pnls = _compute_trade_pnls(fills)
    if trade_pnls:
        wins = [p for p in trade_pnls if p > 0]
        losses = [p for p in trade_pnls if p < 0]
        metrics["win_rate"] = Decimal(len(wins)) / Decimal(len(trade_pnls))
        metrics["average_win"] = (sum(wins, Decimal("0")) / Decimal(len(wins))) if wins else None
        metrics["average_loss"] = (sum(losses, Decimal("0")) / Decimal(len(losses))) if losses else None
        loss_sum = abs(sum(losses, Decimal("0")))
        metrics["profit_factor"] = (sum(wins, Decimal("0")) / loss_sum) if loss_sum > 0 else None
    else:
        metrics["win_rate"] = None
        metrics["average_win"] = None
        metrics["average_loss"] = None
        metrics["profit_factor"] = None

    metrics["cash_utilization"] = (
        (nlv - Decimal(last_snapshot["cash_available_usd"]) - Decimal(last_snapshot["cash_reserved_usd"])) / nlv
        if last_snapshot and nlv is not None and nlv > 0 else None
    )

    gross_values = [
        Decimal(s["gross_market_value_usd"]) for s in snapshots if s["gross_market_value_usd"] is not None
    ]
    metrics["average_gross_exposure_usd"] = (
        sum(gross_values, Decimal("0")) / Decimal(len(gross_values)) if gross_values else None
    )

    max_concentration = None
    unvalued_rates = []
    stale_rates = []
    for s in snapshots:
        if s["position_count"] > 0:
            unvalued_rates.append(Decimal(s["unvalued_position_count"]) / Decimal(s["position_count"]))
            stale_rates.append(Decimal(s["stale_position_count"]) / Decimal(s["position_count"]))
        snap_positions = repo.list_snapshot_positions(conn, book_id, s["snapshot_id"])
        snap_nlv = Decimal(s["net_liquidation_value_usd"]) if s["net_liquidation_value_usd"] is not None else None
        if snap_nlv and snap_nlv > 0:
            for p in snap_positions:
                if p["market_value_usd"] is not None:
                    weight = Decimal(p["market_value_usd"]) / snap_nlv
                    max_concentration = weight if max_concentration is None else max(max_concentration, weight)

    metrics["maximum_position_concentration"] = max_concentration
    metrics["unvalued_position_rate"] = (
        sum(unvalued_rates, Decimal("0")) / Decimal(len(unvalued_rates)) if unvalued_rates else None
    )
    metrics["stale_valuation_rate"] = (
        sum(stale_rates, Decimal("0")) / Decimal(len(stale_rates)) if stale_rates else None
    )

    return metrics


def save_book_metrics(conn, book_id: str, window_start: datetime, window_end: datetime, *, clock) -> str:
    metrics = compute_book_metrics(conn, book_id, window_start, window_end)
    metrics_id = f"pb-metrics-{book_id}-{window_start.isoformat()}-{window_end.isoformat()}"
    repo.save_daily_metrics(conn, book_id, metrics_id, window_start, window_end, metrics, METRIC_VERSION, clock())
    return metrics_id
