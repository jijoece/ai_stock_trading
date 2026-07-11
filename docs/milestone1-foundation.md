# Milestone 1 foundation — developer guide

Covers the first slice of Milestone 1 (see
[AI-Stock-Trading-Implementation-Plan.md](AI-Stock-Trading-Implementation-Plan.md)):
Stories 1A.1–1A.3 and 1B.1. Everything described here runs fully offline —
no Robinhood, Reddit, market-data, or Claude API access required.

**Status: research and paper-trading only.** No component in this slice
places, previews, or prepares a real order. `real_orders` exists purely as a
reserved schema (see below); no code path in this repository writes to it.

## Trading database schema (`storage/trading_schema.py`)

`apply_trading_schema(conn)` applies the Phase 1 trading tables (securities,
price bars, fundamentals, screening/scoring, recommendations, the paper
ledger, approvals, evaluation, and audit tables — full list in the
architecture doc §17) to any SQLite connection. It is idempotent: safe to
call on every connection open, safe to call twice on the same connection.

It is composed of, in order:
1. **Legacy rename guard** — see "Table name history" below.
2. **DDL** (`CREATE TABLE IF NOT EXISTS` for every table).
3. **Column upgrades** — idempotent `ALTER TABLE ... ADD COLUMN` for columns
   added after a table first shipped (e.g. `config_hash`/`git_sha` on
   `recommendations`), so existing databases pick up new columns without a
   migration framework.
4. **Indexes** (`CREATE INDEX IF NOT EXISTS`).
5. **Triggers** enforcing two fail-closed invariants at the database layer,
   not just in application code:
   - `recommendations` rows become immutable once `frozen = 1` — any
     `UPDATE`/`DELETE` on a frozen row aborts. Evaluation results are written
     to `evaluation_results` (a separate table keyed by `rec_id`), never back
     onto the recommendation row.
   - `real_orders` rejects every `INSERT`/`UPDATE`/`DELETE` unconditionally.
     The table exists so the schema for a later, explicitly gated real-money
     phase is already reviewable, but nothing in this codebase can write to
     it — the trigger makes that a database-enforced fact, not just a
     convention.

`storage/database.py`'s `connect()` / `session()` apply both the research-run
schema (`migrations.py`) and the trading schema on every connection, so a
single `.sqlite3` file holds both without the caller needing to know which
schema owns which table.

### Table name history

An early version of `migrations.py` used the name `recommendations` for a
research-run table (columns: `workstream_id`, `recommendation`,
`confidence`). The architecture's trading schema (§17) also calls its frozen,
`rec_id`-keyed table `recommendations` — the correct owner of that name per
the architecture doc. This slice resolves the collision by renaming the
research-run table to `research_recommendations`
(`storage/migrations.py::rename_legacy_research_recommendations`), which runs
automatically before either schema's DDL. It detects the legacy table by
shape (has `workstream_id`, lacks `rec_id`) so it's a no-op on fresh
databases and on databases that already have the trading-schema table. No
code in this repository ever read or wrote rows through the legacy shape, and
the local `data/research.sqlite3` had zero rows in the affected table, so
this is a pure rename with no data migration required.

### Running it

```bash
python3 -c "
import sqlite3
from trading_research.storage.trading_schema import apply_trading_schema
conn = sqlite3.connect(':memory:')
apply_trading_schema(conn)
print('tables:', len(conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()))
"
```

Or just use `storage.database.connect(path)` / `session(path)`, which does
this (and the research schema) for you.

## Verified ticker universe (`universe/tickers.py`)

`TickerUniverse` is the **only** authority on which symbols exist in this
system. Nothing else — not an LLM, not a heuristic — is allowed to decide a
symbol is valid.

- `default_universe()` returns a small embedded seed (`_SEED`) covering
  common names plus every symbol required to exercise the ambiguity rules
  (`AI`, `IT`, `ON`, `ALL`, `SO`, `A`, `FOR`, `ARE`, plus a few more), an OTC
  example (rejected), and an inactive/delisted example (rejected).
- `TickerUniverse.from_csv(path)` loads a universe from a CSV with columns
  `symbol,name,exchange[,sector,is_otc,is_active,source]`. **This is the
  intended replacement path**: point it at a full exchange-listing export
  later and every downstream consumer (extraction, screening, scoring) picks
  it up unchanged — no code changes required elsewhere.
- `normalize_symbol(raw)` is the single normalization path: strips
  whitespace, drops a leading `$`, upper-cases. Raises `UnknownSymbolError`
  for input that cannot possibly be a symbol (empty, embedded whitespace).
- `universe.is_valid(symbol)` — true only for a symbol that is present,
  **not** OTC, and **active**. Anything else (unknown, OTC, inactive) is
  invalid — this is the fail-closed default.
- `universe.require(symbol)` — same check, but raises `UnknownSymbolError`
  instead of returning a bool, for call sites that should hard-fail on an
  unverified symbol rather than silently skip it.

### Ambiguous symbols

`AMBIGUOUS_SYMBOLS` is a fixed set of tickers that are also ordinary English
words (`AI`, `IT`, `ON`, `ALL`, `SO`, `A`, `FOR`, `ARE`, and others). A bare
(non-cashtag) mention of one of these needs contextual confirmation before it
counts as a ticker reference — see the extractor section below.
`universe.is_ambiguous(symbol)` exposes the flag; `universe.name_tokens(symbol)`
returns the lowercased company-name tokens used for one of the confirmation
rules (with common corporate suffixes **and the symbol's own spelling**
stripped — see the correctness note below).

## Recommendation JSON schema (`schemas/recommendation.schema.json`)

Draft-07, `additionalProperties: false`. A recommendation record is designed
to be frozen at creation (`frozen: true` is a schema `const`) and never
mutated afterward — see the DB trigger above for the enforcement side.

Key fields: `rec_id`, `run_id`, `symbol`, `side`
(`buy_candidate`/`watch`/`no_action`/`analysis_incomplete`), `ts`,
`price_at_rec`, `score`, `confidence`, `status`
(`active`/`expired`/`analysis_incomplete`), `factors[]`, `risk_plan`,
`warnings[]`, `missing_data_reasons[]`, `data_timestamps{}`,
`reddit_component`, `model_version`, `prompt_version`, `config_hash`
(sha256 of the *non-secret* configuration used for the run), `git_sha`
(the commit the code ran from, or `"unknown"`), `frozen`, and a `disclaimer`
that is pinned by the schema to the exact research-only wording.

Two `allOf`/`if`/`then` rules enforce the safety property that an incomplete
or no-action analysis can never carry executable order instructions:
- `status == "analysis_incomplete"` ⇒ `risk_plan` must be `null` and
  `missing_data_reasons` must have at least one entry (the fail-closed
  reason is always recorded, never silently dropped).
- `side` in (`no_action`, `analysis_incomplete`) ⇒ `risk_plan` must be `null`.

There is intentionally no field for a broker account identifier or
credential — `additionalProperties: false` rejects any such field outright
(see `invalid_account_identifier.json` fixture).

### Validating a recommendation JSON document

```python
import json
from jsonschema import Draft7Validator
from trading_research.config import REPO_ROOT

schema = json.loads((REPO_ROOT / "schemas" / "recommendation.schema.json").read_text())
validator = Draft7Validator(schema)
rec = json.loads(open("my_recommendation.json").read())
errors = list(validator.iter_errors(rec))
```

The CLI's `analyze` command does exactly this before printing its output and
exits non-zero on a schema violation (`cli.py::main`).

### Fixtures

`tests/fixtures/recommendations/` has `valid_*.json` (active buy candidate,
no-action, analysis-incomplete) and `invalid_*.json` fixtures covering each
safety rule individually (order details on an incomplete analysis, missing
`missing_data_reasons`, order details on a no-action result, overweight
Reddit component, unfrozen record, a smuggled account identifier, a missing
`git_sha`, a lowercase symbol, and a tampered disclaimer).

## Ticker mention parser (`analysis/ticker_extractor.py`)

Deterministic. Never calls an LLM, never calls Reddit or Robinhood, never
mutates the source text, never accepts a symbol the universe doesn't verify.

Matching rules, in priority order:
1. **Cashtags** (`$AAPL`) count if the symbol is verified — cashtags of
   ambiguous symbols count too, since the `$` itself is unambiguous intent.
2. **Bare uppercase tokens** count if verified **and** either the symbol
   isn't ambiguous, or the surrounding text confirms it: either a finance
   word (`stock`, `earnings`, `ticker`, `target`, …) within a 60-character
   window, or a company-name token co-mentioned anywhere in the text.
3. Anything not in the universe is never a mention.

Each `TickerMention` carries a deterministic **confidence category**:

| Confidence | When |
|---|---|
| `high` | cashtag match |
| `medium` | bare, unambiguous symbol |
| `low` | bare, ambiguous symbol, contextually confirmed |
| `rejected` | bare, ambiguous symbol, **not** confirmed — `rejection_reason` explains why |

`mention.counted` is `True` for `high`/`medium`/`low` and `False` for
`rejected`. `mention.start`/`mention.end` give the source-text span.

### Correctness note: company-name self-reference

`name_tokens(symbol)` deliberately excludes the symbol's own lowercased
spelling. Without this, a company whose name starts with its own ticker word
(e.g. `ON` → "ON Semiconductor Corp") would let *any* bare mention of that
word (e.g. "Turn it ON") self-confirm as a ticker mention via the
company-name co-mention rule — defeating the ambiguity check it exists to
enforce. This was caught by `test_false_positive_turn_it_on` while extending
test coverage for this slice; see `test_name_tokens_exclude_symbols_own_spelling`
for the regression test.

## Running the tests

```bash
python3 -m pytest tests/ -q
```

Everything under `tests/unit/` runs offline — no network calls, no live
credentials, no broker or Reddit access. Relevant files for this slice:

- `tests/unit/test_trading_schema.py` — schema creation/idempotency,
  required tables/indexes, FK enforcement, uniqueness (idempotency keys),
  the immutable-recommendation trigger, the `real_orders` write-block, the
  legacy rename migration.
- `tests/unit/test_tickers.py` — normalization, unknown/OTC/inactive
  rejection, ambiguity flags, CSV loading, the name-token self-reference fix.
- `tests/unit/test_ticker_extractor.py` — cashtags, bare-symbol context
  confirmation, confidence categories, rejection reasons, the required
  false-positive and true-positive example phrases from the implementation
  plan.
- `tests/unit/test_recommendation_fixtures.py` — every fixture in
  `tests/fixtures/recommendations/` against the schema.
- `tests/unit/test_recommendation_schema_and_cli.py` — the existing
  end-to-end `cli.py analyze` coverage (still passing, now also exercising
  `config_hash`/`git_sha`/`missing_data_reasons`).
