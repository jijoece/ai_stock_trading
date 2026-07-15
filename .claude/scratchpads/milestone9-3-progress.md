# Milestone 9.3 Progress

## Baseline

Milestone 9.2 commit `655d3bd672ffc774e69aad280f10920f986c3420` is an ancestor of HEAD.
Initial bare `pytest` command was unavailable on PATH; repository `.venv/bin/pytest` was used.
Main: `1549 passed, 14 skipped`. Paper runtime: `33 passed`.
Pre-existing unrelated worktree changes were identified and left untouched.

## Milestone 9.2 correction findings

Controlled readiness still indirectly applied the legacy cost-derived provider floor; provenance lacked outcomes,
completed cycles without facts disappeared, evidence facts remained unlinked, readiness returned early, and
cross-book identity omitted source state. Settlement, namespace, and position/lot checks were incomplete.

## Provider success semantics

Normalized outcomes added. Evidence success is explicit `ok`/success/completed/available; Claude success requires
a completed usable orchestration. Failed/unavailable/partial activity is counted but cannot satisfy readiness.
Completed cycles without usable facts are `UNKNOWN`; category totals reconcile.

## Readiness aggregation

All safe checks now evaluate. Fixed primary priority preserves kill/pause first. Failed, blocking, advisory, and
missing check lists remain distinct. Controlled provider history is split from the legacy cost-derived floor.

## Cross-book verification integrity

Stable scope plus source/check state hash drives verification identity. Added stale detection, event-specific
settlement references, unexpected namespace detection, and position/open-lot quantity reconciliation.

## Campaign design

Bounded explicit JSON manifest; strictly ordered aware dates; no discovery. Disabled strict config. Shared one-day
service, immutable campaign/day/review evidence, default stop-on-hard-blocker, explicit continue override.

## Implementation

Added provenance links/outcomes, campaign service/config/schema/repositories/CLI, and point-in-time audit separation.

## Tests

Baseline: main `1549 passed, 14 skipped`; paper runtime `33 passed`. Targeted Part A/campaign/config/integration
suites passed during implementation. Final: main `1575 passed, 14 skipped`; paper runtime `33 passed`.

## Documentation

Added Milestone 9.3 architecture note and campaign runbook; appended Milestone 9.2 correction section.

## Safety review

Campaign contains no provider/network invocation, external broker mutation, live flag, scheduler installation,
pause/kill clearing, alert auto-resolution, or arm promotion.

## Known limitations

Historical mutable position/lot state is verified from the database state available at verification time; immutable
campaign evidence prevents later replays from rewriting prior day records.

## Final status

Complete. Acceptance checks implemented and verified. No commit or push performed.
