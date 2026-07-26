# Master Plan — Revised PR Sequence

This supersedes the literal PR sequence in `docs/milestones/rebuild/plan.md`
where repository evidence and accepted ADRs required adjustment. See
`DECISIONS.md` for the reasoning behind each divergence and
`DEPENDENCY_MATRIX.md` for the compatibility findings that shaped PR 1, 4,
and the Category-B PRs (13/14).

Per-PR execution still follows `plan.md`'s "Per-PR execution protocol"
(bounded context read, verify current state, present implementation plan,
clear planning context, implement, test incrementally, review, update
status, commit and stop). This document defines *what* each PR contains;
it does not re-litigate *how* each PR session should run.

| PR | Title | Scope | Dependency on earlier PRs | Risk | Model |
|---|---|---|---|---|---|
| 0 | Architecture decisions and ADR reconciliation | This document set; no code | None | Low | Opus plan |
| 1 | Dependency compatibility and optional groups | `pyproject.toml`: raise Python floor to 3.11, add the 8 groups from `DEPENDENCY_MATRIX.md` Section 3; adopt `hypothesis` + `time-machine` into `dev` immediately | PR 0 | Low | Sonnet |
| 2 | Boundary validation evaluation | Pydantic v2 at YAML/env/CLI/JSONL boundaries only (`DECISIONS.md` D2); frozen domain dataclasses unchanged; compare against current hand-written validation | PR 1 | Medium | Sonnet, Opus review |
| 3 | `exchange_calendars` migration | Replace `evaluation/market_calendar.py`; fixture parity across sessions/holidays/DST/pre-market/intraday/after-hours/previous-next session/session counts | PR 1 | Low-Medium | Sonnet |
| 4 | Generic indicator parity and migration | Replace `scripts/indicators.py` with TA-Lib; add required native-library CI step (`brew install ta-lib` / apt equivalent); document warm-up/null/rounding semantics; pandas-ta-classic evaluated only as fallback if the native step is infeasible somewhere | PR 1 | Low-Medium | Sonnet |
| 5 | VectorBT research adapter | New, additive `research` group adapter for signal matrices/parameter sweeps; add an import-boundary test (VectorBT never imported from `paper_runtime` or vice versa, analogous to the LumiBot AST test); no execution authority | PR 1 | Low | Sonnet |
| — | **Pre-step before PR 6** | Resolve the LumiBot-backtest-mode import-boundary question in `DECISIONS.md` D4 open item 1 — record the resolution in `DECISIONS.md` before PR 6 implementation begins | PR 5 | — | Opus review |
| 6 | LumiBot backtest evaluation adapter | New adapter beside existing `backtesting/engine.py`, within whatever boundary the pre-step resolved; no deletion | Pre-step above | Medium | Sonnet, Opus review |
| 7 | Backtest parity report | Run identical fixture signals through old engine and LumiBot backtester; compare orders, fills, entry/exit timestamps, prices, quantities, cash, positions, fees, realized/unrealized P&L, equity, drawdown, final value; classify differences as old-engine defect / adapter defect / intentional library semantic difference / unsupported requirement | PR 6 | Medium | Sonnet |
| 8 | Decide whether custom backtest component can be safely removed | Decision gate only, based on PR 7 evidence — not a pre-committed removal | PR 7 | High (decision) | Opus review |
| 9 | Strengthen LumiBot runtime normalization contract | `runtime/lumibot/adapter.py`, `paper_runtime/.../lumibot_gateway.py`: normalize orders/statuses/fills/positions/account snapshots. **Replaces original plan.md PR 9** — see `DECISIONS.md` D1 | PR 1 | High | Opus plan + Sonnet |
| 10 | Broker-to-paper_books reconciliation parity tests | Prove reconciliation correctness between normalized LumiBot observations and `paper_books` accounting; **do not remove the book ledger**. **Replaces original plan.md PR 10** — see `DECISIONS.md` D1 | PR 9 | High | Opus plan + Sonnet |
| 11 | QuantStats/analytics migration | Replace `evaluation/metrics.py` formulas with `quantstats-lumi` + `empyrical-reloaded`; keep `Decimal`→float boundary explicit; annualization/insufficient-data semantics must match | PR 1 | Low-Medium | Sonnet |
| 12 | Riskfolio-Lib evaluation only | Advisory allocation output, gated through the same "advisory only, never authoritative" boundary as the Claude research overlay (ADR 0003 pattern); document that this group transitively pulls VectorBT's pandas/numpy chain | PR 1 | Medium | Sonnet, Opus review |
| 13 | SQLAlchemy/Alembic feasibility and ADR | No implementation. Must explicitly test: (a) trigger-protected tables (append-only tables, `real_orders`) only ever touched via SQLAlchemy Core statements, never ORM-session flush/unit-of-work; (b) whether Alembic's branching revision graph can be constrained to linear-only history matching the current monotonic gate. Produce a new ADR only if adoption is recommended | PR 1 | High (decision) | Opus review |
| 14 | APScheduler/Tenacity feasibility | Reframed from "replace" to "coexist": APScheduler v3 (not v4 — not production-ready) may handle simple due-time triggering only; existing lease/generation-fencing logic stays custom. Tenacity gains a structural test proving no `@retry`/`Retrying()` usage wraps the ambiguous-broker-retry path in `external_broker.py` | PR 1 | High (decision) | Opus review |
| 15 | Structlog migration | Replace `logging_config.py`; port custom redaction/secret-registration logic to a structlog processor; parity test for redaction behavior | PR 1 | Medium | Sonnet |
| 16 | OpenTelemetry migration | Additive tracing/metrics SDK only; no exporter configured by default in tests (offline-safe); domain telemetry (`cycle_telemetry.py`, `paper_books/metrics.py`) is unaffected and stays | PR 15 | Low-Medium | Sonnet |
| 17 | Remove only approved commodity implementations | Execute only removal-manifest entries that passed parity in PR 3/4/11/15 (and PR 8's decision, if it approved removal) | PR 3, 4, 8, 11, 15 | Medium | Sonnet, Opus review |
| 18 | Final authority and safety audit | Confirm plan.md completion criteria; no dual authorities remain; `REMOVAL_MANIFEST.md` has no unresolved target | All prior | High | Opus review |

## Explicit non-goals carried forward from plan.md and docs/milestones/rebuild/1.md

- No PR in this sequence removes `paper_books/*`, `backtesting/engine.py`,
  `external_broker.py`, or `storage/*` without an explicit decision gate
  (PR 8, PR 13) and, for `paper_books`, an approved superseding ADR that
  does not currently exist and is not scheduled.
- No PR moves a hard safety limit, account-fingerprint check, or
  ambiguous-retry rule into a library default, LumiBot strategy callback,
  broker configuration, CLI presentation logic, or environment variable.
- PR 2 (Pydantic) and PR 12 (Riskfolio-Lib) each require the same
  boundary-respecting pattern already established for Claude's research
  overlay: advisory/boundary only, never authoritative over risk, sizing, or
  order construction.
