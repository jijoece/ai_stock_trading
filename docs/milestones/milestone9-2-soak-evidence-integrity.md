# Milestone 9.2 — Soak evidence-integrity closure

**Status:** Complete for this session's scope.
**Date:** 2026-07-14.
**Applies to:** `src/trading_research/research/provider_provenance.py`,
`src/trading_research/research/scheduled_cycle.py`,
`src/trading_research/paper_books/cross_book_verification.py`,
`src/trading_research/paper_books/controlled_soak_readiness.py`,
`src/trading_research/paper_books/cli_support.py`, `src/trading_research/cli.py`,
`src/trading_research/storage/research_cycle_schema.py`,
`src/trading_research/storage/research_cycle_repositories.py`,
`src/trading_research/storage/paper_books_schema.py`,
`src/trading_research/storage/paper_books_repositories.py`,
`src/trading_research/storage/shadow_alerts_repositories.py`.

This document records the follow-up Milestone 9.1's own known-limitations section
called out: cost-based real-provider inference, an absent cross-book verification
signal, no alert-resolution CLI, and a readiness result that hides simultaneous
failures. It does **not** activate recurring execution, add an external paper
broker, or touch live trading anywhere.

## Milestone 9.3 corrections

Milestone 9.3 supersedes the original provider-history and verification details in this document.
Controlled readiness now qualifies history only from explicit `SUCCEEDED` real-provider provenance;
failed, unavailable, partial, attempted, and unknown outcomes do not satisfy the floor. Cost remains
only a budget/pricing/reporting signal. Completed cycles missing provenance are counted as `UNKNOWN`,
and category totals reconcile to completed history. Evidence facts are linked append-only to the
resulting research run rather than rewritten.

Readiness evaluates all safe checks and returns every failed or missing check while retaining a fixed
primary-status priority. Cross-book verification IDs now incorporate deterministic source state and
check results; repaired state creates a later immutable event, and stale verification cannot satisfy
recurring-review readiness. Settlement references, unexpected book namespaces, and position/lot
quantities receive explicit checks. See `docs/milestone9-3-evidence-integrity-and-soak-campaign.md`.

## 1. Authoritative provider-provenance classification

Cost was never provider identity — `shadow_run_summaries.cost_usd > 0` proved
spending, not which provider actually ran. Milestone 9.2 replaces it with
`research/provider_provenance.py`, which classifies each research cycle from two
already-authoritative-or-newly-persisted sources, joined by `cycle_id`:

* **Claude**: `research_committee_runs.provider`/`research_attempts.provider`
  (Milestone 5, unchanged) already records the real taxonomy — `"anthropic"` is
  real, `"deterministic"`/`"scripted"` are fixture/scripted
  (`shadow/budget.py::REAL_CLAUDE_PROVIDER`/`PRICING_EXEMPT_PROVIDERS`, reused
  verbatim). Never conflated with cost.
* **Evidence** (market/news/sentiment/fundamentals/filing/corporate_status):
  `research_cycles.provider_mode` (Milestone 6) is a single, whole-cycle
  `"fixture"`/`"real"` flag. `cli.py::_build_evidence_provider_registry` never
  mixes a fixture raw client into a `"real"`-mode cycle — each category is
  either a real client or entirely absent — so, combined with which categories
  a symbol's evidence snapshot actually populated, this is sufficient to
  classify each category without a second provider-request-level table.

### New additive table: `research_cycle_provider_provenance`

One immutable row per `(cycle_id, symbol, provider_category)` actually
observed: `provider_name`, `provider_mode`, `is_fixture`, `is_real`,
`request_or_source_id`, `status`, `observed_at`, `classification_version`.
Insert-or-ignore — a reprocessed symbol never creates a duplicate or
overwrites an earlier classification. No credential, no raw provider payload,
no raw Claude output.

`research/scheduled_cycle.py::_run_symbol` persists it in two places, both
using only data already in scope: right after `save_evidence_snapshot` (one row
per evidence category the snapshot actually populated, using
`configuration.provider_mode`), and right after `research_run_id` is assigned
(the `claude` row, using the actual `research_provider_name` — never
`configuration.provider_mode`, a deliberately separate axis per
`shadow/scheduler.py`'s own documented distinction).

### Classification enum

```python
class ProviderProvenanceClassification(str, Enum):
    FIXTURE_ONLY = "FIXTURE_ONLY"
    REAL_EVIDENCE_ONLY = "REAL_EVIDENCE_ONLY"
    REAL_CLAUDE_ONLY = "REAL_CLAUDE_ONLY"
    REAL_EVIDENCE_AND_CLAUDE = "REAL_EVIDENCE_AND_CLAUDE"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"
```

`classify_cycle`/`compute_real_provider_history` are pure aggregations over
persisted rows: a cycle with zero rows is `UNKNOWN` (never guessed); a cycle
where every row is fixture is `FIXTURE_ONLY`; a cycle counts once toward
`real_provider_cycle_count` regardless of how many real providers or symbols it
touched. `MIXED` is deterministically detected (real and fixture rows coexist
within evidence or within Claude) even though today's wiring cannot produce it
— proven directly, not just structurally.

## 2. Real-provider readiness counting

`controlled_soak_readiness.py`'s `real_provider_cycle_count` check now reads
`provider_provenance.compute_real_provider_history(conn, as_of)` instead of
`shadow_readiness`'s cost-based count. It also exposes, as informational
(non-gating) checks: `fixture_only_cycle_count`, `real_evidence_only_cycle_count`,
`real_claude_only_cycle_count`, `real_evidence_and_claude_cycle_count`,
`mixed_cycle_count`, `unknown_cycle_count`. Cost (`shadow_run_summaries.cost_usd`)
remains untouched as a separate pricing-readiness signal
(`shadow.readiness`'s own `_pricing_not_configured_in_history` check, unchanged).

## 3. Authoritative cross-book verification

`paper_books/cross_book_verification.py::verify_cross_book_integrity` replaces
Milestone 9.1's permanent `_CROSS_BOOK_SIGNAL_AVAILABLE = False` constant. Every
`paper_books` table is already `book_id`-scoped by primary key
(`storage/paper_books_schema.py`'s own documented invariant), so every check
joins by `(book_id, some_id)` — the same identifier text in two correctly
isolated books is structurally never compared cross-book.

Checks (each independently `PASSED`/`FAILED`/`NOT_APPLICABLE`):

| Check | What it proves |
|---|---|
| `book_and_arm_identity` | `BASELINE`/`ENHANCED` books map only to their own arm; assignments never map an arm to a foreign `book_id` |
| `orders_arm_matches_book` | every order's `experiment_arm` matches its own book |
| `fills_reference_own_book_order` | every fill's `paper_order_intent_id` resolves to an order in the same book |
| `cash_ledger_foreign_reference` | a cash-ledger `reference_id` never resolves to the OTHER book's fill/order |
| `lots_reference_own_book_fill` | every lot's `opening_fill_id` resolves to a fill in the same book |
| `lifecycle_symbol_results_scope` | lifecycle exit decisions/orders/fills referenced by a symbol result belong to that same book |
| `reconciliations_own_book` | every reconciliation row's `book_id` column matches the book it was queried under |

Overall status: any `FAILED` check → `FAILED`; zero data observed anywhere →
`INSUFFICIENT_DATA` (absence of an exception is never a persisted pass); at
least one check observed data and none failed → `PASSED`.

### New additive tables

`paper_book_cross_book_verifications` (header: `verification_id`, `as_of`,
`operator_run_id`, `lifecycle_run_id`, `status`, `violation_count`,
`policy_version`) and `paper_book_cross_book_verification_checks` (bounded
per-check rows). Both immutable (no-`UPDATE`/`DELETE` triggers).
`verification_id` is a deterministic hash of `(as_of, operator_run_id,
lifecycle_run_id, policy_version)` — mirrors
`paper_soak_operator_runs.operator_run_id`'s own convention — so a replay for
identical inputs resolves to the same row (insert-or-ignore).

## 4. Cross-book verification feeds readiness

`controlled_soak_readiness.py` reads
`pb_repo.latest_cross_book_verification_upto(conn, as_of)` — the latest
persisted verification at-or-before `as_of` — instead of the hardcoded
constant. New status `NOT_READY_CROSS_BOOK`:

* `FAILED` → `NOT_READY_CROSS_BOOK` (blocks readiness outright).
* `INSUFFICIENT_DATA` (or never run) → permits `READY_FOR_MANUAL_SOAK`/
  `READY_FOR_EXTENDED_MANUAL_SOAK` if every other gate clears, but caps the
  final tier below `READY_FOR_RECURRING_ACTIVATION_REVIEW`.
* `PASSED` → satisfies this gate; `READY_FOR_RECURRING_ACTIVATION_REVIEW` is
  now structurally reachable (was permanently blocked in Milestone 9.1) once
  every other gate and the 2x-minimum-market-days tier also clear.

Nothing here activates or schedules anything — `READY_FOR_RECURRING_ACTIVATION_REVIEW`
remains a review status only, exactly as Milestone 9/9.1.

## 5. Alert-list and alert-resolution CLI

Both reuse Milestone 9.1's own `shadow_alerts_repositories.py` primitives
(`list_alerts`, `resolve_alert`) — no new persistence.

```bash
python -m trading_research.cli shadow-alert-list \
  [--severity CRITICAL] [--unresolved-only] [--limit 50]

python -m trading_research.cli shadow-alert-resolve \
  --alert-id <id> --operator <name> --reason "<reason>"
```

`shadow-alert-list`: read-only, bounded (`limit` clamped to `[1, 200]`,
default 50), deterministic ordering (`created_at DESC`), sanitized (message
capped at 500 chars, no raw provider payload). `shadow-alert-resolve`:
`--operator`/`--reason` required and non-empty; unknown `alert_id` fails
closed with an `"error"` key; `resolve_alert`'s own idempotent
`WHERE resolved_at IS NULL` guard (Milestone 9.1, unchanged) means a second
resolution call never overwrites the original `resolved_by`/`resolved_reason`/
`resolved_at` — the response's `newly_resolved_this_call` flag distinguishes
the two cases. Never touches `shadow_pause_state`; resolving an alert never
implies the underlying incident is repaired. No bulk "resolve all" command.

## 6. Readiness diagnostics

`controlled_soak_readiness.py`'s checks already recorded every gate evaluated
before returning (`ControlledSoakReadinessResult.checks`) — Milestone 9.2 adds
four derived, bounded views on top, computed by `cli_support.py::
_controlled_readiness_to_json` (no change to the underlying evaluation
function or its fail-closed early-return ordering):

* `all_failed_checks` — every check with `passed is False`.
* `blocking_checks` — the subset of those that are not `MISSING` classification.
* `advisory_checks` — every `DERIVED`-classification check.
* `missing_checks` — every `MISSING`-classification check.

The single deterministic `status` (documented priority order, unchanged) is
preserved; these are additive, not a replacement.

## 7. Operator workflow: `paper-soak-run`

Order (Section 12): validate config/pause/kill → run lifecycle (reconciles
internally, unchanged) → **run + persist cross-book verification** → build
soak report → evaluate combined readiness (reads the verification just
persisted) → persist the operator-run summary (now carrying
`cross_book_verification_id`/`cross_book_verification_status`, additive
nullable columns on `paper_soak_operator_runs`). A verification `FAILED`
never erases the lifecycle evidence already persisted above it; replay
remains idempotent (same `operator_run_id`, same `verification_id`, no
duplicate rows). No activation side effect at any step.

## 8. Read-only verification CLI

```bash
python -m trading_research.cli paper-book-cross-check \
  --as-of <ISO-8601> [--operator-run-id <id>] [--lifecycle-run-id <id>]
```

Deterministic, no network call, persists the result (so
`controlled_soak_readiness.py` has an authoritative row even when invoked
standalone, outside `paper-soak-run`). Fails closed when `paper_books.enabled`
is false.

## 9. Deferred (unchanged from Milestone 9.1's own list, plus this session's own)

Unattended recurring activation, launchd installation, an external paper
broker, a per-book `paper_runtime` subprocess pool, partial fills, trailing
stops, live trading, automated promotion, remaining corporate-action types,
`unsupported_claim_rate`'s own denominator, and a bulk "resolve all alerts"
command (single-alert resolution only, matching this milestone's own scope).
