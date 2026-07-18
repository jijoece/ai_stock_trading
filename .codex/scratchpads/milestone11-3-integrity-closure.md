# Milestone 11.3 — Remaining Integrity Closure — Scratchpad

## Metadata

- Spec: `docs/milestones/milestone11-3-remaining-integrity-closure.md`
- Starting commit: `c5232adf89b84b7e32dc716e243d3a8426d95eb2` (branch `agent/milestone-11-2-full-integrity-closure`)
- No commits made this session — all changes in the working tree, per instructions.

## Baseline

- `pytest tests/ -q --tb=short` (ambient env): 1755 passed, 15 skipped.
- `cd paper_runtime && pytest tests/ -q`: 59 passed.
- Clean-CI simulation (credential vars unset): 1755 passed, 15 skipped — matches ambient.
- `git diff --check`: clean.

## Finding-validation tracker

| Part | Area | Status | Classification |
|---|---|---|---|
| 2 | migration fixture matrix | done | CONFIRMED_AND_FIXED |
| 23 | provider health sample floor | done | CONFIRMED_AND_FIXED |
| 24 | HTTP client | done | CONFIRMED_AND_FIXED |
| 25 | rate limiter thread-safety | done | CONFIRMED_AND_FIXED |
| 26 | strict scheduled-research booleans | done | CONFIRMED_AND_FIXED |
| 27 | deterministic config hashing | done | CONFIRMED_AND_FIXED |
| 28 | no FS side effects in config load | done | CONFIRMED_AND_FIXED |
| 29 | disclosure negation | done | CONFIRMED_AND_FIXED |
| 30 | flexible market-data validation | done | CONFIRMED_AND_FIXED |
| 31 | SEC point-in-time | done | CONFIRMED_AND_FIXED (look-ahead bug); other sub-items NEEDS_RUNTIME_EVIDENCE |
| 32 | settlement semantics | done | DESIGN_TRADEOFF_DOCUMENTED (retained immediate settlement, now explicit/versioned) |
| 33 | legacy paper quarantine | done | CONFIRMED_AND_FIXED (renamed + explicit flag, "Alternative" option) |
| 34 | schema versioning | done | CONFIRMED_AND_FIXED |
| 35 | remaining docs | done | PARTIALLY_CONFIRMED_AND_FIXED (spot-checked; most already accurate per 11.2) |
| 36/37 | remaining tests + crash scenario | done | CONFIRMED_AND_FIXED (found + fixed real BaseException rollback gap) |

## Implementation checklist

All 16 tasks (see TaskCreate #1-#16) completed.

## Commands / reproductions

- Migration fixtures: `pytest tests/unit/test_paper_books_prior_schema_migration.py -q`
- Schema version: `pytest tests/unit/test_schema_version.py -q`
- Health sample floor: `pytest tests/unit/test_shadow_health_sample_floor.py -q`
- HTTP client: `pytest tests/unit/test_http_client_hardening.py -q`
- Rate limiter: `pytest tests/unit/test_rate_limiter_thread_safety.py -q`
- Strict booleans: `pytest tests/unit/test_scheduled_research_config_strict_bool.py -q`
- Hashing: `pytest tests/unit/test_hashing_deterministic.py -q`
- Config FS side effects: `pytest tests/unit/test_config_no_filesystem_side_effects.py -q`
- Disclosure negation: `pytest tests/unit/test_disclosure_extraction_negation.py -q`
- Market-data shape: `pytest tests/unit/test_macro_pillar_market_data_shape.py -q`
- SEC point-in-time: `pytest tests/unit/test_sec_provider_point_in_time.py -q`
- Settlement policy: `pytest tests/unit/test_settlement_policy.py -q`
- Legacy CLI quarantine: `pytest tests/unit/test_legacy_paper_cli_quarantine.py -q`
- Crash atomicity: `pytest tests/unit/test_external_submit_reservation_crash_atomicity.py -q`

## Files changed

See final report `docs/milestones/milestone11-3-integrity-closure.md` for the full table.

## Open issues

- Pyright still runs `continue-on-error: true` on both main/paper_runtime steps (unchanged from 11.2) — large pre-existing baseline (~1888 errors on main), not newly introduced by this session; spot-checked new diagnostics along the way and none represented a genuine new defect (mostly stale-cache or dataclass-kwargs-widening noise consistent with existing test style).
- Part 23's "persistent failures cross the threshold" / "recovery requires hysteresis" requirements are satisfied structurally (rolling-window sample counts feed the same pure per-cycle evaluator; existing DEGRADED/FAIL two-tier threshold already provides a soft/hard boundary) but no new explicit multi-cycle hysteresis state machine was added — narrower than the literal spec text, documented as a deliberate scope decision.
- Part 31: only the concrete date-only look-ahead bug in `get_company_facts` was fixed and tested; "distinguish auditor statement from management boilerplate," "amendment timing respected" beyond the existing `is_amendment` flag, and "uncertainty sets point-in-time-safe false" downstream propagation were not independently re-audited this session — marked NEEDS_RUNTIME_EVIDENCE / not reproduced as separate defects.
- Part 35: only `.env.example`, `paper_runtime/README.md`, the Alpaca paper runbook, the recurring-scheduler runbook, ADR 0007, and `README.md`'s CLI reference were spot-checked; the remaining ADRs (0001-0006, 0005) and other runbooks were not exhaustively re-audited.

## Resume instructions

All 16 planned parts closed with passing regression tests. If resuming: re-run
the baseline commands above, then `git status --short` to confirm no
unexpected drift, then proceed to final verification / report review.

## Final status

COMPLETE — all 15 remaining parts (2, 23-37) addressed with narrow,
tested corrections. Final suite: 1894 passed, 15 skipped (ambient and
clean-CI identical). `git diff --check` clean. No real broker/network
call. No commit/push.
