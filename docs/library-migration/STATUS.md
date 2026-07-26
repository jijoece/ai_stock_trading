# Migration Status

**Current phase: PR 0 — complete.**
**Next phase: PR 1 — Dependency compatibility and optional groups.**

## Completed work (PR 0)

- Targeted repository inventory across infra, backtesting/accounting, and
  trading/signals/safety (three parallel read-only agents; see
  `ARCHITECTURE.md` and `COMPONENT_MATRIX.md` for the consolidated result).
- Dependency-compatibility research against live PyPI/GitHub data for all 21
  proposed libraries (see `DEPENDENCY_MATRIX.md`).
- ADR reconciliation for the two direct conflicts between the original
  `docs/milestones/rebuild/plan.md` and accepted ADRs 0001 and 0006 (see
  `DECISIONS.md` D1/D2). Neither required a superseding ADR — both were
  resolved by narrowing scope rather than overriding the original decision.
- Revised PR sequence (`MASTER_PLAN.md`), removal manifest
  (`REMOVAL_MANIFEST.md`), and preservation manifest
  (`PRESERVATION_MANIFEST.md`).

## Custom code removed

None. PR 0 is documentation only.

## Library authority established

None yet. `DEPENDENCY_MATRIX.md` records Adopt/Evaluate/Defer/Reject
decisions but no dependency has been installed and no code has been written.

## Tests run

None — no code changed.

## Remaining blockers

1. **LumiBot-backtest-mode import-boundary question** (`DECISIONS.md` D4,
   open item 1) must be resolved before PR 6 starts. Not a blocker for PR 1.
2. **PR 13/14 feasibility outcomes** (SQLAlchemy/Alembic trigger-safety,
   APScheduler lease-coexistence) are unknown until those PRs run — `PR 1`
   does not depend on them.

## Next PR

**PR 1 — Dependency compatibility and optional groups.**

Scope: `pyproject.toml` only.

- Raise the Python floor from `>=3.10` to `>=3.11`.
- Add the eight dependency groups exactly as specified in
  `DEPENDENCY_MATRIX.md` Section 3 (`core`, `research`, `indicators`,
  `backtest` [reserved, empty], `paper`, `analytics`, `observability`,
  `dev`).
- Add `exchange_calendars` to `core`.
- Add `hypothesis` and `time-machine` to `dev` now (low risk, useful
  immediately for PR 3/4/6/7 parity tests).
- Bump the `paper` extra's `lumibot` pin from `4.5.74` to `4.5.78`.
- Do not add `pydantic`, `tenacity`, `sqlalchemy`, `alembic`, or
  `apscheduler` yet — each is Category B, evaluated in its own later PR.
- No production behavior changes; no new imports in `src/` yet (the groups
  exist as installable extras, PR 3/4/5/11/15 wire them into code).

## Exact next-session prompt

```text
Implement PR 1 for the library-first migration of ai_stock_trading_v2.

Read first (bounded context only):
  docs/library-migration/STATUS.md
  docs/library-migration/DEPENDENCY_MATRIX.md (Sections 2 and 3)
  docs/library-migration/MASTER_PLAN.md (PR 1 row)
  pyproject.toml

Scope: pyproject.toml only.

1. Raise requires-python from >=3.10 to >=3.11.
2. Add optional-dependency groups: core (add exchange_calendars>=4.13,
   alongside existing base deps), research (vectorbt>=1.1.0), indicators
   (TA-Lib>=0.7.1), backtest (empty, reserved), paper (bump lumibot to
   ==4.5.78), analytics (quantstats-lumi, empyrical-reloaded>=0.5.12,
   riskfolio-lib>=7.3.0), observability (structlog>=26.1.0,
   opentelemetry-sdk>=1.44.0, opentelemetry-api>=1.44.0), dev (add
   hypothesis>=6.161.5, time-machine>=3.2.0 to the existing dev group).
3. Do not add pydantic, tenacity, sqlalchemy, alembic, or apscheduler.
4. Do not modify any file under src/, scripts/, paper_runtime/src/, or
   tests/.
5. Do not install any dependency in this session unless verifying the
   dependency resolver succeeds (pip install -e ".[dev,core,research,
   indicators,analytics,observability]" in a scratch venv is acceptable to
   confirm resolution; do not commit a populated .venv).
6. Confirm the `research`/`analytics` groups (which pull VectorBT
   transitively via Riskfolio-Lib) resolve independently of the `paper`
   group (LumiBot) — they are never installed together, matching
   DEPENDENCY_MATRIX.md's isolation finding.
7. Update docs/library-migration/STATUS.md at the end: mark PR 1 complete,
   record actual resolved versions, and set next PR to PR 2 (boundary
   validation evaluation) or PR 3 (exchange_calendars migration) — no
   production behavior changes, so tests are unaffected; run the full
   offline suite once to confirm zero regressions from the pyproject.toml
   change alone.
8. Open one PR. Do not begin PR 2 in the same session. Do not merge
   automatically.
```
