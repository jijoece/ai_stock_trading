# Runbook: Soak evidence-integrity and alert operations (Milestone 9.2)

Operator-facing procedure for the new Milestone 9.2 commands: cross-book verification,
alert listing, and audited alert resolution. See `docs/milestones/milestone9-2-soak-evidence-integrity.md`
for the full architecture record, `docs/runbooks/controlled-paper-soak.md` for the daily
`paper-soak-run` workflow this extends (unchanged in shape), and `docs/runbooks/shadow-incident-response.md`
for the broader alert-response process.

**Before you start:** nothing here is automated. `paper-soak-run` now runs cross-book verification
as one more step in the same manual, operator-invoked command it always was — no new schedule,
no new activation path.

## 1. Cross-book verification runs automatically inside `paper-soak-run`

No new step to remember: every `paper-soak-run` invocation now also runs and persists a
cross-book verification, and the combined readiness result it prints reflects that verification.

```bash
python -m trading_research.cli paper-soak-run --as-of "2026-07-14T20:00:00Z"
```

The response now includes:

```json
{
  "cross_book_verification_id": "cbv-...",
  "cross_book_verification_status": "PASSED",
  "cross_book_verification": { "status": "PASSED", "violation_count": 0, "checks": [...] },
  "controlled_readiness": { "status": "...", "all_failed_checks": [...], "blocking_checks": [...],
                             "advisory_checks": [...], "missing_checks": [...] }
}
```

If `cross_book_verification_status` is `FAILED`, `controlled_readiness.status` becomes
`NOT_READY_CROSS_BOOK` (or an earlier-ordered `NOT_READY_*` status, if one of those also fires) —
investigate before running again. The lifecycle evidence from that same invocation is still
persisted; nothing is rolled back.

## 2. Running cross-book verification standalone (read-only)

To check isolation without running the full lifecycle (e.g. mid-investigation, or to confirm a
fix before the next scheduled `paper-soak-run`):

```bash
python -m trading_research.cli paper-book-cross-check --as-of "2026-07-14T20:00:00Z"
```

Deterministic, read-only, no network call — persists its own result so the next
`paper-soak-readiness`/`paper-soak-run` sees it as "the latest verification at or before as_of."

## 3. Listing alerts

```bash
python -m trading_research.cli shadow-alert-list --unresolved-only --severity CRITICAL
```

Bounded (`--limit`, default 50, max 200), newest-first, sanitized (no raw provider payload, no
credential). Omit `--severity`/`--unresolved-only` to see everything within the bound.

## 4. Resolving an alert

```bash
python -m trading_research.cli shadow-alert-resolve \
  --alert-id <alert_id> --operator "your-name" --reason "confirmed transient provider outage, recovered"
```

Both `--operator` and `--reason` are required. An unknown `--alert-id` fails closed with an
`"error"` key. Resolving an alert is **not** the same as fixing the underlying incident — it only
records that a human reviewed it. It never clears shadow pause/kill state; if the alert caused a
pause, you still need `shadow-resume` separately (see `docs/runbooks/shadow-incident-response.md`).

Calling `shadow-alert-resolve` again on an already-resolved alert is safe (idempotent) — it never
overwrites the original `resolved_by`/`resolved_reason`/`resolved_at`. The response's
`newly_resolved_this_call` field tells you whether your call was the one that resolved it.

There is no bulk "resolve all" command — resolve alerts one at a time, deliberately.

## 5. Readiness diagnostics

`paper-soak-readiness`'s response (and `paper-soak-run`'s `controlled_readiness` field) now
always includes, alongside the single deterministic `status`:

* `all_failed_checks` — every check that failed, not only the one that determined `status`.
* `blocking_checks` — the subset of those actually gating readiness.
* `advisory_checks` — derived/informational checks (e.g. `shadow_activation_readiness`).
* `missing_checks` — checks with no data at all (distinct from a failure).

Use these when multiple things are wrong at once — the single `status` still tells you which gate
is checked first, but these lists tell you everything else that also needs attention before the
next `paper-soak-run`.
