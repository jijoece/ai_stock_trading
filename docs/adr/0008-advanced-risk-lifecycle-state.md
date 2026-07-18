# ADR 0008: Versioned advanced-risk lifecycle state

## Status

Accepted — July 2026.

## Context

Fixed percentage exits and aggregate metrics did not provide an authoritative
entry-time loss breaker, replayable ATR stop state, deterministic partial
stages, or a point-in-time macro blackout. Recomputing historical position
state from mutable YAML would make audits and retries unsafe.

## Decision

Persist immutable daily-risk observations and immutable versions of each
position lifecycle state. Append events link stop evaluations and partial
stage completion to their source state and order decision. Exact Decimal
values are stored as TEXT. Entry risk consumes complete, fresh, reconciled
state and fails closed otherwise. Safety pauses are append-only state events
requiring explicit operator resume.

Economic-calendar records are versioned by event ID and content hash. The
blackout policy is pure and model-inaccessible. No undocumented external
calendar integration is introduced.

Historical validation uses one small daily-bar harness that calls production
control functions. It is not a second trading system. Next-session entry and
conservative stop-first bar ambiguity are mandatory.

## Consequences

The database gains additive audit tables and schema-version marker 6. Initial
ATR lifecycle activation requires sufficient point-in-time bars. Books need a
known start-of-day snapshot before loss-controlled entries can proceed. Real
economic-calendar acquisition remains environmentally pending. All new gates
ship disabled except conservative risk thresholds, and no live execution
surface is added.
