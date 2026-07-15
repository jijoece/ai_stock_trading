# Pitfalls and Areas of Improvement

This document provides a detailed analysis of the current implementation and architecture of the Agentic Trading Desk system. It highlights several hidden pitfalls (transactional, numerical, pipeline, and data integration related) and proposes corresponding areas of improvement.

---

## 1. Transactional and Database Pitfalls

### 1.1 Python `sqlite3` Implicit Transactions and `BEGIN IMMEDIATE`
- **Location:** [lease.py:L75](file:///Users/jijopaul/workspace/agentic-trading-desk/src/trading_research/shadow/lease.py#L75)
- **Pitfall:** The system uses `conn.execute("BEGIN IMMEDIATE")` inside [lease.py](file:///Users/jijopaul/workspace/agentic-trading-desk/src/trading_research/shadow/lease.py) to manage processes competing for the singleton run lease. However, by default, Python's `sqlite3` module manages transactions implicitly (`isolation_level` defaults to `""`), starting them automatically when it encounters certain DML statements but not tracking manual transaction control statements like `BEGIN IMMEDIATE` correctly. This can lead to collisions between Python's implicit transaction state and SQLite's database-level locks, causing unexpected `sqlite3.ProgrammingError: cannot start a transaction within a transaction` or transient lock contention errors.
- **Improvement:** Configure connection initialization in [database.py](file:///Users/jijopaul/workspace/agentic-trading-desk/src/trading_research/storage/database.py) to use `isolation_level=None`. This puts the connections in autocommit mode, delegating transaction state management entirely to SQL statements and making manual transaction blocks (such as `BEGIN IMMEDIATE` / `COMMIT`) fully robust and deterministic.

---

## 2. Paper Ledger Simulation Pitfalls

### 2.1 Cash Settlement on Calendar Days instead of Trading Days
- **Location:** [ledger.py:L215-L216](file:///Users/jijopaul/workspace/agentic-trading-desk/src/trading_research/paper/ledger.py#L215-L216)
- **Pitfall:** Cash settlement from stock sales is calculated using calendar days:
  ```python
  settle_date = (_today(now) + timedelta(days=1)).isoformat()
  ```
  If an asset is sold on Friday, `settle_date` is computed as Saturday. Because there are no market sessions over the weekend, if the scheduler runs on Monday, the cash appears fully settled and available for redeployment. In real cash accounts, proceeds from a Friday trade settle on Monday (or Tuesday if Monday is a market holiday) under standard T+1 rules. This mismatch artificially inflates portfolio liquidity and trade velocity during weekends or holidays.
- **Improvement:** Modify [ledger.py](file:///Users/jijopaul/workspace/agentic-trading-desk/src/trading_research/paper/ledger.py) to compute settlement using business days based on the market calendar defined in [market_calendar.py](file:///Users/jijopaul/workspace/agentic-trading-desk/src/trading_research/evaluation/market_calendar.py).

### 2.2 Look-ahead Bias in Snapshot Peak Equity and Drawdown
- **Location:** [ledger.py:L294-L297](file:///Users/jijopaul/workspace/agentic-trading-desk/src/trading_research/paper/ledger.py#L294-L297)
- **Pitfall:** When recording a daily snapshot in the paper ledger, peak equity is determined using the entire historical table:
  ```sql
  SELECT MAX(equity) AS peak FROM simulated_portfolio_snapshots
  ```
  If snapshots are backfilled, loaded, or fixed out of order, this query picks up "future" peak equity values that occur after the snapshot date currently being processed. This introduces look-ahead bias and distorts historical drawdown statistics.
- **Improvement:** Restrict the query to only look at snapshots prior to or equal to the current snapshot date:
  ```sql
  SELECT MAX(equity) AS peak FROM simulated_portfolio_snapshots WHERE snap_date <= ?
  ```

### 2.3 Rigid Snapshot Failure on Missing Mark Price
- **Location:** [ledger.py:L288-L290](file:///Users/jijopaul/workspace/agentic-trading-desk/src/trading_research/paper/ledger.py#L288-L290)
- **Pitfall:** If any ticker currently held in the portfolio is missing a mark price (due to a temporary data provider outage, ticker symbol change, halt, or delisting), the snapshot method raises a hard `LedgerError` and aborts. This stops the snapshot calculation for the entire portfolio, halting all reporting and shadow operations.
- **Improvement:** Introduce a fallback pricing strategy (e.g., using the last known price from [price_bars](file:///Users/jijopaul/workspace/agentic-trading-desk/src/trading_research/storage/trading_schema.py#L26) or caching the last marked price) and raise a data-quality warning instead of failing the entire snapshot pass.

---

## 3. Configuration and Quality Pitfalls

### 3.1 Hardcoded Reddit Sentiment Cap
- **Location:** [cli.py:L37](file:///Users/jijopaul/workspace/agentic-trading-desk/src/trading_research/cli.py#L37) & `scorer.py`
- **Pitfall:** The maximum weight for Reddit sentiment is defined as a hardcoded constant:
  ```python
  REDDIT_WEIGHT = 0.10
  ```
  This duplicates the constant across files and prevents operators from tuning the sentiment weight easily via configuration.
- **Improvement:** Centralize the sentiment weight cap inside [scoring.yaml](file:///Users/jijopaul/workspace/agentic-trading-desk/config/scoring.yaml) and parse it dynamically during runtime scoring.

### 3.2 Single-Symbol Provider Failure Sensitivity
- **Location:** [health.py:L320-L329](file:///Users/jijopaul/workspace/agentic-trading-desk/src/trading_research/shadow/health.py#L320-L329)
- **Pitfall:** The operational health check triggers `PAUSE_REQUIRED` based on `provider_failure_rate` calculated over a single cycle. If the cycle candidate set is small (e.g. 1 or 2 symbols), a single data retrieval failure or rate limit hit on Robinhood or SEC EDGAR yields a 50% or 100% failure rate, which immediately halts the shadow operations. This results in significant operational friction due to temporary external network/API issues.
- **Improvement:** Implement a minimum candidate size limit before calculating rates, or evaluate health metrics using a rolling average window (e.g., across the last 3-5 cycles) to filter out transient network anomalies.

---

## 4. Scraper and Text Extraction Fragility

### 4.1 Keyword Extraction Boundaries on SEC filings
- **Location:** [disclosure_extraction.py:L67-L70](file:///Users/jijopaul/workspace/agentic-trading-desk/src/trading_research/evidence_providers/disclosure_extraction.py#L67-L70)
- **Pitfall:** The going-concern explicit regex matches words between "substantial doubt" and "ability to continue as a going concern" with a strict character window limit:
  ```python
  _GOING_CONCERN_EXPLICIT_RE = re.compile(
      r"substantial\s+doubt\b.{0,80}?\bability\s+to\s+continue\s+as\s+a\s+going\s+concern",
      re.IGNORECASE | re.DOTALL,
  )
  ```
  If a filing contains descriptive legal text or disclosures inside this clause that exceed 80 characters (e.g., listing reasons for financial distress), the explicit regex will fail to match. While it safely triggers the `AMBIGUOUS_DISCLOSURE` fallback regex, it introduces state fluctuation (shifting between `EXPLICIT_DISCLOSURE_FOUND` and `AMBIGUOUS_DISCLOSURE` based on character length), complicating automated screening flags.
- **Improvement:** Tune the character window limit or provide multiple specific regex variations to match standard PCAOB disclosures without relying on a rigid character window constraint.
