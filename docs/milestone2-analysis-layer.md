# Milestone 2 analysis layer — developer guide

Covers the second slice of Milestone 1 (see
[AI-Stock-Trading-Implementation-Plan.md](AI-Stock-Trading-Implementation-Plan.md)
and [docs/milestone-2.md](milestone-2.md)): Stories 1B.2, 1C.1–1C.4. Everything
described here runs fully offline — no Robinhood, Reddit, market-data, or
Claude API access required, and no network call is made anywhere in this
slice's code or tests.

**Status: research and paper-research only.** This slice adds no paper-fill
or order-execution path. `paper/ledger.py` (paper fills, T+1 settlement)
already existed before this slice and is untouched — the offline service in
this slice (`services/analyze_candidate.py`) never calls it. Real orders
remain prohibited: `real_orders` is still a reserved, trigger-protected
table with no writer anywhere in the codebase.

## Pipeline overview

`services/analyze_candidate.py::analyze_candidate()` is a deterministic
**application service**, not an autonomous agent. Given typed, already-
fetched fixture data for one candidate, it runs a fixed sequence:

1. Validate the symbol against `TickerUniverse` (fail closed to
   `ANALYSIS_INCOMPLETE` on an unverified symbol).
2. Run the screener (`analysis/screener.py`) — hard eligibility gates only.
3. Aggregate already-classified Reddit records (`analysis/sentiment.py`) —
   only if screening passed.
4. Compute the composite score (`analysis/scorer.py`).
5. Run the risk engine (`risk/position_sizing.py`) if screening passed and
   scoring succeeded.
6. Build and schema-validate a frozen recommendation
   (`recommendations/builder.py`).
7. Persist the screening run, candidate score, and frozen recommendation +
   factors in one transaction (`storage/trading_repositories.py`).
8. Return the recommendation.

## Screener (`analysis/screener.py`, `config/screening.yaml`)

Applies **hard eligibility gates only** — it never ranks and never produces
order parameters. Every gate in this slice is a hard failure (there is no
soft/warning-only gate yet; see the module docstring for why the
`hard_failure` field is always `True` for now — the schema/model still
carries the field so a future slice can add warning-only gates without a
breaking change).

Gates: max share price ($25 default), min market cap ($150M), min average
daily dollar volume ($2M), min operating history, OTC exclusion, inactive
exclusion, bankruptcy/distress, going-concern warning, shell company, recent
reverse split, excessive dilution (share-count growth YoY), minimum cash
runway, earnings blackout window, abnormal realized volatility, recent
trading halt, wide bid/ask spread, and stale/missing critical data.

Every gate is evaluated independently — an earlier failure never skips a
later gate, so results are order-independent and every outcome (not just the
first failure) is preserved on `ScreeningResult.gate_results` for
auditability. **Unknown critical input is itself a hard failure** for that
gate; a candidate never passes because data is missing.

Thresholds come from `config/screening.yaml`, hashed (`ScreeningConfig.
config_hash`) via `hashing.hash_config()` so the exact configuration used for
a screening run is reproducible and auditable. Invalid configuration
(missing keys, negative thresholds) raises `ScreeningConfigError` at load
time — not silently ignored or defaulted.

## Composite scorer (`analysis/scorer.py`, `config/scoring.yaml`)

Four pillars — fundamentals (35%), technicals/momentum (30%), catalysts &
risk (25%), Reddit sentiment (≤10%, hard-capped) — configured in
`config/scoring.yaml`. The loader (`load_scoring_config`) rejects any
configuration whose weights don't sum to 1.0, or whose Reddit weight exceeds
either the config's own `reddit_weight_cap` or the architectural hard limit
of 0.10 — **it never silently renormalizes**.

**Optional-factor-absence policy (documented, not implicit):** each pillar's
score is `50 + 50 × mean(normalized values of its AVAILABLE factors)`.
Missing factors are excluded from that mean — never treated as neutral-
favorable zero — but every configured factor still appears in the output
with `data_quality_status="missing"` for full auditability. If a **critical**
pillar (fundamentals/technicals/catalysts) has zero available factors,
`compute_composite_score` raises `ScoringIncompleteError`, which the offline
service converts to `ANALYSIS_INCOMPLETE`. Reddit is the one optional
pillar: zero Reddit factors yields a neutral 50 baseline, never gating the
analysis (Reddit must remain supplementary).

**Exact score reconstruction:** every factor's `contribution` is derived so
that `total_score == 50 + 0.5 × sum(all factor contributions)` — this holds
regardless of which optional factors were missing (see the `_score_pillar`
and `reconstruct_total_score_from_factors` docstrings for the derivation).
`reconstruct_total_score_from_factors()` recomputes the total from nothing
but a flat list of persisted `recommendation_factors` rows — the integration
test proves this against a real SQLite read-back, not just in-memory
objects.

## Risk engine (`risk/position_sizing.py`)

Extends the existing (already-tested) `compute_position_plan` rather than
replacing it. New portfolio-level guardrails, all keyword-only fields on
`RiskInputs`:

- **Fail-closed (required, `None` raises `IncompleteStateError`):**
  `existing_position_shares` (0 is a valid *known* answer; `None` is not),
  `portfolio_exposure_fraction`, `account_state_as_of_epoch` (checked for
  staleness the same way `price_as_of_epoch` already was).
- **Policy gates with a permissive default** (consistent with the
  pre-existing `sector_exposure_fraction=0.0` pattern): daily-loss breach,
  drawdown breach, wide spread, duplicate-entry (averaging into an existing
  position), portfolio-exposure cap, and an optional
  `technical_target_price` that — when supplied and its implied
  reward:risk falls below the configured floor — produces `NO_ACTION`
  instead of silently substituting the computed floor target.

Every zero-share result now carries a machine-readable `no_action_reason`
(e.g. `"stop_at_or_above_entry"`, `"daily_loss_breach"`,
`"duplicate_position"`) alongside the existing human-readable `warnings`
tuple — the existing warning strings were kept verbatim so no prior test's
substring assertions changed.

**Design note — float vs. Decimal:** `compute_position_plan`'s internals
stay float-based (unchanged from the previous slice) because the existing
test suite asserts exact float equality (e.g. `plan.dollars_at_risk ==
500.0`); switching to `Decimal` mid-flight risked breaking those assertions
for no behavioral gain, since the arithmetic (floor division, no round-up)
was already correct. `Decimal` is used instead in the new typed snapshot
models (`models/trading_models.py`) and the screener, where there was no
existing float-exact test to preserve — this satisfies the "Decimal where
practical" requirement without a high-risk rewrite of tested code.

## Deterministic Reddit sentiment aggregation (`analysis/sentiment.py`)

Aggregates already-stored, already-classified records — it never retrieves
Reddit data and never calls an LLM. A record's classification can arrive
pre-computed via `RedditRecord.classification` (a `Classification` with
label/confidence/catalyst_phrases/risk_phrases — the pluggable slot a future
bounded Claude classifier fills); when absent, the deterministic
`KeywordClassifier` fixture stand-in is used instead.

New metrics beyond the previous slice: mention velocity, engagement-weighted
sentiment score, sentiment-confidence distribution, duplicate/cross-post/
repeated-link counts, a promotion-risk flag (ratio-based heuristic,
documented threshold), ambiguous/context-confirmed/cashtag mention counts,
new-account concentration (only reported when account-age metadata is
present — `None`, never fabricated, otherwise), catalyst/risk phrase
aggregation, and an optional price-lead/discussion-lead indicator (`None`
unless timestamped price points are supplied). `aggregate_windows()` chains
several consecutive windows with automatic growth computation against each
prior window.

Empty input returns a valid zero-data aggregate (`bullish == bearish ==
neutral == 0`, `net_sentiment == 0.0`, empty confidence distribution) — never
a bullish or bearish conclusion.

## Recommendation builder (`recommendations/builder.py`)

Combines a `ScreeningResult`, `CompositeScore`, and `PositionPlan` into a
single record validated against `schemas/recommendation.schema.json` both by
typed-domain checks (`_validate_domain`) and by the JSON Schema itself.
Status/side mapping:

| Situation | side | status |
|---|---|---|
| symbol invalid / any missing-data reason | `analysis_incomplete` | `analysis_incomplete` |
| screener rejected the candidate | `screened_out` | `active` |
| risk engine returned 0 shares | `no_action` | `active` |
| risk engine returned > 0 shares | `buy_candidate` | `active` |

`rec_id` is derived deterministically from a caller-supplied
`idempotency_key` (`derive_rec_id`) — the same key always produces the same
`rec_id`, so a retried creation is a persistence no-op
(`trading_repositories.save_frozen_recommendation` returns `False` instead
of conflicting), never a duplicate. There is deliberately no update method
on `FrozenRecommendation` — freezing happens once, at construction.

### Schema compatibility correction

`schemas/recommendation.schema.json`'s `side` enum was
`[buy_candidate, watch, no_action, analysis_incomplete]` — it had no value
for "the screener rejected this candidate," which this slice's screener
needed to represent. Smallest safe correction: added `"screened_out"` to the
`side` enum and to the existing `allOf` rule that forces `risk_plan` to
`null` for non-actionable sides (previously only `no_action` and
`analysis_incomplete`). No DB migration was needed — `recommendations.side`
has no `CHECK` constraint, only the JSON Schema enumerates valid values. A
matching `valid_screened_out.json` / `invalid_screened_out_with_risk_plan.json`
fixture pair was added to `tests/fixtures/recommendations/`.

## Persistence (`storage/trading_repositories.py`)

Separate from `storage/repositories.py` (the research-pipeline repositories
over `migrations.py`'s tables) since this module operates on
`trading_schema.py`'s tables. `save_frozen_recommendation` checks existence
by `rec_id` first (idempotent no-op if already present), then inserts the
recommendation row and every factor row, committing once; any failure
partway through rolls back the whole transaction — no partially-persisted
recommendation is ever left behind (tested via a deliberate `factor`
primary-key collision in `test_recommendation_builder.py`). No function in
this module — or anywhere else added in this slice — writes to
`real_orders`; the reserved-table triggers from the previous slice remain
the enforcement backstop regardless.

## Fail-closed behaviors in this slice

- Screener: unknown critical input hard-fails that gate; stale/missing
  freshness timestamps hard-fail the `max_data_staleness_seconds` gate.
- Scorer: a critical pillar with zero available factors raises
  `ScoringIncompleteError` rather than scoring on partial data.
- Risk engine: unknown position state, unknown portfolio exposure, and
  stale account state now raise `IncompleteStateError`, joining the
  previous slice's unknown-equity/cash/price/earnings-date checks.
- Recommendation builder: any `missing_data_reasons` entry forces
  `ANALYSIS_INCOMPLETE` with `risk_plan = null`, before any other branch is
  considered.

## Running the tests

```bash
python3 -m pytest tests/ -q
```

New/updated files for this slice: `tests/unit/test_screener.py`,
`tests/unit/test_scorer.py`, `tests/unit/test_recommendation_builder.py`,
`tests/integration/test_analyze_candidate.py`, plus extensions to
`tests/unit/test_sentiment.py`'s coverage (via the module's own docstring
examples — existing tests were kept unchanged), `tests/unit/
test_position_sizing.py` (new portfolio-guardrail tests), and
`tests/unit/test_recommendation_fixtures.py` (the `screened_out` fixture
pair).

## Running the offline single-candidate analysis service

```python
from datetime import datetime, timezone
from trading_research.analysis.scorer import load_scoring_config
from trading_research.analysis.screener import load_screening_config
from trading_research.services.analyze_candidate import CandidateInput, analyze_candidate
from trading_research.storage.database import connect
from trading_research.universe.tickers import default_universe

conn = connect("data/research.sqlite3")
result = analyze_candidate(
    CandidateInput(...),  # typed snapshots — see tests/integration/test_analyze_candidate.py
    default_universe(), load_screening_config(), load_scoring_config(),
    conn, datetime.now(timezone.utc),
)
print(result.recommendation.to_dict())
```

See `tests/integration/test_analyze_candidate.py::good_candidate()` for a
complete, realistic set of fixture inputs.

## What remains prohibited / not yet implemented

- No simulated fills or paper-order execution from this slice's code path
  (`paper/ledger.py` exists from the previous slice but is not called here).
- No evaluator / performance-measurement implementation.
- No live market-data, Reddit, Robinhood, or Claude API calls anywhere in
  this slice or its tests.
- No real-order implementation; `real_orders` remains reserved and
  trigger-protected.

## Recommended next implementation slice

Paper ledger integration (wiring `analyze_candidate`'s output into
`paper/ledger.py::submit_and_fill` behind an explicit, separately-confirmed
step), followed by the evaluator and a CLI/orchestration layer over the full
offline pipeline.
