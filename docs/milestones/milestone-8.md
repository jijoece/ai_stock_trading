Continue implementation of my existing AI-driven trading-desk repository.

Implement:

# Milestone 8 — Isolated paper portfolios and portfolio-aware experiment evaluation

Milestones 1 through 7.2 are already complete.

Milestone 8 introduces isolated baseline and enhanced paper portfolios so the two research arms can be evaluated fairly without sharing cash, positions, orders, or performance history.

This milestone remains strictly paper-only.

Do not implement live trading, margin, options, short selling, real-money broker execution, or automatic promotion.

Do not restart or redesign Milestones 1–7.2.

---

# Primary objective

Build this end-to-end capability:

```text
Frozen research cycle
        ↓
Baseline recommendation
Enhanced recommendation
        ↓
Independent deterministic portfolio/risk evaluation
        ↓
Independent paper-order intents
        ↓
BASELINE paper book
ENHANCED paper book
        ↓
Independent paper fills, positions, cash, and reconciliation
        ↓
Point-in-time mark-to-market snapshots
        ↓
Portfolio-aware performance evaluation
        ↓
Baseline-versus-enhanced comparison
        ↓
Promotion evidence only
```

The two experiment arms must be isolated:

```text
BASELINE:
    its own cash
    its own positions
    its own pending orders
    its own fills
    its own realized P&L
    its own unrealized P&L
    its own fees/slippage
    its own risk limits

ENHANCED:
    its own cash
    its own positions
    its own pending orders
    its own fills
    its own realized P&L
    its own unrealized P&L
    its own fees/slippage
    its own risk limits
```

The enhanced arm may submit only to its isolated paper book.

It must remain impossible for enhanced recommendations to reach:

* a live broker;
* a real-money order;
* the baseline paper book;
* the existing `real_orders` boundary;
* any shared account state that contaminates comparison.

---

# Authority model

Preserve the existing authority boundary:

```text
Evidence providers:
    Retrieve and normalize point-in-time facts.

Claude:
    Analyze supplied evidence and return structured research.

Deterministic application code:
    Validate recommendations.
    Decide paper eligibility.
    Apply portfolio and risk limits.
    Size paper orders.
    Submit paper intents.
    Maintain books.
    Reconcile.
    Mark to market.
    Evaluate performance.
    Decide promotion readiness.

Claude must never:
    Place an order.
    Select the paper book.
    Override a risk limit.
    Set account cash.
    Alter a fill.
    Reconcile a ledger.
    Compute promotion status authoritatively.
```

---

# Mandatory source review

Before editing, read:

```text
.claude/scratchpads/milestone7-progress.md
.claude/scratchpads/milestone7-1-progress.md
.claude/scratchpads/milestone7-2-progress.md

docs/milestone7-production-shadow-operations.md
docs/milestone7-1-shadow-integration-closure.md
docs/milestone7-2-shadow-health-diagnostics.md
docs/milestone7-pending-work.md

docs/adr/0004-real-evidence-provider-boundary.md
docs/adr/0005-production-shadow-operations-boundary.md

docs/runbooks/shadow-operations.md
docs/runbooks/shadow-incident-response.md
```

Inspect at minimum:

```text
src/trading_research/research/scheduled_cycle.py
src/trading_research/research/models.py
src/trading_research/research/promotion.py
src/trading_research/research/experiment_policy.py
src/trading_research/research/cycle_telemetry.py

src/trading_research/evaluation/
src/trading_research/evaluation/research_comparison.py
src/trading_research/evaluation/market_calendar.py

src/trading_research/paper/
src/trading_research/execution/
src/trading_research/services/

src/trading_research/storage/
src/trading_research/storage/database.py

src/trading_research/shadow/scheduler.py
src/trading_research/shadow/readiness.py

paper_runtime/

src/trading_research/cli.py

config/scheduled_research.yaml
config/shadow_operations.yaml
config/evidence_providers.yaml
config/research.yaml
```

Also inspect all existing:

* paper order models;
* ledger tables;
* fill models;
* position calculations;
* reconciliation logic;
* execution policies;
* recommendation immutability controls;
* experiment-assignment tables;
* evaluation-result tables.

Use the existing code as the source of truth.

Do not duplicate working abstractions.

---

# Mandatory scratchpad

Before implementation edits, create:

```text
.claude/scratchpads/milestone8-progress.md
```

Use:

```markdown
# Milestone 8 Progress

Started:
Branch:
Status: STARTING

## Baseline
## Existing paper architecture
## Existing experiment architecture
## Confirmed gaps
## Architecture decisions
## Paper-book model
## Book isolation
## Portfolio snapshot model
## Mark-to-market design
## Position and lot accounting
## Cash and buying-power model
## Risk-policy design
## Order sizing
## Experiment assignment
## Paper execution
## Reconciliation
## Corporate-action handling
## Performance metrics
## Baseline-versus-enhanced comparison
## Promotion evidence
## CLI design
## Schema changes
## Files created
## Files modified
## Tests added
## Test run log
## Optional paper-broker validation
## Bugs discovered and fixed
## Security and safety review
## Documentation
## Known limitations
## Deferred work
## Final status
```

Scratchpad rules:

1. Update after each major implementation phase.
2. Record actual commands and results.
3. Preserve failed approaches and discovered defects.
4. Never include:

   * credentials;
   * `.env` contents;
   * authorization headers;
   * brokerage account identifiers;
   * raw Claude prompts;
   * raw Claude responses;
   * chain-of-thought.
5. Credential checks must be Boolean only.
6. Distinguish:

   * local simulated paper execution;
   * external paper-broker execution;
   * real-money/live execution.
7. Do not commit or push unless explicitly asked.

---

# Confirmed starting baseline

Verify before editing:

```text
pytest tests/ -q

Expected:
1266 passed, 14 skipped
```

Then:

```text
cd paper_runtime
pytest tests/ -q

Expected:
33 passed
```

Check Git status and preserve all existing work.

If the baseline differs, investigate before proceeding.

---

# Hard safety boundaries

Do not:

* enable live trading;
* create a live broker mode;
* add a `--live` flag;
* add margin;
* add short selling;
* add options;
* let Claude invoke broker tools;
* let Claude select a portfolio or book;
* let Claude override risk controls;
* let Claude set cash or buying power;
* let Claude determine fills;
* share cash between baseline and enhanced books;
* share positions between baseline and enhanced books;
* reuse the same order identifier across books;
* let enhanced recommendations enter the baseline book;
* let baseline recommendations enter the enhanced book;
* mutate frozen recommendations;
* rewrite historical prices;
* use current prices in historical portfolio snapshots;
* fabricate fill prices;
* fabricate cash;
* fabricate cost basis;
* fabricate corporate actions;
* silently treat missing prices as zero;
* silently value a stale position at zero;
* weaken existing claim validation;
* weaken evidence-completeness gating;
* weaken shadow pause/kill controls;
* activate launchd;
* start a daemon;
* automatically promote enhanced research;
* modify `real_orders`;
* perform unrelated Milestone 7 backlog work.

---

# Explicit non-goals

Do not include in Milestone 8:

* fixing `unsupported_claim_rate`;
* real paper-reconciliation health wiring from Milestone 7 backlog;
* duplicate-prevention health wiring;
* Reddit registration;
* new news providers;
* destructive retention;
* real recurring shadow activation;
* remaining deferred corporate-action provider types;
* MFE/MAE;
* live promotion;
* Robinhood integration;
* live Alpaca trading;
* portfolio optimization using an LLM;
* reinforcement learning;
* strategy generation;
* backtesting an unlimited universe.

Those remain separate tasks.

---

# Step 1 — Inventory the existing paper architecture

Document the existing flow for:

```text
recommendation
→ execution eligibility
→ paper order intent
→ paper runtime
→ fill
→ ledger
→ position
→ reconciliation
→ evaluation
```

Identify:

* whether current tables assume one global paper account;
* where starting cash is stored;
* whether cash is derived or persisted;
* how positions are keyed;
* how fills are keyed;
* how idempotency works;
* how order intent IDs are derived;
* how fees and slippage are represented;
* whether partial fills exist;
* how realized P&L is calculated;
* how current position value is calculated;
* whether book/account identity exists;
* whether experiment-arm identity exists;
* where baseline-only submission is enforced;
* what prevents enhanced paper submission today.

Record confirmed gaps before implementing.

---

# Step 2 — Create an ADR

Create:

```text
docs/adr/0006-isolated-paper-books-and-portfolio-evaluation.md
```

The ADR must decide:

1. Whether a paper book is:

   * a logical namespace;
   * a ledger account;
   * a broker-paper account;
   * or a combination.

2. How baseline and enhanced books remain isolated.

3. How starting cash is configured.

4. How book identity enters:

   * order IDs;
   * fill IDs;
   * positions;
   * cash entries;
   * reconciliation;
   * evaluation.

5. How mark-to-market prices are selected point in time.

6. How stale or missing prices are represented.

7. How deterministic risk controls apply independently per book.

8. How the same recommendation timestamp is used for fair arm comparison.

9. Why enhanced paper execution does not create a path to live execution.

10. How promotion remains evidence-only.

11. How historical book records remain immutable.

Do not mark the ADR accepted until implementation and tests support its claims.

---

# Step 3 — Add paper-book configuration

Create a configuration such as:

```text
config/paper_books.yaml
```

Suggested structure:

```yaml
paper_books:
  enabled: false

  books:
    baseline:
      enabled: true
      book_id: BASELINE
      starting_cash_usd: "100000.00"

    enhanced:
      enabled: false
      book_id: ENHANCED
      starting_cash_usd: "100000.00"

  execution:
    provider: local_simulated
    allow_external_paper_broker: false
    allow_live_broker: false

  risk:
    max_position_weight: "0.10"
    max_order_notional_usd: "1000.00"
    max_daily_new_notional_usd: "5000.00"
    minimum_cash_buffer_weight: "0.10"
    max_open_positions: 20
    max_symbol_concentration_weight: "0.10"
    reject_stale_market_price_seconds: 900

  valuation:
    price_source: evidence_snapshot
    maximum_price_age_seconds: 900
    missing_price_policy: MARK_UNVALUED
```

These numbers are examples only.

Inspect existing project conventions and choose conservative defaults.

Requirements:

* shipped disabled by default;
* enhanced book disabled by default;
* external paper-broker submission disabled by default;
* live broker structurally impossible;
* Decimal money values;
* percentages bounded to `[0,1]`;
* unknown keys fail closed;
* invalid book IDs fail closed;
* duplicate book IDs fail closed;
* `.env` cannot enable a book;
* credentials cannot enable execution;
* configuration hash persisted.

---

# Step 4 — Define the paper-book model

Add a typed model such as:

```python
@dataclass(frozen=True)
class PaperBook:
    book_id: str
    experiment_arm: str
    currency: str
    starting_cash_usd: Decimal
    status: str
    created_at: datetime
    config_hash: str
```

Required experiment-arm vocabulary:

```text
BASELINE
ENHANCED
```

Suggested book states:

```text
ACTIVE
PAUSED
CLOSED
```

Requirements:

* immutable identity;
* stable deterministic book ID;
* one experiment arm per book;
* no arm reassignment;
* no shared account balance;
* no automatic reset;
* no mutation from Claude output;
* no live account reference required.

---

# Step 5 — Add additive paper-book persistence

Prefer additive schema.

Potential tables:

```text
paper_books
paper_book_cash_ledger
paper_book_orders
paper_book_fills
paper_book_positions
paper_book_position_lots
paper_book_snapshots
paper_book_snapshot_positions
paper_book_reconciliations
paper_book_daily_metrics
paper_book_experiment_comparisons
```

Do not add tables already represented cleanly by existing schema.

Every money-affecting record must include:

```text
book_id
experiment_arm
cycle_id
symbol
event_timestamp
idempotency_key
```

where applicable.

Requirements:

* Decimal values stored safely;
* additive migrations only;
* foreign keys where repository conventions permit;
* idempotent insert behavior;
* immutable fill history;
* immutable cash-ledger history;
* no destructive reset;
* no shared primary key across books;
* book-aware queries;
* old single-book records remain interpretable.

---

# Step 6 — Cash ledger

Use an append-only cash ledger rather than directly overwriting one balance.

Suggested event types:

```text
INITIAL_CAPITAL
BUY_RESERVATION
BUY_SETTLEMENT
SELL_SETTLEMENT
FEE
SLIPPAGE
DIVIDEND
CASH_ADJUSTMENT
ORDER_RELEASE
```

`CASH_ADJUSTMENT` must require:

* operator;
* reason;
* timestamp;
* audit record.

Requirements:

* available cash is derived from ledger entries;
* reserved cash is separate from settled cash;
* enhanced and baseline cash never mix;
* no negative available cash unless explicitly allowed by policy—which this milestone should not allow;
* no margin;
* no fabricated deposit;
* idempotent settlement;
* reversal entries rather than historical mutation.

---

# Step 7 — Position and lot accounting

Use deterministic long-only lot accounting.

Support:

```text
BUY
SELL
```

Do not support:

```text
SHORT
COVER
OPTION
MARGIN
```

Choose and document a cost-basis method:

```text
FIFO
```

is recommended unless an existing method already exists.

For each book and symbol track:

* quantity;
* available quantity;
* reserved quantity;
* average cost;
* lot-level cost;
* realized P&L;
* fees;
* latest valuation price;
* unrealized P&L;
* valuation timestamp;
* valuation status.

Requirements:

* no sell quantity greater than available long position;
* no cross-book lot consumption;
* partial fills handled deterministically if existing runtime supports them;
* duplicate fill cannot be applied twice;
* reversals are explicit;
* historical lots are immutable;
* realized P&L can be recomputed from fills.

---

# Step 8 — Point-in-time portfolio snapshots

Create a snapshot model:

```python
@dataclass(frozen=True)
class PaperPortfolioSnapshot:
    snapshot_id: str
    book_id: str
    as_of: datetime
    cash_available_usd: Decimal
    cash_reserved_usd: Decimal
    gross_market_value_usd: Decimal | None
    net_liquidation_value_usd: Decimal | None
    total_cost_basis_usd: Decimal
    unrealized_pnl_usd: Decimal | None
    realized_pnl_usd: Decimal
    position_count: int
    unvalued_position_count: int
    stale_position_count: int
    valuation_status: str
    source_hash: str
```

Suggested valuation statuses:

```text
COMPLETE
PARTIAL_MISSING_PRICE
PARTIAL_STALE_PRICE
POINT_IN_TIME_UNSAFE
SOURCE_UNAVAILABLE
```

Requirements:

* snapshot immutable;
* one `as_of` for all positions;
* no current-price leakage into a historical snapshot;
* price provenance persisted;
* missing price does not become zero;
* stale price does not silently become current;
* net liquidation value remains `None` when required positions cannot be valued safely;
* cash is always book-specific;
* snapshot hash includes positions and prices.

---

# Step 9 — Price selection for mark-to-market

Reuse point-in-time market evidence when possible.

Selection priority should be deterministic and documented.

Potential order:

```text
1. Price from the same cycle’s point-in-time EvidenceSnapshot
2. Most recent safe persisted market bar available by snapshot.as_of
3. Explicit SOURCE_UNAVAILABLE
```

Do not:

* call a live quote for a historical snapshot;
* select a price accepted after `as_of`;
* silently forward-fill beyond the configured staleness limit;
* infer price from cost basis;
* use zero.

Persist:

* provider;
* timestamp;
* price;
* available-at time;
* point-in-time-safe flag;
* source-record ID;
* staleness seconds.

---

# Step 10 — Portfolio context for deterministic sizing

Add a typed portfolio context consumed by deterministic execution policy:

```python
@dataclass(frozen=True)
class PaperPortfolioContext:
    book_id: str
    as_of: datetime
    available_cash_usd: Decimal
    reserved_cash_usd: Decimal
    net_liquidation_value_usd: Decimal | None
    current_position_quantity: Decimal
    current_position_market_value_usd: Decimal | None
    current_position_weight: Decimal | None
    open_position_count: int
    daily_new_notional_usd: Decimal
    valuation_status: str
```

This context must not be produced by Claude.

The same candidate may yield different order sizes across books because their portfolios differ.

That is expected, but the reason must be deterministic and persisted.

---

# Step 11 — Deterministic risk policy

Create a paper-only risk decision:

```python
@dataclass(frozen=True)
class PaperRiskDecision:
    decision: str
    requested_notional_usd: Decimal | None
    approved_notional_usd: Decimal | None
    approved_quantity: Decimal | None
    reasons: tuple[str, ...]
    policy_version: str
```

Suggested decisions:

```text
APPROVED
APPROVED_REDUCED
REJECTED_INSUFFICIENT_CASH
REJECTED_MAX_POSITION_WEIGHT
REJECTED_MAX_SYMBOL_CONCENTRATION
REJECTED_MAX_OPEN_POSITIONS
REJECTED_DAILY_NOTIONAL_LIMIT
REJECTED_STALE_PRICE
REJECTED_MISSING_PRICE
REJECTED_INVALID_RECOMMENDATION
REJECTED_BOOK_PAUSED
REJECTED_ARM_MISMATCH
```

Requirements:

* deterministic;
* Decimal arithmetic;
* long-only;
* limit orders only;
* no margin;
* no negative cash;
* no model override;
* no book override;
* decisions persisted;
* same inputs produce same decision;
* risk policy versioned.

---

# Step 12 — Paper order intent

Create a book-aware immutable order intent.

Required fields:

```text
paper_order_intent_id
book_id
experiment_arm
cycle_id
recommendation_id
symbol
side
order_type
quantity
limit_price
notional_usd
time_in_force
as_of
risk_decision_id
portfolio_snapshot_id
config_hash
created_at
status
```

Requirements:

* limit orders only;
* stable idempotency key includes `book_id`;
* baseline and enhanced intents for the same recommendation must have different IDs;
* one intent cannot change books;
* one intent cannot change experiment arms;
* rejected risk decisions never create submit-ready intents;
* enhanced intent can target only the enhanced paper book;
* no live-broker field;
* immutable after creation except lifecycle status through explicit events.

---

# Step 13 — Enable isolated enhanced paper submission

Inspect the existing experiment policy.

Add support for:

```text
BASELINE_ONLY
ENHANCED_ONLY
BOTH_SEPARATE_PAPER_BOOKS
```

Only when paper-book configuration explicitly enables the matching isolated book.

Requirements:

* shipped policy remains conservative;
* enhanced submission disabled by default;
* enhanced paper execution cannot occur without:

  * enhanced book enabled;
  * policy allowing enhanced paper;
  * recommendation passing validation;
  * evidence completeness permitting research;
  * deterministic risk approval;
  * local paper execution enabled.
* no shared account;
* no live path;
* no accidental fallback from enhanced to baseline;
* unsupported policy combinations fail closed;
* policy decision persisted.

Do not change the existing rule that enhanced recommendations cannot be submitted to any live destination.

---

# Step 14 — Fair experiment assignment

Ensure baseline and enhanced arms are compared fairly.

For a given cycle and symbol, persist:

* experiment assignment;
* baseline recommendation ID;
* enhanced recommendation ID;
* shared evidence snapshot ID;
* shared `as_of`;
* shared candidate input;
* shared market assumptions where applicable;
* baseline book ID;
* enhanced book ID;
* paper-order intent IDs;
* execution outcomes.

Requirements:

* same evidence cutoff;
* same recommendation timestamp window;
* no future data;
* no selective assignment after seeing results;
* no dropping failed enhanced cycles from evaluation;
* no cross-arm order contamination;
* assignment immutable.

---

# Step 15 — Local paper execution

Reuse the existing paper runtime where safe.

Add book identity to the process boundary.

The paper runtime must receive only bounded order-intent data:

```text
book_id
paper_order_intent_id
symbol
side
quantity
limit_price
time_in_force
market simulation inputs
```

It must not receive:

* Claude prompts;
* Claude responses;
* API keys;
* live broker credentials;
* research chain-of-thought;
* unrelated portfolio state.

Requirements:

* local simulated fills by default;
* deterministic fixture execution for tests;
* book-aware idempotency;
* book-aware fills;
* no fill shared across books;
* no live execution;
* process-boundary protocol updated compatibly;
* paper runtime tests remain isolated.

---

# Step 16 — Fill and order simulation

Document and preserve the existing fill model.

At minimum support:

```text
PENDING
FILLED
PARTIALLY_FILLED
CANCELLED
EXPIRED
REJECTED
```

If the existing runtime does not support partial fills, do not invent them casually. Record them as deferred.

Fill simulation must be based on point-in-time price data.

Do not guarantee a fill merely because an order exists.

Persist:

* simulated market price;
* limit-price comparison;
* fill quantity;
* fill price;
* fees;
* slippage;
* fill timestamp;
* simulation rule version.

---

# Step 17 — Book-aware reconciliation

Implement deterministic reconciliation among:

```text
paper order intents
paper runtime orders
paper fills
cash ledger
position lots
position aggregates
portfolio snapshots
```

Reconciliation statuses:

```text
MATCHED
MISSING_ORDER
MISSING_FILL
DUPLICATE_FILL
CASH_MISMATCH
POSITION_MISMATCH
LOT_MISMATCH
BOOK_MISMATCH
ARM_MISMATCH
PENDING_NOT_APPLICABLE
```

Requirements:

* one book reconciled independently;
* no cross-book balancing;
* a mismatch in one book cannot be hidden by the other;
* exact Decimal arithmetic;
* mismatch details persisted;
* reconciliation does not rewrite history;
* operator repair uses explicit compensating events;
* no Claude involvement.

---

# Step 18 — Corporate actions

For Milestone 8, support only what is already safely modeled and required for paper-book correctness.

At minimum consider:

```text
forward split
reverse split
cash dividend
```

Use fixture events unless the real corporate-action provider is already validated.

Requirements:

* book-specific application;
* lot quantities adjusted for splits;
* cost basis preserved correctly;
* cash dividends credited to the correct book;
* effective date and entitlement date respected;
* one event applied once;
* no event inferred from price movement;
* unsupported action type remains explicit and unapplied.

Do not broaden the provider-integration scope from the Milestone 7 backlog.

---

# Step 19 — Performance metrics

Calculate per book:

```text
starting cash
ending cash
net liquidation value
realized P&L
unrealized P&L
total return
daily return
cumulative return
maximum drawdown
volatility
turnover
trade count
win rate
average win
average loss
profit factor
fees
slippage
cash utilization
average gross exposure
maximum position concentration
unvalued-position rate
stale-valuation rate
```

Only compute a metric when the required data is available.

Requirements:

* missing data remains unavailable;
* no division by zero;
* no fabricated zero return;
* Decimal money calculations;
* explicit sample window;
* point-in-time valuation;
* metric versioning;
* same calendar and cutoff across arms.

Do not add Sharpe or other annualized ratios unless sample-size and annualization semantics are clearly defined.

---

# Step 20 — Baseline-versus-enhanced comparison

Create a comparison result:

```python
@dataclass(frozen=True)
class PaperExperimentComparison:
    comparison_id: str
    experiment_id: str
    baseline_book_id: str
    enhanced_book_id: str
    window_start: datetime
    window_end: datetime
    baseline_metrics_id: str
    enhanced_metrics_id: str
    comparable: bool
    comparability_reasons: tuple[str, ...]
    metric_deltas: Mapping[str, Decimal | None]
    policy_version: str
```

Comparability must fail closed when:

* valuation windows differ;
* evidence cutoffs differ;
* one book has unsafe prices;
* one arm has missing cycles;
* corporate actions were applied inconsistently;
* starting cash differs unexpectedly;
* one arm was selectively disabled;
* sample size is insufficient.

Do not automatically declare the enhanced arm better.

---

# Step 21 — Promotion evidence only

Extend promotion reporting with paper-book evidence.

Potential inputs:

* minimum comparable cycles;
* minimum trading days;
* minimum closed trades;
* return difference;
* drawdown difference;
* cost difference;
* evidence completeness;
* failure rates;
* unvalued-position rate;
* reconciliation status;
* operational health.

Possible result:

```text
INSUFFICIENT_DATA
NOT_COMPARABLE
BASELINE_OUTPERFORMS
ENHANCED_OUTPERFORMS_OBSERVED
ENHANCED_OUTPERFORMS_NOT_PROMOTABLE
PROMOTION_REVIEW_ELIGIBLE
```

Requirements:

* no automatic promotion;
* no live execution;
* no changing recommendation authority;
* minimum sample floors;
* confidence language remains conservative;
* operational failures block review eligibility;
* one strong trade cannot produce promotion eligibility.

---

# Step 22 — CLI commands

Add commands consistent with existing CLI conventions.

Suggested commands:

```bash
python -m trading_research.cli paper-book-list

python -m trading_research.cli paper-book-show \
  --book-id BASELINE

python -m trading_research.cli paper-book-snapshot \
  --book-id BASELINE \
  --as-of 2026-07-13T20:00:00Z

python -m trading_research.cli paper-book-run-cycle \
  --cycle-id <id> \
  --experiment-policy BOTH_SEPARATE_PAPER_BOOKS \
  --provider-mode fixture

python -m trading_research.cli paper-book-reconcile \
  --book-id BASELINE

python -m trading_research.cli paper-experiment-compare \
  --experiment-id <id>

python -m trading_research.cli paper-promotion-status \
  --experiment-id <id>
```

Requirements:

* JSON output;
* sanitized;
* deterministic ordering;
* no account credentials;
* no live mode;
* book identity explicit;
* mutating commands require paper configuration enabled;
* enhanced book disabled by default;
* missing book fails closed.

---

# Step 23 — Offline end-to-end test

Add an offline integration test proving:

```text
one shared evidence snapshot
→ baseline recommendation
→ enhanced recommendation
→ BASELINE book risk decision
→ ENHANCED book risk decision
→ independent order intents
→ independent paper-runtime submissions
→ independent fills
→ independent cash ledgers
→ independent positions
→ mark-to-market snapshots
→ independent reconciliation
→ performance metrics
→ comparison result
→ no live execution
```

Use intentionally different risk or portfolio state so the same symbol may produce different approved quantities across books.

Verify that this difference is deterministic and not cross-contamination.

---

# Step 24 — Isolation tests

Add explicit tests proving:

* baseline cash cannot satisfy an enhanced order;
* enhanced cash cannot satisfy a baseline order;
* baseline position cannot satisfy an enhanced sell;
* enhanced fill cannot update baseline positions;
* same recommendation creates different book-aware intent IDs;
* duplicate fill is idempotent within one book;
* fill ID reused in another book does not silently collide;
* cross-book reconciliation fails;
* arm mismatch fails;
* enhanced order cannot target baseline book;
* baseline order cannot target enhanced book;
* disabling enhanced book prevents enhanced intent creation;
* no fallback to baseline book occurs;
* no live destination exists.

---

# Step 25 — Portfolio and risk tests

Add tests for:

* initial capital;
* available versus reserved cash;
* insufficient cash;
* maximum order notional;
* maximum position weight;
* maximum symbol concentration;
* maximum open positions;
* daily new-notional limit;
* minimum cash buffer;
* stale price;
* missing price;
* partial valuation;
* book paused;
* buy settlement;
* sell settlement;
* FIFO lot consumption;
* realized P&L;
* unrealized P&L;
* fees;
* slippage;
* duplicate settlement;
* compensating cash adjustment.

---

# Step 26 — Point-in-time tests

Add tests proving:

* historical snapshot excludes later price;
* future fill excluded;
* future corporate action excluded;
* stale price labeled stale;
* missing price does not become zero;
* unsafe source blocks complete valuation;
* same `as_of` used across comparison books;
* current quote never substitutes for historical price;
* snapshot ID changes when valuation inputs change;
* snapshot ID remains stable for identical inputs.

---

# Step 27 — Experiment-comparison tests

Add tests for:

* comparable books;
* different starting cash;
* different evaluation windows;
* missing enhanced cycle;
* unsafe valuation;
* inconsistent corporate action;
* insufficient sample size;
* baseline outperformance;
* enhanced observed outperformance;
* enhanced not promotion-ready;
* operational health block;
* reconciliation block;
* no automatic promotion.

---

# Step 28 — Optional external paper-broker validation

Do not perform an external paper-broker submission unless:

* an existing paper-only broker integration already exists;
* account environment is verifiably paper;
* user explicitly approves the mutation;
* paper books can be isolated safely;
* no live credential path is reachable.

The default milestone validation should use the local simulated paper runtime.

If an external paper smoke is explicitly approved later:

* one symbol;
* one small paper-only limit order;
* one book only;
* no live account;
* no real money;
* cancellation after validation when appropriate;
* sanitized output only.

Do not perform this automatically.

---

# Step 29 — Documentation

Create:

```text
docs/milestone8-isolated-paper-portfolios.md
docs/runbooks/paper-book-operations.md
docs/runbooks/paper-book-reconciliation.md
```

Update as needed:

```text
docs/adr/0006-isolated-paper-books-and-portfolio-evaluation.md
docs/milestone7-pending-work.md
docs/runbooks/shadow-operations.md
```

Document:

* paper-book model;
* isolation guarantees;
* cash ledger;
* position accounting;
* valuation;
* risk controls;
* experiment policy;
* enhanced paper-only boundary;
* execution flow;
* reconciliation;
* corporate actions;
* performance metrics;
* comparison;
* promotion evidence;
* CLI commands;
* test results;
* limitations.

Clearly distinguish:

```text
LOCAL-SIMULATED-PAPER
EXTERNAL-PAPER-BROKER
LIVE-BROKER
```

Only the first is required for Milestone 8 completion.

---

# Security and safety review

Before completion verify:

* no live broker path added;
* no `--live` CLI flag;
* no live credentials read;
* no Robinhood mutation;
* no enhanced-to-baseline fallback;
* no baseline-to-enhanced fallback;
* no shared cash;
* no shared positions;
* no shared fills;
* no cross-book lot consumption;
* no Claude-selected book;
* no Claude risk override;
* no negative cash;
* no margin;
* no short position;
* no unsupported order type;
* no mutable fill history;
* no mutable recommendation history;
* no current-price leakage;
* no missing price converted to zero;
* no unknown P&L converted to zero;
* no automatic promotion;
* no recurring deployment activation;
* `real_orders` remains write-blocked;
* existing tests were not weakened or deleted.

---

# Suggested implementation order

Proceed in this order:

1. Create the Milestone 8 scratchpad.
2. Verify Git state and baseline.
3. Inventory current paper/execution/evaluation architecture.
4. Confirm gaps.
5. Draft ADR 0006.
6. Add disabled-by-default paper-book config.
7. Add paper-book identity.
8. Add additive persistence.
9. Implement cash ledger.
10. Implement position and lot accounting.
11. Implement point-in-time portfolio snapshots.
12. Implement price selection and staleness handling.
13. Add deterministic portfolio context.
14. Add risk policy.
15. Add book-aware order intents.
16. Add enhanced isolated paper policy.
17. Thread book identity through paper runtime.
18. Implement fill application.
19. Implement reconciliation.
20. Add supported corporate-action handling.
21. Add performance metrics.
22. Add experiment comparisons.
23. Extend promotion evidence.
24. Add CLI commands.
25. Add unit tests.
26. Add isolation tests.
27. Add offline end-to-end tests.
28. Run targeted tests.
29. Run full main suite.
30. Run paper-runtime suite.
31. Update ADR and documentation.
32. Complete safety review.
33. Finalize scratchpad.
34. Do not commit or push unless explicitly requested.

---

# Acceptance criteria

Milestone 8 is complete only when:

1. Existing 1,266 main tests continue to pass.
2. Existing 33 paper-runtime tests continue to pass.
3. Baseline and enhanced books have separate identities.
4. Baseline and enhanced books have separate cash.
5. Baseline and enhanced books have separate positions.
6. Baseline and enhanced books have separate orders and fills.
7. Cross-book contamination is structurally tested.
8. Enhanced paper submission remains disabled by default.
9. Enhanced paper submission requires an isolated enhanced book.
10. Enhanced submission can never reach live execution.
11. Book-aware order IDs are deterministic.
12. Cash is append-only ledger-derived.
13. Position lots are deterministic and book-specific.
14. Negative cash is prohibited.
15. Short positions are prohibited.
16. Margin is prohibited.
17. Portfolio snapshots are point-in-time safe.
18. Missing prices never become zero.
19. Stale prices are explicit.
20. Net liquidation value remains unavailable when valuation is incomplete.
21. Deterministic risk controls apply independently per book.
22. The same recommendation can produce different approved sizes only for persisted deterministic portfolio reasons.
23. Paper runtime receives explicit book identity.
24. Duplicate fills are idempotent.
25. Reconciliation is book-specific.
26. Supported corporate actions apply once per book.
27. Performance metrics are book-specific.
28. Baseline and enhanced evaluation windows align.
29. Non-comparable books fail closed.
30. Promotion remains evidence-only.
31. No automatic promotion exists.
32. No live-trading path exists.
33. No recurring scheduler is activated.
34. Documentation matches implementation.
35. No commit or push occurs unless explicitly requested.

---

# Required final response

At completion provide:

1. Git state and baseline.
2. Scratchpad path and final status.
3. Existing architecture findings.
4. Confirmed gaps.
5. ADR decision summary.
6. Paper-book model.
7. Isolation guarantees.
8. Configuration.
9. Schema changes.
10. Cash-ledger behavior.
11. Position and lot accounting.
12. Mark-to-market behavior.
13. Missing/stale price behavior.
14. Portfolio context.
15. Risk-policy decisions.
16. Book-aware order intent.
17. Enhanced paper-only policy.
18. Paper-runtime changes.
19. Fill behavior.
20. Reconciliation.
21. Corporate-action handling.
22. Performance metrics.
23. Experiment comparison.
24. Promotion evidence.
25. CLI commands.
26. Files created.
27. Files modified.
28. Tests added.
29. Targeted-test results.
30. Full main-suite result.
31. Paper-runtime result.
32. Optional external paper validation, if explicitly approved.
33. Proof of no cross-book contamination.
34. Proof of no live execution.
35. Security review.
36. Known limitations.
37. Recommended Milestone 9 scope.

Include:

```text
Requirement → implementation file → verifying test
```

Use labels:

```text
OFFLINE-DETERMINISTIC
PAPER-BOOK-ISOLATED
BASELINE-PAPER-ONLY
ENHANCED-PAPER-ONLY
POINT-IN-TIME-VALUED
PORTFOLIO-RISK-ENFORCED
BOOK-RECONCILED
EXPERIMENT-COMPARABLE
PROMOTION-EVIDENCE-ONLY
LIVE-TRADING-NOT-IMPLEMENTED
ACTUAL-RECURRING-DEPLOYMENT-NOT-ACTIVATED
ENVIRONMENTALLY-PENDING
```

Do not claim Milestone 8 is complete if baseline and enhanced books share cash, positions, orders, fills, or reconciliation state.

Do not commit or push unless explicitly asked.
