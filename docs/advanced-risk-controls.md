# Advanced risk controls and historical validation

Milestone 13 adds deterministic, disabled-by-default controls to the isolated
paper-book subsystem. Python remains the only authority for ATR, position
size, loss and drawdown state, stop advancement, partial quantities, economic
blackouts, and historical execution. Model output cannot override these
controls. Live trading remains unavailable.

## Daily loss and drawdown

Each complete paper-book valuation can produce an immutable daily risk state:

```text
total_pnl_today = current_equity - start_of_day_equity - net_external_cash_flow_today
daily_loss_fraction = total_pnl_today / start_of_day_equity
current_drawdown_fraction = (current_equity - historical_peak_equity) / historical_peak_equity
```

Losses are negative. Realized and unrealized daily P&L must reconcile exactly
to `total_pnl_today`. The calculation requires a known positive start-of-day
baseline, a complete point-in-time valuation, and—when configured—the latest
book reconciliation status `MATCHED`. Unknown, stale, partial, unsafe, or
unreconciled state blocks new BUY exposure. Existing SELL exits remain
eligible.

An exact threshold is a breach. A breach appends a book safety-pause event;
a later good valuation never clears it. `paper_books/safety_pause.py::resume`
requires an explicit operator and reason.

## ATR entry and lifecycle state

`analysis/indicators.py` implements true range and Wilder ATR. ATR requires
high, low, previous close, and `period + 1` ordered bars. It has no implicit
clock and returns `None` for insufficient history.

For a long entry:

```text
initial_stop = entry_price - ATR * initial_stop_multiple
initial_target = entry_price + ATR * initial_target_multiple
```

The stop distance is applied before whole-share sizing. Missing, non-positive,
out-of-range, stale, or point-in-time-unsafe ATR evidence fails closed. Every
filled ATR entry receives an immutable lifecycle-state version containing its
entry ATR, frozen stop/target, original and remaining quantity, high-water
mark, partial stage, config hash, and source bar identity.

Breakeven activates at its configured R multiple and raises the stop to entry
plus the configured basis-point offset. Trailing protection activates at its
configured R multiple and proposes:

```text
highest_eligible_price - current_ATR * trailing_atr_multiple
```

The long stop is the maximum valid candidate and can never decrease. A stale
price does not advance state. Missing refresh ATR preserves the previous valid
stop and records an incomplete evaluation.

## Partial profits and exit priority

A stage quantity is always based on original shares:

```text
floor(original_quantity * close_fraction)
```

It is clamped to available unreserved shares and to remaining shares above
the configured minimum. Zero remains a documented no-action; the system never
fabricates one share. A stage cannot complete twice. Local fill, position,
cash, remaining quantity, and completed-stage evidence use transaction
boundaries; broker partial fills do not complete a stage until its intended
quantity is accounted for.

Priority is manual full exit, hard/trailing/breakeven stop, safety full exit,
maximum holding period, recommendation reversal, partial-profit stage, final
target, then hold. Stop exits close all remaining shares.

## Economic-event blackout

`EconomicCalendarProvider` is framework-neutral and is not exposed to model
providers. No documented calendar API is configured in this repository, so
the real provider remains `ENVIRONMENTALLY_PENDING`; only the protocol,
fixtures, persistence, and policy ship here.

When enabled, the pure policy blocks new BUY entries inside inclusive before
and after windows for configured markets, categories, and importance. Missing,
stale, future-published, malformed, or point-in-time-unsafe calendar state
fails closed. Disabled configuration has no effect. SELL exits bypass the
blackout.

## Historical execution semantics

The minimum backtester reuses the production ATR, partial-quantity, blackout,
and risk formulas. Signals are generated only after a session close and are
eligible on the next symbol session. Orders are whole-share LIMIT-style with
explicit deterministic fees and slippage. A position must exist at the start
of a bar before that bar's high/low can trigger its lifecycle. A gap below a
stop fills at the adverse open. If the daily range touches both stop and a
favorable exit, `CONSERVATIVE_STOP_FIRST` applies. Current quotes never replace
missing historical bars.

Reports include provenance, equity and drawdown curves, realized/unrealized
P&L, exit counts, rejection reasons, benchmark return when supplied, and
incomplete evaluations. A run validates controls; it does not establish
strategy quality.

## Persistence and rollout

Schema migration 10 adds exact-Decimal append-only risk, lifecycle, calendar,
blackout, safety, and backtest structures. Existing records are not rewritten.
The default YAML keeps the paper subsystem, lifecycle, ATR, trailing,
breakeven, partial-profit, economic blackout, and external paper submission
disabled.

Safe rollout is: migrate an offline copy, populate start-of-day snapshots and
reconciliation evidence, replay fixture backtests, run manual local lifecycle
days, review immutable decisions, and only then consider editing a dormant
paper-only evaluation profile. This does not authorize or enable any broker
submission or scheduler installation.
