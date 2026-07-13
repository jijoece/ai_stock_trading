"""Production shadow-operations control layer (Milestone 7).

This package is a bounded, single-invocation control layer around the
existing scheduled-cycle service (`research/scheduled_cycle.py`) — not a new
authority, not a daemon. See `docs/adr/0005-production-shadow-operations-boundary.md`.

Modules:
    config       -- `config/shadow_operations.yaml` loader (Step 12).
    lease        -- database-backed run lease (Step 14).
    pause        -- global pause/kill switch (Step 15).
    budget       -- cycle-level budget reservation/settlement (Step 16).
    role_budget  -- per-role-call pre-flight budget gate (Step 17).

No module in this package imports anything from `evidence_providers` or
`research` (Claude/provider code) — pause/kill/budget/lease are pure
deterministic application code, per ADR 0005 Decision 4.
"""
