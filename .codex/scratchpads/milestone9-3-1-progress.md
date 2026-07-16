# Milestone 9.3.1 Progress

## Baseline

- Main: 1598 passed, 14 skipped (via `.venv/bin/pytest`; bare `pytest` was not on PATH).
- Paper runtime: 33 passed.

## Campaign-attempt model

- Added immutable definition dates plus resumable attempt headers and append-only attempt days.

## Resume and crash recovery

- RUNNING attempts resume; complete operator evidence reconstructs; uncertain lifecycle-only stage evidence requires review.

## Activation-review integrity

- State-sensitive immutable review IDs, evidence hashes, attempt scope, explicit supersession/latest review.

## Point-in-time corrections

- UTC/session validation, price availability, cutoff alerts/pause/cost/comparison/snapshots, bounded cross-book hashes.

## Provider qualification

- Added all-observed-real-success qualifying count and readiness gate.

## Tests

- Focused campaign/integrity tests and affected Milestone 8 compatibility tests pass.
- Final main suite: 1607 passed, 14 skipped. Paper runtime: 33 passed.

## Documentation

- Added Milestone 9.3.1 design note and updated milestone/runbook commands and limitations.

## Known limitations

- Offline calendar does not model early closes; lifecycle internals retain their existing idempotent commit seams.

## Final status

- Complete; manual/disabled-by-default boundaries preserved.
