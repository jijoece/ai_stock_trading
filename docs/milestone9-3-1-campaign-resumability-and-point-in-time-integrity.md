# Milestone 9.3.1 — Campaign resumability and point-in-time integrity

Milestone 9.3.1 keeps the soak workflow manual, disabled by default, local-paper-only, and advisory.
It does not install a scheduler, contact a broker, fetch live quotes, activate recurring execution,
clear safety state, or promote an experiment.

## Definitions and attempts

`paper_soak_campaigns` is the immutable campaign definition: campaign ID, canonical manifest hash,
configuration hash, and requested window. Each execution is a `paper_soak_campaign_attempts` row.
An attempt is persisted as `RUNNING` before its first date and then finalized. Its deterministic ID
includes campaign ID, attempt number, manifest hash, configuration hash, and policy version.
`paper_soak_campaign_attempt_days` is append-only.

A normal replay returns the completed attempt. An explicit continuation creates the next attempt,
preserves prior evidence, skips completed dates, and processes skipped or retry-safe blocked dates.
The operator and remediation reason are required. Replaying the same continuation is idempotent.
Manifest or configuration drift under an existing campaign ID fails closed.

On restart, a `RUNNING` attempt resumes at its first missing date. Complete operator evidence is
reconstructed without rerunning lifecycle mutation. A lifecycle run without enough later-stage
evidence becomes `RECOVERY_REQUIRES_REVIEW`; uncertain mutation is never silently repeated.
Unexpected failures abort remaining dates and persist only a bounded error code, stage, and
sanitized message.

## Immutable refreshable reviews

Activation reviews are append-only events. The stable scope is campaign ID plus manifest hash. A
review ID additionally includes attempt ID, configuration hash, frozen evidence-state hash, and
policy version. Changed campaign evidence creates a new review linked through
`supersedes_activation_review_id`; identical frozen evidence returns the existing review. The
latest review is explicit in campaign display output. Reviews never activate execution.

Review inputs are campaign-scoped and bounded by campaign start/end: effective attempt dates,
cycle IDs, operator/lifecycle/verification IDs, reconciliations, valuations, alerts, pause
transitions, comparisons, promotion evidence, and research-run cost. Alert resolution and pause
state are reconstructed at the cutoff. Open positions come from the final immutable campaign
snapshot. Later rows do not alter an earlier review. Missing historical authority remains missing
or insufficient rather than being replaced by current state.

## Time, sessions, and prices

New campaign timestamps use one timezone-aware UTC conversion policy. Naive timestamps are
rejected, equivalent offsets hash identically, and chronological comparisons parse instants rather
than comparing mixed-offset SQLite text.

Normal campaign dates must be U.S. equity trading sessions at or after the regular 4:00 p.m.
America/New_York close. A non-trading date requires `"lifecycle_only": true` and an empty
`cycle_ids` list. The offline calendar intentionally does not model early-close half-days, so
operators must not rely on its regular-close time on those sessions.

Historical `PricePoint` values expose `available_at`. Valuation measures staleness from that
instant and rejects prices unavailable at the requested `as_of`. Alpaca historical bars carry the
regular session close; deterministic fixtures can register an explicit availability instant. No
current quote substitutes for a close.

## Verification and provider qualification

Cross-book source hashes use cutoff-bounded immutable events, snapshots, and child rows. Mutable
current position and remaining-lot state are excluded from historical hashes; holdings can be
reconstructed from cutoff-bounded fills. Future rows cannot stale an earlier verification. A check
without safe reconstruction is not reported as passed.

The existing provider counters remain informational. Readiness uses
`qualifying_real_provider_cycle_count`: a completed cycle must contain real-provider activity and
every observed real-provider result must be `SUCCEEDED`. Until authoritative required-category
sets are persisted, any real `FAILED`, `SOURCE_UNAVAILABLE`, `PARTIAL`, `ATTEMPTED`, or `UNKNOWN`
row disqualifies the cycle.

## SQLite policy

Connections enable foreign keys, WAL journaling, a 5-second bounded busy timeout, and
`synchronous=NORMAL`. New campaign repository writes accept explicit transaction control so an
attempt-day unit of work does not accidentally commit its caller's outer transaction.
