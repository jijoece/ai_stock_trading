# ADR 0007 — External Paper Account Isolation

Status: Accepted

## Context

BASELINE and ENHANCED are isolated local books. Combining both in one Alpaca
paper account would destroy broker-side attribution and make account cash and
positions impossible to reconcile independently.

## Decision

One isolated `paper_runtime` credential set maps to at most one externally
enabled paper book. `enabled_book_ids` therefore has a maximum length of one.
Every client order ID starts with a readable book namespace and includes a
collision-resistant digest of immutable approved inputs.

Before preview, submit, retry, cancel, fill application, and reconciliation,
the runtime obtains the broker account ID and returns only a SHA-256-derived
`acct_...` fingerprint. The main process persists that fingerprint, never the
raw account ID, and fails closed if it changes.

Separate-account multi-book orchestration is deferred. Local simulation may
continue for the other book. The recurring scheduler may create and queue an
external-eligible intent but cannot submit or cancel it.

## Consequences

Operators need a distinct paper account/runtime credential mapping to claim
broker-level isolation for another book. This limitation is intentional and
visible. It prevents silent namespace mixing and makes cash/position
reconciliation meaningful. Live trading remains structurally unavailable.
