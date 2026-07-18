You are continuing implementation of my existing trading-desk repository:

https://github.com/jijoece/ai_stock_trading

Do not create a new repository, replace the existing architecture, or weaken any current safety boundary.

The repository already provides:

* deterministic screening, scoring, and position sizing;
* bounded AI research providers;
* isolated local paper books;
* explicit Alpaca paper-account execution;
* broker reconciliation and partial-fill handling;
* persistent usage, health, budgets, leases, and audit records;
* disabled-by-default scheduled and execution capabilities;
* no live trading, options, margin, shorting, fractional orders, or extended-hours execution.

Your task is to implement the highest-priority missing risk-management and validation capabilities.

# Objective

Implement the following four workstreams:

1. End-to-end daily-loss and drawdown circuit breakers.
2. Deterministic ATR-based lifecycle exits, trailing stops, and breakeven protection.
3. Deterministic partial profit-taking.
4. A point-in-time-safe high-impact economic-event blackout filter.

Also add the minimum historical backtesting framework needed to verify these controls against past market data.

Do not implement WebSocket market data in this milestone. The application currently operates at a low-frequency daily research and paper-trading cadence, so REST-based data remains acceptable.

# Mandatory implementation principles

The following rules are non-negotiable:

* Python owns every risk calculation and execution decision.
* Claude must not calculate ATR, stop levels, quantities, position sizes, partial-close quantities, or circuit-breaker status.
* AI output must not override deterministic risk controls.
* Unknown, stale, unreconciled, or inconsistent financial state must fail closed.
* Live trading must remain structurally unavailable.
* External Alpaca paper submission must remain disabled by default.
* The recurring scheduler must never submit or cancel external orders.
* Do not add automatic fallback behavior.
* Do not add credential-driven capability activation.
* All default tests must remain offline.
* Do not make any real broker, provider, or model call unless guarded by an existing explicit opt-in test flag.
* Do not commit, push, create a pull request, install a scheduler, or enable production configuration unless explicitly requested.

# Begin with repository inspection

Before changing code, inspect the current implementation.

Read at minimum:

* `README.md`
* `docs/INDEX.md`
* current ADRs
* `src/trading_research/risk/position_sizing.py`
* `src/trading_research/paper_books/risk_policy.py`
* `src/trading_research/paper_books/exit_policy.py`
* `src/trading_research/paper_books/lifecycle.py`
* `src/trading_research/paper_books/metrics.py`
* `src/trading_research/paper_books/valuation.py`
* `src/trading_research/paper_books/execution.py`
* `src/trading_research/paper_books/positions.py`
* `src/trading_research/paper_books/cash_ledger.py`
* `src/trading_research/paper_books/scheduled_integration.py`
* `src/trading_research/paper_books/recurring.py`
* `src/trading_research/paper_books/external_broker.py`
* `src/trading_research/evidence_providers/market_data_provider.py`
* `src/trading_research/evaluation/`
* `src/trading_research/analysis/`
* `src/trading_research/storage/`
* `config/paper_books.yaml`
* `config/screening.yaml`
* `config/shadow_operations.yaml`
* relevant schemas, migrations, repositories, runbooks, and tests

Search the repository for:

```text
daily_loss
drawdown
stop_loss
profit_target
exit_policy
partial
ATR
true_range
highest_price
high_water
breakeven
economic
calendar
blackout
earnings
market_days
backtest
forward_return
paper_book_metrics
reconciliation
```

Identify every place where:

* a new paper-book entry may be approved;
* an existing position may be exited;
* daily portfolio metrics are calculated;
* lifecycle state is persisted;
* scheduler readiness is evaluated;
* paper-order quantities are constructed;
* point-in-time historical data is selected.

# Progress scratchpad

Before changing implementation files, create:

```text
.claude/scratchpads/advanced-risk-controls-progress.md
```

Include:

```markdown
# Advanced Risk Controls Progress

Started:
Branch:
Commit:
Status:

## Baseline
- Main test suite:
- Paper runtime suite:
- Git status:
- Existing failures:
- Existing skipped tests:

## Repository findings

## Gap analysis

## Architecture decisions

## Workstream 1 — Daily-loss and drawdown breakers

## Workstream 2 — ATR, trailing stop, and breakeven

## Workstream 3 — Partial closes

## Workstream 4 — Economic-event blackout

## Workstream 5 — Historical backtesting

## Schema and migrations

## Files created

## Files modified

## Tests added

## Test run log

## Bugs discovered

## Safety verification

## Known limitations

## Remaining work

## Final status
```

Update the scratchpad after every major implementation stage.

Do not put credentials, tokens, account identifiers, full environment output, prompts, or raw provider responses in the scratchpad.

# Workstream 1 — End-to-end daily-loss and drawdown circuit breakers

The generic position-sizing layer already exposes daily-loss and drawdown inputs, but the active paper-book risk path must calculate and enforce these values from persisted book state.

Implement authoritative book-level circuit-breaker calculations.

## Required calculations

For each paper book and market date, calculate:

```text
start_of_day_equity
current_equity
realized_pnl_today
unrealized_pnl_today
total_pnl_today
daily_loss_fraction
peak_equity
current_drawdown_fraction
```

Define the formulas explicitly and document them.

Suggested daily-loss formula:

```text
daily_loss_fraction =
    (current_equity - start_of_day_equity - net_external_cash_flows_today)
    / start_of_day_equity
```

Because these are simulated paper books, external cash flows may normally be zero. Still represent the field explicitly so deposits or administrative adjustments cannot be confused with trading profit.

Suggested drawdown formula:

```text
current_drawdown_fraction =
    (current_equity - historical_peak_equity)
    / historical_peak_equity
```

Both loss values should be negative during a loss.

## Persisted state

Add a versioned, immutable or append-only daily risk-state record containing at least:

* record ID;
* book ID;
* market date;
* as-of timestamp;
* start-of-day equity;
* current equity;
* realized P&L;
* unrealized P&L;
* total daily P&L;
* net external cash flow;
* daily-loss fraction;
* historical peak equity;
* drawdown fraction;
* valuation status;
* source snapshot IDs;
* reconciliation status;
* calculation policy version;
* configuration hash;
* created timestamp.

Use `Decimal` for all financial values and ratios.

Do not use float for persisted money or risk ratios.

## Enforcement

Add configuration under `paper_books.risk`, with quoted decimal values:

```yaml
max_daily_loss_fraction: "0.03"
max_drawdown_fraction: "0.15"
daily_loss_action: PAUSE_NEW_ENTRIES
drawdown_action: PAUSE_NEW_ENTRIES
require_reconciled_risk_state: true
```

Use closed enums and strict configuration validation.

Before approving any new BUY intent, the active paper-book risk path must:

1. load or calculate the current daily risk state;
2. verify valuation completeness according to policy;
3. verify reconciliation status;
4. reject stale risk state;
5. reject an unknown start-of-day baseline;
6. reject if daily loss has breached the configured limit;
7. reject if drawdown has breached the configured limit.

Add explicit decision codes such as:

```text
RISK_REJECTED_DAILY_LOSS_LIMIT
RISK_REJECTED_DRAWDOWN_LIMIT
RISK_REJECTED_RISK_STATE_UNAVAILABLE
RISK_REJECTED_RISK_STATE_STALE
RISK_REJECTED_RISK_STATE_UNRECONCILED
```

Existing SELL exits must remain allowed when a loss breaker is active. A circuit breaker stops new exposure; it must not trap an existing position.

## Book pause integration

When configured, a daily-loss or drawdown breach should persist a deterministic book-level safety pause or block record.

Do not silently reactivate the book on the next successful calculation.

Use existing explicit operator resume semantics or add a similarly explicit resume action if the paper-book subsystem does not currently have one.

Avoid introducing a second conflicting pause system if the existing book status or shadow pause system can safely represent this state.

# Workstream 2 — ATR-based stops, trailing stops, and breakeven

Implement deterministic ATR calculations and lifecycle state.

## ATR calculation

Create a production reusable indicator implementation, not only a standalone script.

Suggested location:

```text
src/trading_research/analysis/indicators.py
```

Or extend an existing production indicator module if one already exists.

Implement:

```python
true_range(...)
average_true_range(...)
```

Use Wilder ATR with a configurable default period of 14.

Inputs must include high, low, and previous close. Do not approximate ATR from close-only data.

Requirements:

* deterministic;
* point-in-time safe;
* no future bars;
* no implicit current time;
* explicit insufficient-history result;
* Decimal-compatible output at the risk boundary;
* tests against hand-calculated fixtures.

## Entry risk plan

Extend the deterministic entry plan to support configured ATR risk levels.

Example configuration:

```yaml
atr:
  enabled: true
  period: 14
  initial_stop_multiple: "2.0"
  initial_target_multiple: "3.0"
  minimum_atr_percent: "0.005"
  maximum_atr_percent: "0.20"
```

Suggested long-position calculations:

```text
initial_stop_price = entry_price - ATR × initial_stop_multiple
initial_target_price = entry_price + ATR × initial_target_multiple
```

The ATR-derived stop must be calculated before final position sizing because risk per share depends on the stop distance.

Do not let the model provide ATR, stop price, target price, or risk multiple.

Fail closed when:

* ATR is unavailable;
* required bars are missing;
* bars are stale;
* bars are not point-in-time safe;
* ATR is zero or negative;
* ATR percentage is outside configured bounds;
* the resulting stop is invalid;
* the resulting quantity rounds to zero.

## Persist lifecycle state at entry

Persist a versioned per-position lifecycle state with at least:

* lifecycle state ID;
* book ID;
* symbol;
* originating intent ID;
* entry fill ID;
* opened timestamp;
* original quantity;
* remaining quantity;
* average entry price;
* entry ATR;
* ATR period;
* initial stop price;
* current stop price;
* initial target price;
* highest eligible price since entry;
* trailing-stop activation status;
* breakeven activation status;
* partial-profit stage;
* policy version;
* config hash;
* last evaluated timestamp;
* source market-data identifier.

The state must be replayable and idempotent.

Do not derive all of this later from mutable configuration.

## Trailing stop

Add configuration:

```yaml
trailing_stop:
  enabled: true
  activation_r_multiple: "1.5"
  atr_multiple: "2.0"
  never_loosen_stop: true
```

For a long position:

```text
initial_risk_per_share = entry_price - initial_stop_price
current_r_multiple =
    (reference_price - entry_price) / initial_risk_per_share
```

Activate trailing protection only after the configured R multiple is reached.

Suggested trailing level:

```text
candidate_trailing_stop =
    highest_eligible_price_since_entry - current_ATR × trailing_atr_multiple
```

Requirements:

* update the high-water mark only using point-in-time-safe, non-stale prices;
* never lower an existing long-position stop;
* persist every material stop-state transition;
* do not update state if the price is missing, stale, or unsafe;
* if ATR refresh is unavailable, preserve the previous valid stop and record an incomplete evaluation;
* a stop breach must result in a full or configured partial exit according to deterministic policy.

## Breakeven protection

Add configuration:

```yaml
breakeven:
  enabled: true
  activation_r_multiple: "1.0"
  offset_bps: "0"
  never_loosen_stop: true
```

Once activated:

```text
breakeven_stop =
    entry_price × (1 + offset_bps / 10000)
```

The current stop becomes the maximum of:

* existing stop;
* breakeven stop;
* trailing-stop candidate.

For long positions, the stop must never decrease.

Persist the exact reason whenever the stop changes:

```text
INITIAL_ATR_STOP
BREAKEVEN_ACTIVATED
TRAILING_STOP_ADVANCED
UNCHANGED
```

# Workstream 3 — Deterministic partial profit-taking

The current exit policy uses full-position exits only. Extend it carefully without allowing fractional-share behavior.

## Configuration

Add a closed, ordered list such as:

```yaml
partial_profit:
  enabled: true
  stages:
    - stage: 1
      trigger_r_multiple: "1.5"
      close_fraction: "0.50"
    - stage: 2
      trigger_r_multiple: "2.5"
      close_fraction: "0.25"
  minimum_remaining_quantity: "1"
```

Use exact Decimal parsing.

Validate:

* stages are strictly increasing by trigger;
* stage IDs are unique;
* fractions are greater than zero and at most one;
* cumulative configured close fractions do not exceed one;
* whole-share rounding is deterministic;
* remaining quantity cannot become negative;
* no stage can execute twice.

## Quantity rules

Calculate a stage close quantity using deterministic whole-share rounding.

Suggested rule:

```text
target_close_quantity =
    floor(original_position_quantity × configured_close_fraction)
```

Then clamp it to:

```text
min(
    target_close_quantity,
    available_unreserved_quantity,
    current_remaining_quantity - minimum_remaining_quantity
)
```

If the calculated quantity is zero, persist a no-action reason rather than fabricating one share.

Do not calculate the percentage from the already reduced remaining quantity unless that is the explicitly documented design.

## Exit priority

Define and test an explicit priority order.

Recommended order:

1. Manual full exit.
2. Hard stop or trailing stop.
3. Safety or reconciliation-mandated full exit.
4. Maximum holding period.
5. Recommendation reversal.
6. Partial profit stage.
7. Final profit target.
8. Hold.

A stop breach should normally close the full remaining quantity.

A partial-profit stage should close only the deterministic stage quantity.

Prevent simultaneous duplicate SELL intents for the same position and stage.

Persist:

* stage ID;
* trigger;
* evaluated price;
* quantity before;
* quantity requested;
* quantity approved;
* quantity filled;
* quantity remaining;
* resulting stop state;
* decision ID;
* originating lifecycle evaluation ID.

Support partial broker fills without considering the stage complete until its intended quantity has been fully accounted for or deterministically resolved.

# Workstream 4 — High-impact economic-event blackout

Implement a framework-neutral economic calendar boundary and deterministic blackout policy.

Do not give Claude access to this provider.

## Provider contract

Suggested protocol:

```python
class EconomicCalendarProvider(Protocol):
    def fetch_events(
        self,
        *,
        start: datetime,
        end: datetime,
        as_of: datetime,
    ) -> tuple[EconomicEvent, ...]:
        ...
```

Suggested event fields:

* event ID;
* title;
* category;
* country or market;
* scheduled timestamp;
* originally published timestamp;
* last updated timestamp;
* importance;
* status;
* actual value;
* forecast value;
* previous value;
* source provider;
* source locator;
* retrieved timestamp;
* available timestamp;
* point-in-time-safe flag;
* content hash.

## Initial provider strategy

Do not add a fragile scraper casually.

First inspect whether an existing configured news or data provider supports an economic calendar through a documented API.

If no suitable documented provider is already available, implement:

* the provider protocol;
* fixtures;
* persistence;
* blackout policy;
* scheduler integration;
* configuration;
* tests;

and leave the real external provider disabled and explicitly `ENVIRONMENTALLY_PENDING`.

Do not use undocumented endpoints or scrape a website without an explicit architectural decision.

## Blackout policy

Add configuration such as:

```yaml
economic_event_blackout:
  enabled: false
  before_minutes: 30
  after_minutes: 30
  minimum_importance: HIGH
  markets:
    - US
  blocked_categories:
    - FOMC
    - CPI
    - PPI
    - NONFARM_PAYROLLS
    - GDP
    - RETAIL_SALES
    - UNEMPLOYMENT
  unknown_event_state_action: BLOCK_NEW_ENTRIES
```

Create a pure deterministic function such as:

```python
evaluate_economic_event_blackout(
    *,
    as_of: datetime,
    events: tuple[EconomicEvent, ...],
    configuration: EconomicEventBlackoutConfiguration,
) -> EconomicEventBlackoutDecision
```

It must return:

* allowed or blocked;
* matched event IDs;
* blackout start and end;
* reason codes;
* policy version;
* configuration hash.

Block new BUY entries during the blackout.

Do not block risk-reducing SELL exits.

Fail closed when the blackout is enabled but required event data is unavailable, stale, malformed, or point-in-time unsafe.

Persist each blackout decision used for an order-risk evaluation.

# Workstream 5 — Minimum historical backtesting framework

Build a deterministic historical backtesting framework specifically to validate the new controls.

Do not create an unrelated, second trading system.

Reuse existing:

* screening;
* scoring;
* recommendation construction;
* position sizing;
* ATR calculation;
* paper-book risk policy;
* lifecycle exit policy;
* cash, lot, and position accounting;
* metrics;
* market calendar;
* point-in-time price selection.

## Suggested package

```text
src/trading_research/backtesting/
```

Possible modules:

```text
models.py
configuration.py
engine.py
data_provider.py
execution_model.py
reports.py
```

Use repository conventions rather than forcing this exact layout.

## Backtest requirements

Support:

* one or more symbols;
* explicit start and end dates;
* daily bars;
* initial cash;
* long-only strategies;
* whole-share LIMIT-style simulated orders;
* deterministic slippage;
* deterministic fees, defaulting to zero but explicitly represented;
* no look-ahead;
* no current quote substitution;
* point-in-time corporate and event data where required;
* entry and exit on explicitly documented bar timing;
* partial exits;
* stop gaps;
* maximum holding periods;
* daily-loss breaker;
* drawdown breaker;
* economic-event blackout;
* cash and share reservations;
* realized and unrealized P&L;
* equity curve;
* drawdown curve;
* trade ledger;
* rejected-entry reasons;
* configuration and code provenance.

## Bar execution semantics

Explicitly document the chosen semantics.

A safe default would be:

* signals are generated only after a session’s close;
* resulting entry orders are eligible no earlier than the next session;
* next-session fills use an explicitly defined deterministic rule;
* intraday stop and target checks use daily high and low only after the position existed at the beginning of that bar;
* when both stop and target are touched in the same bar and sequence is unknown, use the conservative adverse outcome or classify the bar as ambiguous according to policy;
* never assume the favorable trigger happened first.

Persist or report the ambiguity policy.

## Backtest reports

Produce at minimum:

* start/end dates;
* symbols;
* initial and ending equity;
* total return;
* benchmark return;
* maximum drawdown;
* realized P&L;
* number of completed trades;
* win rate;
* average win;
* average loss;
* profit factor;
* average holding period;
* stop exits;
* trailing-stop exits;
* breakeven exits;
* partial-profit fills;
* final-target exits;
* daily-loss blocks;
* drawdown blocks;
* economic-event blocks;
* rejected signals;
* unresolved or incomplete evaluations.

Do not claim strategy quality from a single run.

# Schema and migrations

Add forward-safe, additive migrations for every new persisted structure.

Potential new structures include:

```text
paper_book_daily_risk_states
paper_book_position_lifecycle_states
paper_book_lifecycle_state_events
paper_book_partial_exit_stages
economic_calendar_events
economic_blackout_decisions
backtest_runs
backtest_daily_states
backtest_orders
backtest_fills
backtest_positions
backtest_metrics
```

Use the minimum number of tables that preserves:

* immutability;
* idempotency;
* auditability;
* replay;
* crash safety;
* exact Decimal round trips;
* explicit foreign-key relationships.

Add migration tests starting from at least:

* the immediately previous schema;
* an older repository schema fixture if the project maintains one;
* repeated migration application;
* a future unsupported schema version.

Do not modify existing historical records in place unless an explicit migration requires it.

# Transaction and concurrency requirements

Safety-sensitive state transitions must be atomic.

At minimum, ensure atomicity for:

* circuit-breaker state calculation and persistence;
* lifecycle stop-state advancement;
* partial-stage claim and SELL intent creation;
* partial-fill application;
* position remaining-quantity updates;
* stage completion;
* reservation changes;
* order/fill/position/cash updates.

Use existing transaction helpers and lease/fencing patterns.

Do not introduce check-then-write races.

Idempotent retries must not:

* execute a partial stage twice;
* advance a stop twice incorrectly;
* double-apply a fill;
* double-count P&L;
* reset a historical equity peak;
* duplicate a blackout decision;
* create duplicate SELL intents.

# Tests

Add comprehensive offline tests.

## Daily-loss and drawdown

Test:

* start-of-day baseline;
* realized gain;
* realized loss;
* unrealized loss;
* combined realized and unrealized P&L;
* external cash-flow adjustment;
* exact threshold;
* just below threshold;
* daily loss breach;
* drawdown breach;
* missing baseline;
* stale valuation;
* partial valuation;
* unreconciled book;
* SELL exits remain allowed;
* BUY entries are blocked;
* pause persistence;
* explicit resume behavior;
* Decimal precision;
* no look-ahead in peak equity.

## ATR

Test:

* true range calculations;
* Wilder ATR fixtures;
* insufficient history;
* missing high/low/close;
* zero ATR;
* extreme ATR;
* point-in-time filtering;
* initial stop and target;
* position sizing with ATR stop distance;
* quantity never exceeds configured risk.

## Trailing and breakeven

Test:

* no activation before threshold;
* exact activation threshold;
* breakeven activation;
* trailing activation;
* high-water-mark advancement;
* stop never loosens;
* stale price does not advance state;
* stale ATR preserves previous stop;
* gap below stop;
* idempotent repeated lifecycle run;
* restart/replay from persisted state.

## Partial exits

Test:

* first stage;
* second stage;
* stage cannot repeat;
* whole-share rounding;
* zero-share stage;
* minimum remaining quantity;
* full stop after a prior partial exit;
* partial broker fill;
* unresolved SELL reservation;
* concurrent lifecycle evaluation;
* duplicate SELL prevention;
* final quantity never negative;
* total exited quantity never exceeds original position.

## Economic blackout

Test:

* no events;
* event outside window;
* exact before boundary;
* exact after boundary;
* high-impact blocked;
* low-impact allowed;
* unrecognized category;
* missing event data;
* stale data;
* future-published event rejected;
* point-in-time unsafe event rejected;
* BUY blocked;
* SELL allowed;
* disabled configuration does nothing.

## Backtesting

Test:

* no look-ahead;
* next-session entry;
* stop-gap behavior;
* same-bar stop and target ambiguity;
* partial exits;
* daily-loss blocking;
* drawdown blocking;
* economic blackout;
* cash accounting;
* whole-share behavior;
* deterministic replay;
* same inputs produce identical outputs;
* changed configuration changes config hash and results;
* historical bars after `as_of` are never read;
* benchmark comparison;
* empty or incomplete datasets fail closed.

## Safety regression tests

Assert:

* no live gateway becomes available;
* no new CLI live flag exists;
* no model provider imports broker code;
* no model-generated quantity becomes authoritative;
* recurring scheduler still cannot submit or cancel external orders;
* default external submission remains false;
* options, shorting, margin, fractional shares, and extended-hours remain disabled;
* default tests make no network call;
* default tests make no real Claude or Codex call;
* default tests make no broker call.

# Configuration posture

All new features must ship disabled or conservatively configured.

Recommended default posture:

```yaml
paper_books:
  enabled: false

  risk:
    max_daily_loss_fraction: "0.03"
    max_drawdown_fraction: "0.15"
    require_reconciled_risk_state: true

  lifecycle:
    enabled: false

    atr:
      enabled: false

    trailing_stop:
      enabled: false

    breakeven:
      enabled: false

    partial_profit:
      enabled: false

    economic_event_blackout:
      enabled: false
```

Do not automatically modify production configuration to enable them.

Create a separate example or dormant evaluation profile if needed.

# CLI additions

Add only narrowly scoped commands.

Potential commands:

```text
paper-book-daily-risk-show
paper-book-lifecycle-state-show
paper-book-blackout-check
backtest-run
backtest-show
backtest-report
```

All commands must:

* emit bounded structured output;
* avoid raw secrets;
* validate timezone-aware timestamps;
* avoid implicit current-time behavior where reproducibility matters;
* fail closed on unknown IDs or configuration;
* distinguish incomplete data from a valid no-action result.

Do not add an automatic external-order submission command.

# Documentation

Create a focused document such as:

```text
docs/advanced-risk-controls.md
```

Document:

* architecture and authority boundaries;
* daily-loss and drawdown formulas;
* valuation and reconciliation requirements;
* ATR formula and bar requirements;
* entry stop and target calculations;
* trailing-stop behavior;
* breakeven behavior;
* partial-exit quantity rules;
* exit priority;
* economic-event blackout;
* backtesting timing semantics;
* same-bar ambiguity handling;
* migration details;
* operational rollout;
* known limitations.

Update:

* `README.md`
* `docs/INDEX.md`
* relevant ADRs or create a new ADR if the lifecycle-state design is architectural;
* paper-book runbooks;
* configuration comments.

Do not describe live trading as supported.

# Verification

Run the repository’s existing baseline before implementation and record it.

After implementation, run at minimum:

```bash
pytest tests/ -q --tb=short
cd paper_runtime && pytest tests/ -q --tb=short
```

Also run focused groups for:

```text
position sizing
paper-book risk policy
exit policy
lifecycle
daily risk state
partial exits
economic blackout
backtesting
migrations
reconciliation
recurring scheduler
external broker safety
```

Run:

```bash
git diff --check
```

Run the repository’s existing type checker and compare it with the documented baseline. Do not claim type-check cleanliness unless it is genuinely clean.

Do not make a real provider, model, or broker request during verification.

# Required final response

After implementation, provide:

1. Executive summary.
2. Baseline test results.
3. Architecture decisions.
4. Exact files created.
5. Exact files modified.
6. Configuration changes.
7. Database migrations.
8. Daily-loss and drawdown formulas.
9. ATR, trailing-stop, and breakeven behavior.
10. Partial-close rules and exit priority.
11. Economic-event provider and blackout behavior.
12. Backtesting execution assumptions.
13. Tests added.
14. Exact verification results.
15. Pre-existing failures or type-check baseline.
16. Known limitations.
17. Operational rollout steps.
18. Explicit confirmation that:

    * no live trading was added;
    * no real broker request was made;
    * no real order was submitted;
    * no scheduler was activated;
    * all external paper submission flags remain disabled.

Do not stop after producing a plan. Inspect the repository, implement the complete vertical slice, add migrations, tests, configuration, documentation, and run the available verification in the current working branch.
