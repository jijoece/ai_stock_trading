# Milestone 11 Progress

## Baseline

- Main: 1607 passed, 14 skipped.
- Paper runtime: 33 passed.

## Prerequisite verification

- Milestone 9.3.1 and Milestone 10 symbols verified present.
- Recurring scheduler remains disabled by default and rejects external/live brokers.

## Existing runtime boundary

- Kept broker SDKs and Alpaca credential reads inside `paper_runtime`.
- Main process now passes only PATH/PYTHONPATH to the runtime and exchanges bounded normalized JSONL.

## Account isolation

- One credentialed runtime account enables at most one paper book.
- Raw account IDs are SHA-256 fingerprinted inside the runtime and never cross the boundary.

## Submission state machine

- Added immutable preview/order-event/lookup/reconciliation/fill/queue records.
- `SUBMISSION_REQUESTED` commits before the broker mutation; transport ambiguity becomes `UNKNOWN_REQUIRES_RECONCILIATION`.

## Runtime protocol

- Upgraded both sides to strict `paper-runtime.v2` with an operation allowlist and 65,536-byte envelope limits.
- Added account check, preview, submit, lookup, cancel, fill, position, and account-snapshot operations.

## Reconciliation

- Lookup by deterministic book-scoped client ID precedes any retry.
- Reconciliation checks account, namespace, order, fills, cash, and positions; current critical drift blocks new submission.

## CLI and operator controls

- Added account-check, preview, submit, show, reconcile, cancel, and bounded retry commands.
- Direct credentialed `execute-paper` submission is closed with a migration error.

## Tests

- Added offline workflows for successful partial/final fills, replay, ambiguous submission, authoritative not-found retry, cancellation during drift, and scheduler isolation.
- Added a dedicated real-paper smoke marker/test that is skipped unless the operator flag and exact intent inputs are supplied.
- Final main suite: 1626 passed, 15 skipped.
- Final paper runtime suite: 39 passed.

## Documentation

- Added Milestone 11 architecture, ADR 0007, Alpaca operations runbook, README/config examples, and Milestone 10 boundary note.

## Safety review

- Local simulation remains default; external enablement and submission are both false in shipped config.
- Runtime accepts paper endpoint, LIMIT/DAY, whole-share equity, and long-only behavior; no live path exists.

## Known limitations

- One runtime credential set maps to one book; separate-account multi-book orchestration is deferred.
- No market, fractional, short, option, margin, replacement, automatic external submit, or automatic cancel path.

## Final status

- Implementation and final offline verification complete; real-paper smoke not executed; publication pending.
