"""Paper-trading ledger over SQLite (architecture §13).

Simulation only — this module has no broker interaction of any kind. Fills
apply a spread + slippage model; cash follows T+1 settlement (sale proceeds
and freed buying power settle the next calendar day and are unusable until
then). Orders carry idempotency keys so a retried submission cannot double.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from ..storage.trading_schema import apply_trading_schema


class LedgerError(RuntimeError):
    pass


class DuplicateOrderError(LedgerError):
    """An order with this idempotency key was already accepted."""


@dataclass(frozen=True)
class FillModel:
    """Deterministic fill assumptions: half the quoted spread plus slippage."""

    slippage_bps: float = 10.0  # applied against the trade direction

    def fill_price(self, side: str, bid: float, ask: float) -> tuple[float, float, float]:
        """Return (price, spread_cost_per_share, slippage_cost_per_share)."""
        if bid <= 0 or ask <= 0 or ask < bid:
            raise LedgerError(f"invalid quote bid={bid} ask={ask}")
        mid = (bid + ask) / 2
        half_spread = (ask - bid) / 2
        slip = mid * self.slippage_bps / 10_000
        if side == "buy":
            return (mid + half_spread + slip, half_spread, slip)
        if side == "sell":
            return (mid - half_spread - slip, half_spread, slip)
        raise LedgerError(f"unknown side {side!r}")


def _today(now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).date()


class PaperLedger:
    """Simulated portfolio: cash, T+1 settlement, positions, fills, snapshots."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        starting_cash: float = 100_000.0,
        fill_model: FillModel | None = None,
    ):
        self.conn = conn
        self.fill_model = fill_model or FillModel()
        apply_trading_schema(conn)
        self._ensure_cash_state(starting_cash)

    # -- cash state (stored as a single JSON row keyed by 'cash') -----------

    def _ensure_cash_state(self, starting_cash: float) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS paper_cash_state "
            "(id INTEGER PRIMARY KEY CHECK (id = 1), state_json TEXT NOT NULL)"
        )
        row = self.conn.execute("SELECT state_json FROM paper_cash_state WHERE id = 1").fetchone()
        if row is None:
            state = {"settled": starting_cash, "pending": []}  # pending: [[iso_date, amount]]
            self.conn.execute(
                "INSERT INTO paper_cash_state (id, state_json) VALUES (1, ?)",
                (json.dumps(state),),
            )
            self.conn.commit()

    def _load_cash(self) -> dict:
        row = self.conn.execute("SELECT state_json FROM paper_cash_state WHERE id = 1").fetchone()
        return json.loads(row["state_json"])

    def _save_cash(self, state: dict) -> None:
        self.conn.execute(
            "UPDATE paper_cash_state SET state_json = ? WHERE id = 1", (json.dumps(state),)
        )

    def settle(self, now: datetime | None = None) -> None:
        """Move pending amounts whose settlement date has arrived into settled cash."""
        state = self._load_cash()
        today = _today(now).isoformat()
        still_pending, settled_delta = [], 0.0
        for due, amount in state["pending"]:
            if due <= today:
                settled_delta += amount
            else:
                still_pending.append([due, amount])
        state["settled"] += settled_delta
        state["pending"] = still_pending
        self._save_cash(state)
        self.conn.commit()

    def settled_cash(self, now: datetime | None = None) -> float:
        self.settle(now)
        return self._load_cash()["settled"]

    def total_cash(self) -> float:
        state = self._load_cash()
        return state["settled"] + sum(a for _, a in state["pending"])

    # -- orders and fills ----------------------------------------------------

    def submit_and_fill(
        self,
        symbol: str,
        side: str,
        qty: int,
        bid: float,
        ask: float,
        idempotency_key: str,
        rec_id: str | None = None,
        stop: float | None = None,
        target: float | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Submit a simulated market order and fill it immediately at the modeled price.

        Returns a summary dict. Raises DuplicateOrderError on idempotency-key
        reuse and LedgerError on policy violations (insufficient settled cash,
        selling more than held, bad quotes).
        """
        if qty <= 0:
            raise LedgerError("qty must be positive")
        now = now or datetime.now(timezone.utc)
        self.settle(now)

        dup = self.conn.execute(
            "SELECT order_id FROM simulated_orders WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if dup is not None:
            raise DuplicateOrderError(f"idempotency key {idempotency_key!r} already used")

        price, half_spread, slip = self.fill_model.fill_price(side, bid, ask)
        state = self._load_cash()

        pos = self.conn.execute(
            "SELECT qty, avg_cost FROM simulated_positions WHERE symbol = ? AND status = 'open'",
            (symbol,),
        ).fetchone()

        if side == "buy":
            cost = price * qty
            if cost > state["settled"]:
                raise LedgerError(
                    f"insufficient settled cash: need {cost:.2f}, have {state['settled']:.2f}"
                )
            state["settled"] -= cost
        elif side == "sell":
            held = pos["qty"] if pos else 0
            if qty > held:
                raise LedgerError(f"cannot sell {qty} {symbol}: only {held} held")
            proceeds = price * qty
            settle_date = (_today(now) + timedelta(days=1)).isoformat()
            state["pending"].append([settle_date, proceeds])  # T+1
        else:
            raise LedgerError(f"unknown side {side!r}")

        order_id = str(uuid.uuid4())
        ts = now.isoformat()
        self.conn.execute(
            "INSERT INTO simulated_orders (order_id, rec_id, ts, symbol, side, qty, "
            "order_type, status, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, 'market', 'filled', ?)",
            (order_id, rec_id, ts, symbol, side, qty, idempotency_key),
        )
        self.conn.execute(
            "INSERT INTO simulated_fills (order_id, ts, price, qty, spread_cost, slippage_cost) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, ts, price, qty, half_spread * qty, slip * qty),
        )

        # Position update
        if side == "buy":
            if pos is None:
                self.conn.execute(
                    "INSERT INTO simulated_positions (symbol, qty, avg_cost, opened_at, stop, target) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (symbol, qty, price, ts, stop, target),
                )
            else:
                new_qty = pos["qty"] + qty
                new_avg = (pos["qty"] * pos["avg_cost"] + qty * price) / new_qty
                self.conn.execute(
                    "UPDATE simulated_positions SET qty = ?, avg_cost = ? WHERE symbol = ?",
                    (new_qty, new_avg, symbol),
                )
        else:
            remaining = pos["qty"] - qty
            if remaining == 0:
                self.conn.execute(
                    "UPDATE simulated_positions SET qty = 0, status = 'closed' WHERE symbol = ?",
                    (symbol,),
                )
            else:
                self.conn.execute(
                    "UPDATE simulated_positions SET qty = ? WHERE symbol = ?",
                    (remaining, symbol),
                )

        self._save_cash(state)
        self.conn.commit()
        return {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "fill_price": round(price, 4),
            "spread_cost": round(half_spread * qty, 4),
            "slippage_cost": round(slip * qty, 4),
        }

    # -- reporting -----------------------------------------------------------

    def positions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT symbol, qty, avg_cost, opened_at, stop, target FROM simulated_positions "
            "WHERE status = 'open' AND qty > 0 ORDER BY symbol"
        ).fetchall()
        return [dict(r) for r in rows]

    def snapshot(self, marks: dict[str, float], now: datetime | None = None) -> dict:
        """Record a daily snapshot; `marks` maps symbol → mark price."""
        now = now or datetime.now(timezone.utc)
        self.settle(now)
        state = self._load_cash()
        positions = self.positions()
        missing = [p["symbol"] for p in positions if p["symbol"] not in marks]
        if missing:
            raise LedgerError(f"no mark price for open positions: {missing} — fail closed")
        pos_value = sum(p["qty"] * marks[p["symbol"]] for p in positions)
        equity = state["settled"] + sum(a for _, a in state["pending"]) + pos_value

        prev = self.conn.execute(
            "SELECT MAX(equity) AS peak FROM simulated_portfolio_snapshots"
        ).fetchone()
        peak = max(prev["peak"] or equity, equity)
        drawdown = 0.0 if peak == 0 else (equity - peak) / peak

        snap_date = _today(now).isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO simulated_portfolio_snapshots "
            "(snap_date, equity, cash, settled_cash, positions_json, drawdown) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                snap_date,
                equity,
                self.total_cash(),
                state["settled"],
                json.dumps(positions),
                drawdown,
            ),
        )
        self.conn.commit()
        return {"date": snap_date, "equity": round(equity, 2), "drawdown": round(drawdown, 4)}
